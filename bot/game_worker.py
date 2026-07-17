"""
Game worker subprocess for chess.com bot.

Runs the game loop in an isolated subprocess for memory management.
Connects to the main process's browser via CDP (Chrome DevTools Protocol).

Key design:
- Does NOT launch a browser — only connects via connect_over_cdp()
- Lc0 engine runs IN this subprocess — RAM freed by OS on exit
- Receives CDP endpoint via CDP_ENDPOINT environment variable
- Receives config path via BOT_CONFIG environment variable

When this subprocess exits, ALL memory (Lc0 + python heap + chess objects)
is reclaimed by the OS — no Python GC needed.
"""

import os
import sys
import asyncio
import gc
import logging

import chess
from playwright.async_api import async_playwright

from bot.config import Config
from bot.board_parser import BoardParser
from bot.lc0_engine import Lc0Engine
from bot.timing import HumanTiming, build_position_metrics
from bot.move_maker import MoveMaker
from bot.game_tracker import GameTracker

logger = logging.getLogger(__name__)

GAME_URL_PARTS = ("/game/live/", "/game/daily/", "/play/game/")
GAME_PAGE_WAIT_SECONDS = 15


def _is_game_url(url):
    return any(part in (url or "") for part in GAME_URL_PARTS)


async def _page_has_game_board(page):
    try:
        return await page.evaluate("""
            () => {
                const board = document.querySelector(
                    'wc-chess-board, chess-board, .board, #board-single'
                );
                if (!board) return false;

                const rect = board.getBoundingClientRect();
                if (rect.width < 100 || rect.height < 100) return false;

                return !!document.querySelector(
                    '[class*="clock"], [class*="timer"], [class*="time-component"], ' +
                    '[class*="resign"], [class*="draw"], [class*="abort"]'
                );
            }
        """)
    except Exception as e:
        message = str(e).lower()
        if (
            "execution context was destroyed" in message or
            "navigation" in message or
            "target closed" in message
        ):
            return False
        logger.debug("Game page probe failed for %s: %s", page.url, e)
        return False


async def _select_game_page(browser):
    """
    Find the live game page after the main process accepts a challenge.

    CDP can expose multiple pages, and page ordering is not guaranteed. The
    worker must attach to the page that is actually showing the board.
    """
    deadline = asyncio.get_running_loop().time() + GAME_PAGE_WAIT_SECONDS
    last_urls = []
    url_candidate = None
    url_seen_at = None

    while asyncio.get_running_loop().time() < deadline:
        now = asyncio.get_running_loop().time()
        pages = [
            page
            for context in browser.contexts
            for page in context.pages
            if not page.is_closed()
        ]
        last_urls = [page.url for page in pages]

        for page in pages:
            if await _page_has_game_board(page):
                logger.info("Selected game page by board probe: %s", page.url[:120])
                return page

        for page in pages:
            if _is_game_url(page.url):
                if url_candidate is None:
                    url_candidate = page
                    url_seen_at = now

        if url_candidate is not None and url_candidate.is_closed():
            url_candidate = None
            url_seen_at = None

        if url_candidate is not None and url_seen_at is not None:
            if now - url_seen_at >= 2.5:
                logger.info("Selected game page by URL: %s", url_candidate.url[:120])
                return url_candidate

        await asyncio.sleep(0.25)

    if url_candidate is not None and not url_candidate.is_closed():
        logger.warning(
            "Using game URL page without board probe success: %s",
            url_candidate.url[:120],
        )
        return url_candidate

    logger.error("No active game page found via CDP. Pages seen: %s", last_urls)
    return None


