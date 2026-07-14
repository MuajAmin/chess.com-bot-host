"""
Game state tracker for chess.com.
Detects game start, game end, results, and manages game lifecycle.
"""

import logging
from datetime import datetime, date

logger = logging.getLogger(__name__)


class GameTracker:
    """Tracks game state on chess.com — start, end, results, daily limits."""

    def __init__(self, config, page):
        self.config = config
        self.page = page
        self._games_today = 0
        self._today = date.today()
        self._game_active = False
        self._game_start_time = None

    @property
    def game_active(self):
        return self._game_active

    @property
    def games_today(self):
        # Reset counter if day changed
        if date.today() != self._today:
            self._games_today = 0
            self._today = date.today()
            logger.info("New day — game counter reset.")
        return self._games_today

    @property
    def can_play(self):
        """Check if we haven't exceeded the daily game limit."""
        return self.games_today < self.config.max_games_per_day

    def start_game(self):
        """Mark a new game as started."""
        self._game_active = True
        self._game_start_time = datetime.now()
        self._games_today += 1
        logger.info(
            "Game started! (Game #%d today, limit: %d)",
            self._games_today, self.config.max_games_per_day,
        )

    def end_game(self, result=None):
        """Mark the current game as ended."""
        duration = None
        if self._game_start_time:
            duration = (datetime.now() - self._game_start_time).total_seconds()

        self._game_active = False
        self._game_start_time = None

        logger.info(
            "Game ended. Result: %s, Duration: %ss, Games today: %d/%d",
            result or "unknown",
            f"{duration:.0f}" if duration else "?",
            self._games_today,
            self.config.max_games_per_day,
        )

    async def detect_game_end(self):
        """
        Check if the current game has ended by looking for end-game indicators.

        Returns:
            (is_ended: bool, result: str or None)
        """
        try:
            # Method 1: Check for game-over modal/overlay
            end_selectors = [
                '.game-over-modal',
                '.game-review-modal',
                '.modal-game-over',
                '[class*="game-over"]',
                '.board-modal-container-component',
            ]

            for selector in end_selectors:
                el = self.page.locator(selector)
                if await el.count() > 0:
                    # Try to extract the result text
                    result = await self._extract_result(el)
                    return True, result

            # Method 2: Check for result text in the move list area
            result_indicators = [
                '.result-text',
                '.game-result',
                '[class*="result"]',
            ]

            for selector in result_indicators:
                el = self.page.locator(selector)
                if await el.count() > 0:
                    text = await el.first.text_content()
                    if text and any(r in text for r in ["1-0", "0-1", "½-½", "1/2", "won", "lost", "draw"]):
                        return True, text.strip()

            # Method 3: Check for "New Game" or "Rematch" buttons (game ended)
            postgame_selectors = [
                'button:has-text("New Game")',
                'button:has-text("Rematch")',
                'button:has-text("Game Review")',
                '[data-cy="new-game-button"]',
            ]

            for selector in postgame_selectors:
                el = self.page.locator(selector)
                if await el.count() > 0:
                    return True, "game_ended"

            return False, None

        except Exception as e:
            logger.warning("Game end detection error: %s", e)
            return False, None

    async def _extract_result(self, modal_element):
        """Extract game result text from the end-game modal."""
        try:
            # Try common result text selectors within the modal
            result_selectors = [
                '.game-over-header-component',
                '.header-title-component',
                'h3',
                '.game-result',
            ]

            for selector in result_selectors:
                el = modal_element.locator(selector)
                if await el.count() > 0:
                    text = await el.first.text_content()
                    if text:
                        return text.strip()

            # Fallback: get all text from modal
            all_text = await modal_element.first.text_content()
            return all_text.strip()[:100] if all_text else "unknown"

        except Exception:
            return "unknown"

    async def detect_game_start(self):
        """
        Check if a game is currently in progress.

        Returns:
            True if a game board with active clocks is detected
        """
        try:
            # Check for active game board
            board_present = await self.page.locator(
                'wc-chess-board, .board, chess-board'
            ).count() > 0

            if not board_present:
                return False

            # Check for running clocks (indicates active game)
            active_clock = await self.page.locator(
                '.clock-component--active, .clock-player-turn, '
                '[class*="clock"][class*="active"]'
            ).count()

            return active_clock > 0

        except Exception as e:
            logger.warning("Game start detection error: %s", e)
            return False

    async def dismiss_end_modal(self):
        """Close the game-over modal if present."""
        try:
            close_selectors = [
                '.modal-close-button',
                '.ui_outside-close-component',
                'button[aria-label="Close"]',
                '.icon-font-chess.x',
            ]

            for selector in close_selectors:
                el = self.page.locator(selector)
                if await el.count() > 0:
                    await el.first.click()
                    await self.page.wait_for_timeout(500)
                    logger.debug("End-game modal dismissed.")
                    return True

            # Press Escape as fallback
            await self.page.keyboard.press("Escape")
            await self.page.wait_for_timeout(500)
            return True

        except Exception as e:
            logger.warning("Could not dismiss modal: %s", e)
            return False
