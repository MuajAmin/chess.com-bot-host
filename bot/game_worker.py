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
from bot.humanizer import Humanizer
from bot.move_maker import MoveMaker
from bot.game_tracker import GameTracker

logger = logging.getLogger(__name__)


async def play_game(page, config, board_parser, engine, humanizer, move_maker, game_tracker):
    """
    Play a single game from start to finish.

    Core loop: detect turn → read board → get move → humanize → click
    Identical logic to main.py's play_game, but runs in subprocess.
    """
    # Detect which color we're playing
    await board_parser.detect_our_color()
    move_maker.set_color(board_parser.is_white)
    humanizer.reset()

    color_name = "WHITE" if board_parser.is_white else "BLACK"
    logger.info("=" * 50)
    logger.info("GAME STARTED — Playing as %s (subprocess worker)", color_name)
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

            # Get the existing page from main process's context
            if not browser.contexts or not browser.contexts[0].pages:
                logger.error("No pages found in connected browser!")
                sys.exit(1)

            page = browser.contexts[0].pages[0]
            logger.info("Connected to page: %s", page.url[:80])

            # Initialize game components (all in THIS subprocess)
            board_parser = BoardParser(page)
            engine = Lc0Engine(config)
            humanizer = Humanizer(config)
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