async def play_game(page, config, board_parser, engine, humanizer, move_maker, game_tracker):
    """
    Play a single game from start to finish.

    Core loop: detect turn → read board → get move → humanize → click
    Identical logic to main.py's play_game, but runs in subprocess.
    """
    # Detect which color we're playing
    await board_parser.detect_our_color()
    move_maker.set_color(board_parser.is_board_white_bottom)
    humanizer.reset()

    bot_color = board_parser.bot_color or ("white" if board_parser.is_white else "black")
    color_name = bot_color.upper()
    board_bottom = "WHITE" if board_parser.is_board_white_bottom else "BLACK"
    logger.info("=" * 50)
    logger.info(
        "GAME STARTED - Playing as %s (board bottom: %s, subprocess worker)",
        color_name,
        board_bottom,
    )
    logger.info("=" * 50)

    # Detect time control and feed to timing model
    tc_data = await board_parser.detect_time_control()
    if tc_data:
        humanizer.set_time_control(tc_data["base_time"], tc_data["increment"])

    game_tracker.start_game()
    consecutive_errors = 0
    max_errors = 10
    last_move_key = None       # (fen, uci) of last move we played
    repeat_move_count = 0      # how many times we've tried the same move
    MAX_REPEAT_MOVES = 3       # abort after this many identical attempts
    last_not_our_turn_fen = None
    _wait_logged = False       # flag to log "waiting" only once per opponent turn
    _wait_count = 0            # how many 0.5s cycles we've waited

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
                _wait_count += 1
                if not _wait_logged:
                    logger.info("Waiting for opponent's move...")
                    _wait_logged = True
                # Every 30s of waiting, log a heartbeat so user knows bot is alive
                if _wait_count % 60 == 0:
                    logger.info(
                        "Still waiting for opponent... (%ds elapsed)",
                        _wait_count * 0.5,
                    )
                await asyncio.sleep(0.5)
                continue
            _wait_logged = False
            _wait_count = 0

            # Update clock data for timing model
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

            if board_parser.our_color is not None and board.turn != board_parser.our_color:
                fen = board.fen()
                if fen != last_not_our_turn_fen:
                    logger.info(
                        "Parsed board says %s to move, but we are %s. Waiting for opponent.",
                        "WHITE" if board.turn else "BLACK",
                        "WHITE" if board_parser.our_color else "BLACK",
                    )
                    last_not_our_turn_fen = fen
                last_move_key = None
                repeat_move_count = 0
                await asyncio.sleep(0.5)
                continue
            last_not_our_turn_fen = None

            metrics = build_position_metrics(board)
            if metrics["legal_move_count"] == 0:
                logger.info("No legal moves available.")
                await asyncio.sleep(2)
                continue

            consecutive_errors = 0

            # Decide: optional move change or best engine move
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

            # Guard against replaying the same move on the same position
            move_key = (board.fen(), move.uci())
            if move_key == last_move_key:
                repeat_move_count += 1
                if repeat_move_count >= MAX_REPEAT_MOVES:
                    logger.error(
                        "Same move %s attempted %d times on same position — "
                        "board parsing likely stuck. Aborting game worker.",
                        move.uci(), repeat_move_count,
                    )
                    game_tracker.end_game("error")
                    break
            else:
                last_move_key = move_key
                repeat_move_count = 1

            # Apply human-like delay without changing the selected move
            await humanizer.apply_delay(board, metrics)

            # Make the move with Bézier mouse movement
            success = await move_maker.make_move(move)
            if not success:
                logger.error("Failed to execute move!")
                consecutive_errors += 1
                await asyncio.sleep(1)
                continue

            move_registered = await board_parser.wait_for_position_change(board.fen())
            if not move_registered:
                logger.warning(
                    "Clicked %s but board state did not advance within timeout; "
                    "trying controller fallback.",
                    move.uci(),
                )
                if await move_maker.make_controller_move(move):
                    move_registered = await board_parser.wait_for_position_change(
                        board.fen(),
                        timeout_sec=3.0,
                    )

            if not move_registered:
                logger.warning(
                    "Move %s still did not register after fallback; re-checking position.",
                    move.uci(),
                )
                consecutive_errors += 1
                await asyncio.sleep(0.5)
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


async def worker_main():
    """
    Worker entry point — connect to browser via CDP, play one game, exit.

    The main process provides:
    - CDP_ENDPOINT env var: localhost Chrome DevTools endpoint
    - BOT_CONFIG env var: Path to config.yaml
    """
    cdp_url = os.environ.get("CDP_ENDPOINT")
    if not cdp_url:
        print("ERROR: CDP_ENDPOINT environment variable not set.", file=sys.stderr)
        sys.exit(1)

    config_path = os.environ.get("BOT_CONFIG", "config.yaml")
    config = Config(config_path)

    logger.info("Game worker starting (PID %d)", os.getpid())
    logger.info("  CDP endpoint: %s", cdp_url[:60] + "...")
    logger.info("  Config: %s", config_path)

    engine = None

    try:
        async with async_playwright() as p:
            # Connect to existing browser — DO NOT launch a new one
            browser = await p.chromium.connect_over_cdp(cdp_url)

            # Get the active game page from the main process's browser.
            page = await _select_game_page(browser)
            if page is None:
                sys.exit(1)

            logger.info("Connected to page: %s", page.url[:80])

            # Initialize game components (all in THIS subprocess)
            board_parser = BoardParser(page, config.username)
            engine = Lc0Engine(config)
            humanizer = HumanTiming(config)
            move_maker = MoveMaker(page)
            game_tracker = GameTracker(config, page)

            # Start engine ON-DEMAND in this subprocess
            if not await engine.start():
                logger.error("Failed to start engine in worker. Exiting.")
                sys.exit(1)

            # Play the game
            await play_game(
                page, config, board_parser,
                engine, humanizer, move_maker, game_tracker,
            )

    except Exception as e:
        logger.error("Worker fatal error: %s", e, exc_info=True)
        sys.exit(1)

    finally:
        # Kill engine before exit (belt + suspenders — OS will clean up anyway)
        if engine and engine.is_running:
            await engine.close()

        logger.info(
            "Game worker exiting (PID %d). All RAM will be freed by OS.",
            os.getpid(),
        )


def run():
    """Synchronous entry point for subprocess execution."""
    asyncio.run(worker_main())


if __name__ == "__main__":
    run()
