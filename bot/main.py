"""
Chess.com Lc0 Bot — Main Entry Point

On-demand bot lifecycle:
1. Login with stealth browser
2. Listener mode — poll for challenges
3. Accept challenge → start Lc0 (nodes=1 for Maia)
4. Play game with humanized moves
5. Cleanup: kill engine, gc.collect(), recreate browser context
6. Repeat until daily limit
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
from bot.humanizer import Humanizer
from bot.move_maker import MoveMaker
from bot.game_tracker import GameTracker

logger = logging.getLogger(__name__)


async def play_game(session, config, board_parser, engine, humanizer, move_maker, game_tracker):
    """
    Play a single game from start to finish.

    Core loop: detect turn → read board → get move → humanize → click
    """
    page = session.page

    # Detect which color we're playing
    await board_parser.detect_our_color()
    move_maker.set_color(board_parser.is_white)
    humanizer.reset()

    color_name = "WHITE" if board_parser.is_white else "BLACK"
    logger.info("=" * 50)
    logger.info("GAME STARTED — Playing as %s", color_name)
    logger.info("=" * 50)

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

            # Apply human-like delay (Gaussian distribution)
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
    logger.info("  Headless:  %s", config.headless)
    logger.info("=" * 60)

    # Initialize session manager and login
    session = SessionManager(config)
    if not await session.login():
        logger.error("Failed to login. Exiting.")
        await session.close()
        sys.exit(1)

    logger.info("Logged in. Entering listener mode...")

    page = session.page
    challenge_listener = ChallengeListener(config, page)
    game_tracker = GameTracker(config, page)

    try:
        while True:
            # Check daily game limit
            if not game_tracker.can_play:
                await wait_until_midnight()
                await session.refresh_session()
                continue

            # Check for incoming challenges
            logger.debug("Checking for challenges...")
            challenge_accepted = await challenge_listener.check_and_accept()

            if challenge_accepted:
                logger.info("Challenge accepted! Initializing game...")

                # Start engine ON-DEMAND (saves RAM when idle)
                engine = Lc0Engine(config)
                if not await engine.start():
                    logger.error("Failed to start engine. Skipping game.")
                    continue

                # Initialize game components
                board_parser = BoardParser(session.page)
                humanizer = Humanizer(config)
                move_maker = MoveMaker(session.page)

                try:
                    await play_game(
                        session, config, board_parser,
                        engine, humanizer, move_maker, game_tracker,
                    )
                finally:
                    # CRITICAL CLEANUP
                    # 1. Kill engine process (free ~60-200MB)
                    await engine.close()

                    # 2. Python garbage collection
                    gc.collect()

                    # 3. Recreate browser context to prevent Chromium memory leaks
                    # This closes the old Chromium context and creates a fresh one
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
    finally:
        await session.close()
        logger.info("Bot shutdown complete.")


def run():
    """Synchronous entry point."""
    asyncio.run(main())


if __name__ == "__main__":
    run()
