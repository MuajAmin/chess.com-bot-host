"""
Chess.com Lc0 Bot — Main Entry Point

On-demand bot lifecycle:
1. Login with stealth browser
2. Listener mode — poll for challenges
3. Accept challenge → spawn game_worker subprocess (memory isolation)
4. Worker plays game with humanized moves + clock-aware delays
5. Cleanup: subprocess exit frees all Lc0 RAM, recreate browser context
6. Repeat until daily limit

Process isolation model:
- Main process: browser + challenge listening (persistent)
- Worker subprocess: Lc0 + game loop (spawned per-game, killed after)
- CDP endpoint shared via CDP_ENDPOINT env variable
- Worker connects via connect_over_cdp() — no second browser

Fallback: If CDP subprocess mode is unavailable, falls back to the
in-process game loop.
"""

import asyncio
import gc
import logging
import sys
import os
from datetime import datetime, timedelta

from bot.config import Config
from bot.session_manager import SessionManager
from bot.challenge_listener import ChallengeListener
from bot.board_parser import BoardParser
from bot.lc0_engine import Lc0Engine
from bot.humanizer import Humanizer, build_position_metrics
from bot.move_maker import MoveMaker
from bot.game_tracker import GameTracker
from bot.notifier import Notifier

logger = logging.getLogger(__name__)

WORKER_TIMEOUT_RETURN_CODE = 124


def _read_process_memory_kb():
    """Return current process memory stats without requiring psutil."""
    status_path = f"/proc/{os.getpid()}/status"
    try:
        values = {}
        with open(status_path, "r", encoding="utf-8") as status_file:
            for line in status_file:
                if line.startswith(("VmRSS:", "VmHWM:")):
                    key, value = line.split(":", 1)
                    values[key] = int(value.strip().split()[0])
        if values:
            return values
    except OSError:
        pass

    try:
        import resource

        max_rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        return {"VmHWM": int(max_rss)}
    except Exception:
        return {}


def log_memory_snapshot(label):
    stats = _read_process_memory_kb()
    if not stats:
        return
    rss = stats.get("VmRSS")
    hwm = stats.get("VmHWM")
    parts = []
    if rss is not None:
        parts.append(f"rss={rss / 1024:.1f}MB")
    if hwm is not None:
        parts.append(f"hwm={hwm / 1024:.1f}MB")
    logger.info("Memory snapshot (%s): %s", label, ", ".join(parts))


async def play_game_inprocess(session, config, board_parser, engine, humanizer, move_maker, game_tracker):
    """
    Play a single game in-process (fallback when subprocess mode unavailable).

    Core loop: detect turn → read board → get move → humanize → click
    """
    page = session.page

    # Detect which color we're playing
    await board_parser.detect_our_color()
    move_maker.set_color(board_parser.is_white)
    humanizer.reset()

    color_name = "WHITE" if board_parser.is_white else "BLACK"
    logger.info("=" * 50)
    logger.info("GAME STARTED — Playing as %s (in-process fallback)", color_name)
    logger.info("=" * 50)

    # Detect time control and feed to humanizer
    tc_data = await board_parser.detect_time_control()
    if tc_data:
        humanizer.set_time_control(tc_data["base_time"], tc_data["increment"])

    game_tracker.start_game()
    consecutive_errors = 0
    max_errors = 10
    final_result = "completed"
    duration = None

    while True:
        try:
            # Check if game has ended
            is_ended, result = await game_tracker.detect_game_end()
            if is_ended:
                logger.info("Game over! Result: %s", result)
                final_result = result or "completed"
                duration = game_tracker.end_game(final_result)
                await game_tracker.dismiss_end_modal()
                break

            # Wait for our turn
            if not await board_parser.is_our_turn():
                await asyncio.sleep(0.5)
                continue

            # Update clock data for humanizer
            clock_data = await board_parser.get_remaining_time()
            if clock_data:
                humanizer.update_clocks(
                    clock_data["our_time"],
                    clock_data["opp_time"],
                )

            # Read the current board position
            board = await board_parser.get_full_board()
            if board is None:
                logger.warning("Could not parse board. Retrying...")
                consecutive_errors += 1
                if consecutive_errors >= max_errors:
                    logger.error("Too many errors. Aborting game.")
                    final_result = "error"
                    duration = game_tracker.end_game(final_result)
                    break
                await asyncio.sleep(1)
                continue

            if not board.is_valid():
                logger.warning("Invalid board position. Retrying...")
                consecutive_errors += 1
                await asyncio.sleep(1)
                continue

            metrics = build_position_metrics(board)
            if metrics["legal_move_count"] == 0:
                logger.info("No legal moves available.")
                await asyncio.sleep(2)
                continue

            consecutive_errors = 0

            # Decide: blunder or best move?
            if humanizer.should_blunder(board, metrics):
                time_adj = humanizer.get_engine_time_adjustment(board, metrics)
                top_moves = await engine.get_top_moves(
                    board, count=3,
                    time_limit=config.engine_time_per_move * time_adj,
                )
                if top_moves:
                    move = humanizer.pick_blunder_move(top_moves, board)
                else:
                    move = await engine.get_best_move(board)
            else:
                time_adj = humanizer.get_engine_time_adjustment(board, metrics)
                move = await engine.get_best_move(
                    board,
                    time_limit=config.engine_time_per_move * time_adj,
                )

            if move is None:
                logger.error("Engine returned no move!")
                consecutive_errors += 1
                await asyncio.sleep(1)
                continue

            # Apply human-like delay (clock-aware Gaussian distribution)
            await humanizer.apply_delay(board, metrics)

            # Make the move with Bézier mouse movement
            success = await move_maker.make_move(move)
            if not success:
                logger.error("Failed to execute move!")
                consecutive_errors += 1
                await asyncio.sleep(1)
                continue

            logger.info(
                "Move played: %s (move #%d)",
                board.san(move), humanizer._move_number,
            )

            # Brief pause after move
            await asyncio.sleep(0.3)

        except asyncio.CancelledError:
            logger.info("Game cancelled.")
            final_result = "cancelled"
            duration = game_tracker.end_game(final_result)
            break
        except Exception as e:
            logger.error("Game loop error: %s", e, exc_info=True)
            consecutive_errors += 1
            if consecutive_errors >= max_errors:
                logger.error("Too many errors. Aborting game.")
                final_result = "error"
                duration = game_tracker.end_game(final_result)
                break
            await asyncio.sleep(2)

    return final_result, duration


