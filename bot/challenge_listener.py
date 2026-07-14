"""
Challenge listener for chess.com.
Monitors for incoming challenges and accepts them based on configuration.
Supports whitelist mode (specific users) and open mode (accept all).
"""

import logging

logger = logging.getLogger(__name__)


class ChallengeListener:
    """Listens for and accepts incoming chess challenges on chess.com."""

    # Chess.com pages where challenges appear
    HOME_URL = "https://www.chess.com/home"
    PLAY_URL = "https://www.chess.com/play"

    def __init__(self, config, page):
        self.config = config
        self.page = page

    async def check_and_accept(self):
        """
        Check for pending challenges and accept one if it matches our criteria.

        Returns:
            True if a challenge was accepted, False otherwise
        """
        try:
            # Navigate to home/play page to see challenges
            current_url = self.page.url
            if "/play" not in current_url and "/home" not in current_url:
                await self.page.goto(self.PLAY_URL, wait_until="domcontentloaded", timeout=20000)
                await self.page.wait_for_timeout(2000)

            # Look for incoming challenge notifications
            challenge = await self._find_challenge()
            if challenge is None:
                return False

            # Check if we should accept this challenge
            challenger_name = challenge.get("username", "unknown")
            if not self._should_accept(challenger_name):
                logger.info("Declined challenge from %s (not in whitelist)", challenger_name)
                return False

            # Accept the challenge
            accepted = await self._accept_challenge(challenge)
            if accepted:
                logger.info("Accepted challenge from: %s", challenger_name)
                # Wait for game to load
                await self.page.wait_for_timeout(3000)
                return True

            return False

        except Exception as e:
            logger.error("Challenge check failed: %s", e)
            return False

    async def _find_challenge(self):
        """
        Find a pending challenge on the page.

        Returns:
            Dict with challenge info or None
        """
        try:
            # Method 1: Check for challenge popup/notification
            challenge_selectors = [
                # Challenge notification popup
                '.challenge-notification',
                '.notification-challenge',
                '[class*="challenge"][class*="notification"]',
                # Challenge in play area
                '.challenge-component',
                '.pending-challenge',
                # Generic notification with challenge
                '.notification-item:has-text("challenge")',
            ]

            for selector in challenge_selectors:
                elements = self.page.locator(selector)
                count = await elements.count()
                if count > 0:
                    logger.info("Challenge found via selector: %s", selector)

                    # Try to extract challenger username
                    username = await self._extract_challenger_name(elements.first)

                    return {
                        "selector": selector,
                        "element": elements.first,
                        "username": username,
                    }

            # Method 2: Check for challenge via JavaScript (chess.com internal state)
            has_challenge = await self.page.evaluate("""
                () => {
                    // Look for any challenge-related elements
                    const els = document.querySelectorAll(
                        '[class*="challenge"], [class*="Challenge"]'
                    );
                    for (const el of els) {
                        const text = el.textContent || '';
                        if (text.toLowerCase().includes('accept') ||
                            text.toLowerCase().includes('play') ||
                            text.toLowerCase().includes('challenge')) {
                            return true;
                        }
                    }
                    return false;
                }
            """)

            if has_challenge:
                logger.info("Challenge detected via JS scan")
                return {
                    "selector": '[class*="challenge"]',
                    "element": self.page.locator('[class*="challenge"]').first,
                    "username": "unknown",
                }

            return None

        except Exception as e:
            logger.debug("Challenge search error: %s", e)
            return None

    async def _extract_challenger_name(self, challenge_element):
        """Extract the username of the challenger from the challenge element."""
        try:
            # Try common username selectors within the challenge
            name_selectors = [
                '.user-username-component',
                '.username',
                '[class*="username"]',
                '[class*="user-tagline"]',
                'a[href*="/member/"]',
            ]

            for selector in name_selectors:
                name_el = challenge_element.locator(selector)
                if await name_el.count() > 0:
                    name = await name_el.first.text_content()
                    if name:
                        return name.strip()

            return "unknown"

        except Exception:
            return "unknown"

    def _should_accept(self, challenger_name):
        """
        Decide whether to accept a challenge based on config.

        Args:
            challenger_name: Username of the challenger

        Returns:
            True if challenge should be accepted
        """
        if self.config.challenge_mode == "open":
            return True

        if self.config.challenge_mode == "whitelist":
            allowed = [u.lower() for u in self.config.allowed_users]
            if "*" in allowed:
                return True
            return challenger_name.lower() in allowed

        logger.warning("Unknown challenge mode: %s", self.config.challenge_mode)
        return False

    async def _accept_challenge(self, challenge):
        """
        Click the accept button on a challenge.

        Args:
            challenge: Dict with challenge info from _find_challenge

        Returns:
            True if successfully accepted
        """
        try:
            # Try clicking "Accept" button within the challenge element
            accept_selectors = [
                'button:has-text("Accept")',
                'button:has-text("Play")',
                '.challenge-accept',
                '[class*="accept"]',
                'button.accept',
            ]

            # First try within the challenge element
            challenge_el = challenge.get("element")
            if challenge_el:
                for selector in accept_selectors:
                    btn = challenge_el.locator(selector)
                    if await btn.count() > 0:
                        await btn.first.click()
                        logger.info("Accepted via button: %s (in challenge)", selector)
                        return True

            # Fallback: try page-wide accept buttons
            for selector in accept_selectors:
                btn = self.page.locator(selector)
                if await btn.count() > 0:
                    await btn.first.click()
                    logger.info("Accepted via button: %s (page-wide)", selector)
                    return True

            # Last resort: click the challenge element itself
            if challenge_el:
                await challenge_el.click()
                logger.info("Clicked challenge element directly")
                return True

            logger.warning("Could not find accept button for challenge")
            return False

        except Exception as e:
            logger.error("Failed to accept challenge: %s", e)
            return False

    async def decline_challenge(self, challenge):
        """Decline a challenge (for unwanted challengers)."""
        try:
            decline_selectors = [
                'button:has-text("Decline")',
                'button:has-text("Reject")',
                '.challenge-decline',
                '[class*="decline"]',
            ]

            challenge_el = challenge.get("element")
            if challenge_el:
                for selector in decline_selectors:
                    btn = challenge_el.locator(selector)
                    if await btn.count() > 0:
                        await btn.first.click()
                        logger.info("Declined challenge")
                        return True

            return False

        except Exception as e:
            logger.warning("Failed to decline challenge: %s", e)
            return False
