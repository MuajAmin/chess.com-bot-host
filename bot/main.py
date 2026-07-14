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

Fallback: If CDP subprocess mode is unavailable (ws_endpoint capture
failed), falls back to in-process game loop (original behavior).
"""

import asyncio
import gc
import logging
import subprocess
import sys
import os
from datetime import datetime, timedelta

from bot.config import Config
from bot.session_manager import SessionManager
from bot.challenge_listener import ChallengeListener
from bot.board_parser import BoardParser
from bot.lc0_engine import Lc0Engine
from bot.humanizer import Humanizer
from bot.move_maker import MoveMaker
from bot.game_tracker import GameTracker
from bot.notifier import Notifier

logger = logging.getLogger(__name__)


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

    while True:
        try:
            # Check if game has ended
            is_ended, result = await game_tracker.detect_game_end()
            if is_ended:
                logger.info("Game over! Result: %s", result)
                game_tracker.end_game(result)
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
                    game_tracker.end_game("error")
                    break
                await asyncio.sleep(1)
                continue

            if not board.is_valid():
                logger.warning("Invalid board position. Retrying...")
                consecutive_errors += 1
                await asyncio.sleep(1)
                continue

            if not list(board.legal_moves):
                logger.info("No legal moves available.")
                await asyncio.sleep(2)
                continue

            consecutive_errors = 0

            # Decide: blunder or best move?
            if humanizer.should_blunder(board):
                time_adj = humanizer.get_engine_time_adjustment(board)
                top_moves = await engine.get_top_moves(
                    board, count=3,
                    time_limit=config.engine_time_per_move * time_adj,
                )
                if top_moves:
                    move = humanizer.pick_blunder_move(top_moves, board)
                else:
                    move = await engine.get_best_move(board)
            else:
                time_adj = humanizer.get_engine_time_adjustment(board)
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
            await humanizer.apply_delay(board)

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
            game_tracker.end_game("cancelled")
            break
        except Exception as e:
            logger.error("Game loop error: %s", e, exc_info=True)
            consecutive_errors += 1
            if consecutive_errors >= max_errors:
                logger.error("Too many errors. Aborting game.")
                game_tracker.end_game("error")
                break
            await asyncio.sleep(2)


def play_game_subprocess(session, config):
    """
    Spawn game as a subprocess for memory isolation.

    The subprocess connects to the same browser via CDP and runs
    the entire game loop (Lc0 + board parsing + move making).
    When it exits, OS reclaims all its RAM — no GC needed.

    Returns:
        subprocess return code (0 = success)
    """
    try:
        ws_endpoint = session.ws_endpoint
    except RuntimeError:
        logger.warning("CDP endpoint unavailable — cannot use subprocess mode.")
        return None  # Signal to use in-process fallback

    env = os.environ.copy()
    env["CDP_ENDPOINT"] = ws_endpoint
    env["BOT_CONFIG"] = config.config_path

    logger.info("Spawning game worker subprocess...")
    logger.info("  CDP: %s", ws_endpoint[:60] + "...")

    proc = subprocess.Popen(
        [sys.executable, "-m", "bot.game_worker"],
        env=env,
        cwd=os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    )

    # Wait for subprocess to finish (blocks this coroutine)
    returncode = proc.wait()

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
            sys.exit(1)
        logger.info("Logged in via cookies.")
    else:
        # auto or credentials mode
        if not await session.login():
            logger.error("Failed to login. Exiting.")
            await notifier.error("Login failed. Check credentials.")
            await session.close()
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

                if subprocess_available:
                    # --- SUBPROCESS MODE (preferred) ---
                    # Spawn game in isolated subprocess — all RAM freed on exit
                    await notifier.game_started(
                        "UNKNOWN",  # Color detected inside subprocess
                        "challenger",
                    )

                    # Run subprocess (blocking — main waits for game to finish)
                    loop = asyncio.get_event_loop()
                    returncode = await loop.run_in_executor(
                        None, play_game_subprocess, session, config,
                    )

                    if returncode is None:
                        # CDP endpoint failed — fall through to in-process
                        subprocess_available = False
                        logger.warning("Subprocess mode failed. Falling back to in-process.")
                    else:
                        result = "completed" if returncode == 0 else f"error (code={returncode})"
                        await notifier.game_ended(result)

                        # Force Python GC (subprocess already freed its own RAM)
                        gc.collect()

                if not subprocess_available:
                    # --- IN-PROCESS FALLBACK ---
                    engine = Lc0Engine(config)
                    if not await engine.start():
                        logger.error("Failed to start engine. Skipping game.")
                        await notifier.error("Engine start failed. Skipping game.")
                        continue

                    board_parser = BoardParser(session.page)
                    humanizer = Humanizer(config)
                    move_maker = MoveMaker(session.page)

                    # Detect color for notification
                    await board_parser.detect_our_color()
                    color_name = "WHITE" if board_parser.is_white else "BLACK"
                    await notifier.game_started(color_name)

                    try:
                        await play_game_inprocess(
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
                        "completed",
                        duration_secs=(
                            (datetime.now() - game_tracker._game_start_time).total_seconds()
                            if game_tracker._game_start_time else None
                        ),
                    )

                # Recreate browser context to prevent Chromium memory leaks
                await session.maybe_recreate_context()

                # Update page references after context recreation
                page = session.page
                challenge_listener = ChallengeListener(config, page)
                game_tracker = GameTracker(config, page)

                logger.info(
                    "Cleanup complete. Games: %d/%d, RAM freed.",
                    game_tracker.games_today, config.max_games_per_day,
                )

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
        logger.info("Bot shutdown complete.")


def run():
    """Synchronous entry point."""
    asyncio.run(main())


if __name__ == "__main__":
    run()
