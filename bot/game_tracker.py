"""
Game state tracker for chess.com.
Detects game start, game end, results, and manages game lifecycle.
"""

import logging
from datetime import date, datetime

logger = logging.getLogger(__name__)


class GameTracker:
    """Tracks game state on chess.com: start, end, results, daily limits."""

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
        if date.today() != self._today:
            self._games_today = 0
            self._today = date.today()
            logger.info("New day: game counter reset.")
        return self._games_today

    @property
    def can_play(self):
        """Check if we have not exceeded the daily game limit."""
        return self.games_today < self.config.max_games_per_day

    def start_game(self):
        """Mark a new game as started."""
        if self._game_active:
            logger.debug("Game already active; daily counter not incremented again.")
            return

        _ = self.games_today
        self._game_active = True
        self._game_start_time = datetime.now()
        self._games_today += 1
        logger.info(
            "Game started! (Game #%d today, limit: %d)",
            self._games_today,
            self.config.max_games_per_day,
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
        return duration

    async def detect_game_end(self):
        """
        Check if the current game has ended.

        Uses a single browser-side DOM pass instead of many Playwright
        locator/count round trips.
        """
        try:
            result = await self.page.evaluate("""
                () => {
                    const isVisible = (el) => {
                        if (!el) return false;
                        const rect = el.getBoundingClientRect();
                        const style = window.getComputedStyle(el);
                        return rect.width > 0 &&
                            rect.height > 0 &&
                            style.display !== 'none' &&
                            style.visibility !== 'hidden';
                    };

                    const textOf = (el) => (el?.textContent || '').trim();
                    const modalSelectors = [
                        '.game-over-modal',
                        '.game-review-modal',
                        '.modal-game-over',
                        '[class*="game-over"]',
                        '.board-modal-container-component',
                    ];
                    const resultSelectors = [
                        '.game-over-header-component',
                        '.header-title-component',
                        'h3',
                        '.game-result',
                        '.result-text',
                        '[class*="result"]',
                    ];

                    for (const selector of modalSelectors) {
                        const modal = document.querySelector(selector);
                        if (!isVisible(modal)) continue;
                        for (const resultSelector of resultSelectors) {
                            const node = modal.querySelector(resultSelector);
                            const text = textOf(node);
                            if (text) {
                                return { ended: true, result: text.substring(0, 100) };
                            }
                        }
                        const text = textOf(modal);
                        return {
                            ended: true,
                            result: text ? text.substring(0, 100) : 'unknown',
                        };
                    }

                    for (const selector of resultSelectors) {
                        const node = document.querySelector(selector);
                        if (!isVisible(node)) continue;
                        const text = textOf(node);
                        const lower = text.toLowerCase();
                        if (
                            text.includes('1-0') ||
                            text.includes('0-1') ||
                            text.includes('1/2') ||
                            text.includes('\\u00bd') ||
                            lower.includes('won') ||
                            lower.includes('lost') ||
                            lower.includes('draw')
                        ) {
                            return { ended: true, result: text.substring(0, 100) };
                        }
                    }

                    const buttons = document.querySelectorAll('button, [role="button"], a[href]');
                    for (const button of buttons) {
                        if (!isVisible(button)) continue;
                        const attrs = [
                            button.getAttribute('data-cy') || '',
                            button.getAttribute('aria-label') || '',
                            button.getAttribute('title') || '',
                            textOf(button),
                        ].join(' ').toLowerCase();
                        if (
                            attrs.includes('new-game-button') ||
                            attrs.includes('new game') ||
                            attrs.includes('rematch') ||
                            attrs.includes('game review')
                        ) {
                            return { ended: true, result: 'game_ended' };
                        }
                    }

                    return { ended: false, result: null };
                }
            """)
            return bool(result.get("ended")), result.get("result")

        except Exception as e:
            logger.warning("Game end detection error: %s", e)
            return False, None

    async def detect_game_start(self):
        """
        Check if a game is currently in progress.

        Returns True if a board with an active clock is detected.
        """
        try:
            return bool(await self.page.evaluate("""
                () => {
                    const board = document.querySelector('wc-chess-board, .board, chess-board');
                    if (!board) return false;
                    return !!document.querySelector(
                        '.clock-component--active, .clock-player-turn, [class*="clock"][class*="active"]'
                    );
                }
            """))

        except Exception as e:
            logger.warning("Game start detection error: %s", e)
            return False

    async def dismiss_end_modal(self):
        """Close the game-over modal if present."""
        try:
            clicked = await self.page.evaluate("""
                () => {
                    const selectors = [
                        '.modal-close-button',
                        '.ui_outside-close-component',
                        'button[aria-label="Close"]',
                        '.icon-font-chess.x',
                    ];
                    for (const selector of selectors) {
                        const el = document.querySelector(selector);
                        if (el) {
                            el.click();
                            return true;
                        }
                    }
                    return false;
                }
            """)
            if clicked:
                await self.page.wait_for_timeout(250)
                logger.debug("End-game modal dismissed.")
                return True

            await self.page.keyboard.press("Escape")
            await self.page.wait_for_timeout(250)
            return True

        except Exception as e:
            logger.warning("Could not dismiss modal: %s", e)
            return False