async def play_game_subprocess(ws_endpoint, config):
    """
    Spawn game as a subprocess for memory isolation.

    The subprocess connects to the same browser via CDP and runs
    the entire game loop (Lc0 + board parsing + move making).
    When it exits, OS reclaims all its RAM — no GC needed.

    Returns:
        subprocess return code (0 = success)
    """
    if not ws_endpoint:
        logger.warning("CDP endpoint unavailable; cannot use subprocess mode.")
        return None  # Signal to use in-process fallback

    env = os.environ.copy()
    env["CDP_ENDPOINT"] = ws_endpoint
    env["BOT_CONFIG"] = config.config_path
    timeout = config.worker_timeout_seconds

    logger.info("Spawning game worker subprocess...")
    logger.info("  CDP: %s", ws_endpoint[:60] + "...")
    logger.info("  Timeout: %ss", timeout)

    proc = await asyncio.create_subprocess_exec(
        sys.executable, "-m", "bot.game_worker",
        env=env,
        cwd=os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    )

    try:
        returncode = await asyncio.wait_for(proc.wait(), timeout=timeout)
    except asyncio.TimeoutError:
        logger.error(
            "Game worker subprocess timed out after %ss (PID=%d). Terminating.",
            timeout, proc.pid,
        )
        proc.terminate()
        try:
            await asyncio.wait_for(proc.wait(), timeout=10)
        except asyncio.TimeoutError:
            logger.error("Worker did not terminate cleanly. Killing PID=%d.", proc.pid)
            proc.kill()
            await proc.wait()
        return WORKER_TIMEOUT_RETURN_CODE
    except asyncio.CancelledError:
        logger.warning("Subprocess wait cancelled. Terminating worker PID=%d.", proc.pid)
        proc.terminate()
        try:
            await asyncio.wait_for(proc.wait(), timeout=10)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
        raise

    logger.info(
        "Game worker subprocess exited (code=%d, PID=%d). RAM freed.",
        returncode, proc.pid,
    )
    return returncode


async def wait_until_midnight():
    """Sleep until midnight (reset daily game counter)."""
    now = datetime.now()
    tomorrow = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    wait_seconds = (tomorrow - now).total_seconds()
    logger.info("Daily limit reached. Sleeping %.0f seconds until midnight...", wait_seconds)
    await asyncio.sleep(wait_seconds)


async def main():
    """Main entry point — login, listen, play, cleanup, repeat."""
    config_path = os.environ.get("BOT_CONFIG", "config.yaml")
    config = Config(config_path)

    logger.info("=" * 60)
    logger.info("  Chess.com Lc0 Bot — Starting")
    logger.info("  User:      %s", config.username)
    logger.info("  Engine:    %s (%s backend)", config.engine_type, config.engine_backend)
    logger.info("  Challenge: %s mode", config.challenge_mode)
    logger.info("  Limit:     %d games/day", config.max_games_per_day)
    logger.info("  Login:     %s mode", config.login_mode)
    logger.info("  Headless:  %s", config.headless)
    logger.info("=" * 60)

    # Initialize session manager and notifier
    session = SessionManager(config)
    notifier = Notifier(config)

    # Handle login based on login_mode
    if config.login_mode == "cookie_only":
        # Only try cookie restore, no credential login
        await session.start_browser()
        if not await session._is_logged_in():
            logger.error("Cookie login failed and login_mode='cookie_only'. Exiting.")
            await notifier.error("Cookie login failed. Manual intervention needed.")
            await session.close()
            await notifier.close()
            sys.exit(1)
        logger.info("Logged in via cookies.")
    else:
        # auto or credentials mode
        if not await session.login():
            logger.error("Failed to login. Exiting.")
            await notifier.error("Login failed. Check credentials.")
            await session.close()
            await notifier.close()
            sys.exit(1)

    logger.info("Logged in. Entering listener mode...")

    # Check subprocess mode availability
    subprocess_available = False
    try:
        _ = session.ws_endpoint
        subprocess_available = True
        logger.info("Subprocess mode available (CDP endpoint captured).")
    except RuntimeError:
        logger.info("Subprocess mode unavailable — using in-process game loop.")

    page = session.page
    challenge_listener = ChallengeListener(config, page)
    game_tracker = GameTracker(config, page)

    try:
        while True:
            # Check daily game limit
            if not game_tracker.can_play:
                await notifier.daily_limit_reached(game_tracker.games_today)
                await wait_until_midnight()
                await session.refresh_session()
                continue

            # Check for incoming challenges
            logger.debug("Checking for challenges...")
            challenge_accepted = await challenge_listener.check_and_accept()

            if challenge_accepted:
                logger.info("Challenge accepted! Initializing game...")
                game_tracker.start_game()

                if subprocess_available:
                    # --- SUBPROCESS MODE (preferred) ---
                    # Spawn game in isolated subprocess — all RAM freed on exit
                    # Run subprocess (blocking — main waits for game to finish)
                    returncode = None
                    try:
                        ws_endpoint = session.ws_endpoint
                    except RuntimeError:
                        ws_endpoint = None
                        subprocess_available = False
                        logger.warning("Subprocess mode unavailable. Falling back to in-process.")

                    if subprocess_available:
                        await notifier.game_started(
                            "UNKNOWN",  # Color detected inside subprocess
                            "challenger",
                        )
                        returncode = await play_game_subprocess(ws_endpoint, config)

                    if returncode is None:
                        # CDP endpoint failed — fall through to in-process
                        subprocess_available = False
                        logger.warning("Subprocess mode failed. Falling back to in-process.")
                    else:
                        if returncode == 0:
                            result = "completed"
                        elif returncode == WORKER_TIMEOUT_RETURN_CODE:
                            result = "timeout"
                        else:
                            result = f"error (code={returncode})"

                        duration = game_tracker.end_game(result)
                        await notifier.game_ended(result, duration_secs=duration)

                        # Force Python GC (subprocess already freed its own RAM)
                        gc.collect()

                if not subprocess_available:
                    # --- IN-PROCESS FALLBACK ---
                    engine = Lc0Engine(config)
                    if not await engine.start():
                        logger.error("Failed to start engine. Skipping game.")
                        await notifier.error("Engine start failed. Skipping game.")
                        duration = game_tracker.end_game("engine_start_failed")
                        await notifier.game_ended(
                            "engine_start_failed",
                            duration_secs=duration,
                        )
                        continue

                    board_parser = BoardParser(session.page)
                    humanizer = Humanizer(config)
                    move_maker = MoveMaker(session.page)

                    # Detect color for notification
                    await board_parser.detect_our_color()
                    color_name = "WHITE" if board_parser.is_white else "BLACK"
                    await notifier.game_started(color_name)

                    try:
                        fallback_result, fallback_duration = await play_game_inprocess(
                            session, config, board_parser,
                            engine, humanizer, move_maker, game_tracker,
                        )
                    finally:
                        # CRITICAL CLEANUP
                        # 1. Kill engine process (free ~60-200MB)
                        await engine.close()

                        # 2. Python garbage collection
                        gc.collect()

                    await notifier.game_ended(
                        fallback_result,
                        duration_secs=fallback_duration,
                    )

                # Recreate browser context to prevent Chromium memory leaks
                await session.maybe_recreate_context()

                # Update page references after context recreation
                page = session.page
                challenge_listener = ChallengeListener(config, page)
                game_tracker.page = page

                logger.info(
                    "Cleanup complete. Games: %d/%d, RAM freed.",
                    game_tracker.games_today, config.max_games_per_day,
                )
                if game_tracker.games_today % config.memory_log_interval_games == 0:
                    log_memory_snapshot("post-game cleanup")

                # Pause between games (human behavior)
                pause = 5 + __import__('random').uniform(0, 10)
                logger.info("Pausing %.0fs before next challenge check...", pause)
                await asyncio.sleep(pause)

            else:
                # No challenge — sleep and check again
                await asyncio.sleep(config.check_interval)

    except KeyboardInterrupt:
        logger.info("Bot stopped by user (Ctrl+C).")
    except Exception as e:
        logger.error("Fatal error: %s", e, exc_info=True)
        await notifier.error(f"Fatal error: {e}")
    finally:
        await session.close()
        await notifier.close()
        logger.info("Bot shutdown complete.")


def run():
    """Synchronous entry point."""
    asyncio.run(main())


if __name__ == "__main__":
    run()
