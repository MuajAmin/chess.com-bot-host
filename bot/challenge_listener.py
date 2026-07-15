"""
Challenge listener for chess.com.
Monitors for incoming challenges and accepts them based on configuration.
Supports whitelist mode (specific users) and open mode (accept all).

IMPORTANT: chess.com challenge notifications use ICON buttons (✓ ✗),
not text buttons. The detection must account for this.
"""

import logging

logger = logging.getLogger(__name__)

# How many times to dump debug info (avoid spamming logs)
_MAX_DEBUG_DUMPS = 3


class ChallengeListener:
    """Listens for and accepts incoming chess challenges on chess.com."""

    # Chess.com pages where challenges appear
    HOME_URL = "https://www.chess.com/home"
    PLAY_URL = "https://www.chess.com/play"

    def __init__(self, config, page):
        self.config = config
        self.page = page
        self._debug_dump_count = 0

    async def check_and_accept(self):
        """
        Check for pending challenges and accept one if it matches our criteria.

        Returns:
            True if a challenge was accepted AND we verified we're in a game
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
                declined = await self.decline_challenge(challenge)
                if declined:
                    logger.info("Declined challenge from %s (not in whitelist)", challenger_name)
                else:
                    logger.warning(
                        "Challenge from %s is not in whitelist, but no decline control was found.",
                        challenger_name,
                    )
                return False

            # Accept the challenge
            accepted = await self._accept_challenge(challenge)
            if accepted:
                logger.info("Accepted challenge from: %s", challenger_name)
                # Wait for game to load and verify
                await self.page.wait_for_timeout(3000)

                # CRITICAL: Verify we're actually on a game page
                if await self._verify_game_started():
                    logger.info("✅ Game verified — on game page!")
                    return True
                else:
                    logger.warning("Accepted challenge but did NOT land on a game page. URL: %s",
                                   self.page.url)
                    return False

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
            # ── Step 1: Use JavaScript to detect the REAL challenge notification ──
            # Chess.com challenge notifications are popups/alerts that appear
            # with specific structure. We look for elements that:
            # - Contain "Challenge" text (as in "10 min · Challenge")
            # - Have accept/decline icon buttons nearby
            challenge_info = await self.page.evaluate("""
                () => {
                    // Strategy 1: Look for elements containing "Challenge" with
                    // nearby clickable buttons (the ✓ and ✗ icons)
                    const allEls = document.querySelectorAll('*');
                    for (const el of allEls) {
                        // Skip elements that are huge (like body, main containers)
                        if (el.offsetHeight > 200 || el.offsetWidth > 600) continue;
                        // Skip invisible elements
                        if (el.offsetParent === null && el.tagName !== 'BODY') continue;

                        const text = (el.textContent || '').trim();
                        // Look for challenge notification text patterns
                        if (text.includes('Challenge') && text.includes('min')) {
                            // Found a challenge notification — get its parent container
                            let container = el;
                            // Walk up to find the notification container with buttons
                            for (let i = 0; i < 5; i++) {
                                if (!container.parentElement) break;
                                container = container.parentElement;
                                const buttons = container.querySelectorAll(
                                    'button, [role="button"], a[class*="btn"], [class*="icon"]'
                                );
                                if (buttons.length >= 2) {
                                    return {
                                        found: true,
                                        method: 'text_scan',
                                        containerTag: container.tagName,
                                        containerClass: (container.className || '').substring(0, 200),
                                        containerHTML: container.outerHTML.substring(0, 3000),
                                        text: text.substring(0, 200),
                                        buttonCount: buttons.length,
                                    };
                                }
                            }
                        }
                    }

                    // Strategy 2: Look for elements with challenge-related classes
                    // that also have interactive children
                    const challengeEls = document.querySelectorAll(
                        '[class*="challenge-notification"], ' +
                        '[class*="challenge-popup"], ' +
                        '[class*="challenge-alert"], ' +
                        '[class*="notification"][class*="challenge"], ' +
                        '[class*="incoming-challenge"], ' +
                        '[class*="ChallengeAlert"], ' +
                        '[class*="challenge-component"]'
                    );

                    for (const el of challengeEls) {
                        if (el.offsetParent === null && el.style.display !== 'fixed') continue;
                        return {
                            found: true,
                            method: 'class_match',
                            containerTag: el.tagName,
                            containerClass: (el.className || '').substring(0, 200),
                            containerHTML: el.outerHTML.substring(0, 3000),
                            text: (el.textContent || '').trim().substring(0, 200),
                            buttonCount: el.querySelectorAll('button, [role="button"]').length,
                        };
                    }

                    return { found: false };
                }
            """)

            if challenge_info and challenge_info.get("found"):
                method = challenge_info.get("method", "unknown")
                logger.info(
                    "Challenge found via %s! Text: %s, Buttons: %d, Class: %s",
                    method,
                    challenge_info.get("text", "")[:80],
                    challenge_info.get("buttonCount", 0),
                    challenge_info.get("containerClass", "")[:80],
                )

                # Extract the container class to create a Playwright locator
                container_class = challenge_info.get("containerClass", "")
                container_html = challenge_info.get("containerHTML", "")

                # Try to extract challenger username from the HTML
                username = self._extract_username_from_html(container_html)

                # Create a locator for the challenge container
                element = await self._find_challenge_element(container_class, challenge_info)

                return {
                    "info": challenge_info,
                    "element": element,
                    "username": username,
                    "container_html": container_html,
                }

            return None

        except Exception as e:
            logger.debug("Challenge search error: %s", e)
            return None

    def _extract_username_from_html(self, html):
        """Extract username from challenge notification HTML."""
        import re
        # Look for member links
        match = re.search(r'/member/([^"\'/?]+)', html)
        if match:
            return match.group(1)
        return "unknown"

    async def _find_challenge_element(self, container_class, challenge_info):
        """Create a Playwright locator for the challenge container."""
        try:
            # Try using the first specific class from the container
            if container_class:
                classes = container_class.split()
                for cls in classes:
                    cls = cls.strip()
                    if not cls or len(cls) < 3:
                        continue
                    selector = f".{cls}"
                    try:
                        loc = self.page.locator(selector)
                        if await loc.count() > 0:
                            return loc.first
                    except Exception:
                        continue

            # Fallback: use the text content to find the element
            text = challenge_info.get("text", "")
            if "Challenge" in text:
                # Try to find element containing the challenge text
                loc = self.page.locator(f'text=Challenge')
                if await loc.count() > 0:
                    return loc.first

        except Exception as e:
            logger.debug("Could not create element locator: %s", e)

        return None

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
        Click the accept button on a challenge notification.

        Chess.com uses ICON buttons (green ✓ and red ✗), not text buttons.

        Returns:
            True if successfully clicked accept
        """
        try:
            container_html = challenge.get("container_html", "")
            challenge_el = challenge.get("element")

            # ── Method 1: JavaScript-based accept ──
            # Find the first green/accept button within the challenge notification
            # and click it. This is the most reliable approach.
            js_result = await self.page.evaluate("""
                () => {
                    // Find the challenge notification container
                    const allEls = document.querySelectorAll('*');
                    let container = null;

                    for (const el of allEls) {
                        if (el.offsetHeight > 200 || el.offsetWidth > 600) continue;
                        if (el.offsetParent === null && el.tagName !== 'BODY') continue;
                        const text = (el.textContent || '').trim();
                        if (text.includes('Challenge') && text.includes('min')) {
                            // Walk up to find container with buttons
                            let cur = el;
                            for (let i = 0; i < 5; i++) {
                                if (!cur.parentElement) break;
                                cur = cur.parentElement;
                                const buttons = cur.querySelectorAll(
                                    'button, [role="button"], a'
                                );
                                if (buttons.length >= 2) {
                                    container = cur;
                                    break;
                                }
                            }
                            if (container) break;
                        }
                    }

                    if (!container) {
                        return { clicked: false, reason: 'no_container' };
                    }

                    // Get all clickable elements in the container
                    const clickables = container.querySelectorAll(
                        'button, [role="button"], a, [class*="icon"], svg'
                    );

                    // Strategy A: Look for accept-related attributes
                    for (const btn of clickables) {
                        const aria = (btn.getAttribute('aria-label') || '').toLowerCase();
                        const title = (btn.getAttribute('title') || '').toLowerCase();
                        const cls = (btn.className || '').toLowerCase();
                        const text = (btn.textContent || '').trim().toLowerCase();
                        const dataAttr = btn.getAttribute('data-cy') || '';

                        if (aria.includes('accept') || title.includes('accept') ||
                            cls.includes('accept') || text.includes('accept') ||
                            aria.includes('confirm') || cls.includes('confirm') ||
                            dataAttr.includes('accept')) {
                            btn.click();
                            return {
                                clicked: true,
                                method: 'aria/class match',
                                detail: aria || title || cls.substring(0, 50),
                            };
                        }
                    }

                    // Strategy B: Look for green-colored or checkmark buttons
                    for (const btn of clickables) {
                        const style = window.getComputedStyle(btn);
                        const bgColor = style.backgroundColor;
                        const color = style.color;
                        const cls = (btn.className || '').toLowerCase();

                        // Green buttons (accept)
                        const isGreen = (
                            bgColor.includes('rgb(') && (() => {
                                const match = bgColor.match(/rgb\((\d+),\s*(\d+),\s*(\d+)\)/);
                                if (match) {
                                    const [, r, g, b] = match.map(Number);
                                    return g > r * 1.3 && g > b * 1.3;  // Green dominant
                                }
                                return false;
                            })()
                        ) || cls.includes('green') || cls.includes('success') ||
                           cls.includes('positive') || cls.includes('check');

                        if (isGreen) {
                            btn.click();
                            return {
                                clicked: true,
                                method: 'green/check button',
                                detail: cls.substring(0, 50),
                            };
                        }
                    }

                    // Strategy C: Click the FIRST button (usually accept is first)
                    const buttons = container.querySelectorAll('button, [role="button"]');
                    if (buttons.length >= 2) {
                        buttons[0].click();
                        return {
                            clicked: true,
                            method: 'first button (positional)',
                            detail: (buttons[0].className || '').substring(0, 50),
                        };
                    }
                    if (buttons.length === 1) {
                        buttons[0].click();
                        return {
                            clicked: true,
                            method: 'only button',
                            detail: (buttons[0].className || '').substring(0, 50),
                        };
                    }

                    // Strategy D: Click any SVG-containing element (icon buttons)
                    const svgParents = container.querySelectorAll('*:has(> svg)');
                    if (svgParents.length >= 2) {
                        svgParents[0].click();
                        return {
                            clicked: true,
                            method: 'first svg parent (icon button)',
                            detail: (svgParents[0].className || '').substring(0, 50),
                        };
                    }

                    return { clicked: false, reason: 'no_accept_button_found' };
                }
            """)

            if js_result and js_result.get("clicked"):
                logger.info(
                    "Challenge accepted via JS: method=%s, detail=%s",
                    js_result.get("method"),
                    js_result.get("detail"),
                )
                return True

            reason = js_result.get("reason", "unknown") if js_result else "js_error"
            logger.warning("JS accept failed: %s", reason)

            # ── Method 2: Playwright selectors as fallback ──
            accept_selectors = [
                'button:has-text("Accept")',
                'button:has-text("Play")',
                '[aria-label*="accept" i]',
                '[aria-label*="Accept"]',
                '[title*="Accept"]',
                '[data-cy*="accept"]',
                'button[class*="accept"]',
                'button[class*="green"]',
                'button[class*="check"]',
                'button[class*="confirm"]',
            ]

            for selector in accept_selectors:
                try:
                    btn = self.page.locator(selector)
                    count = await btn.count()
                    for index in range(count):
                        candidate = btn.nth(index)
                        if await candidate.is_visible():
                            await candidate.click()
                            logger.info("Accepted via Playwright: %s", selector)
                            return True
                except Exception:
                    continue

            # DEBUG: dump what we see
            await self._debug_challenge_dom(challenge_el)
            logger.warning("Could not find accept button for challenge")
            return False

        except Exception as e:
            logger.error("Failed to accept challenge: %s", e)
            return False

    async def _verify_game_started(self):
        """
        Verify that we actually landed on a game page after accepting.

        Returns:
            True if we're on a real game page
        """
        try:
            # Wait a moment for navigation
            await self.page.wait_for_timeout(2000)

            url = self.page.url
            logger.debug("Post-accept URL: %s", url)

            # Chess.com game pages have specific URL patterns
            if "/game/live/" in url or "/game/daily/" in url or "/play/game/" in url:
                return True

            # Check if a chess board is present AND we're on a game page
            has_game_board = await self.page.evaluate("""
                () => {
                    // Check for live game board (not puzzle or home page board)
                    const board = document.querySelector('wc-chess-board, chess-board, [class*="board"]');
                    if (!board) return false;

                    // Check for game clock (present in live games)
                    const clock = document.querySelector(
                        '[class*="clock"], [class*="timer"], [class*="time-component"]'
                    );

                    // Check for resign/draw buttons (present in active games)
                    const gameControls = document.querySelector(
                        '[class*="resign"], [class*="draw"], [class*="abort"]'
                    );

                    return !!(clock || gameControls);
                }
            """)

            if has_game_board:
                logger.info("Game board with clock/controls detected on page")
                return True

            # Wait a bit more and check URL again
            await self.page.wait_for_timeout(3000)
            url = self.page.url
            if "/game/live/" in url or "/game/daily/" in url:
                return True

            logger.warning("Not on a game page. URL: %s", url)
            return False

        except Exception as e:
            logger.error("Game verification error: %s", e)
            return False

    async def _debug_challenge_dom(self, challenge_el):
        """Dump challenge element HTML and take screenshot for debugging."""
        if self._debug_dump_count >= _MAX_DEBUG_DUMPS:
            return
        self._debug_dump_count += 1

        try:
            # Dump the challenge element's outer HTML
            if challenge_el:
                try:
                    html = await challenge_el.evaluate("el => el.outerHTML")
                    logger.warning("Challenge element HTML:\n%s", html[:2000])
                except Exception:
                    pass

            # Dump all visible buttons on the page
            buttons_info = await self.page.evaluate("""
                () => {
                    const buttons = document.querySelectorAll('button, [role="button"]');
                    const result = [];
                    for (const btn of buttons) {
                        const isVisible = btn.offsetParent !== null && btn.offsetWidth > 0;
                        if (isVisible) {
                            result.push({
                                tag: btn.tagName,
                                text: (btn.textContent || '').trim().substring(0, 80),
                                classes: (btn.className || '').toString().substring(0, 120),
                                aria: btn.getAttribute('aria-label') || '',
                                title: btn.getAttribute('title') || '',
                            });
                        }
                    }
                    return result;
                }
            """)
            logger.warning("Visible buttons on page (%d): %s", len(buttons_info), buttons_info)

            # Take a debug screenshot
            screenshot_path = "./logs/challenge_debug.png"
            await self.page.screenshot(path=screenshot_path)
            logger.warning("Challenge debug screenshot saved: %s", screenshot_path)
        except Exception as e:
            logger.debug("Debug dump failed: %s", e)

    async def decline_challenge(self, challenge):
        """Decline a challenge (for unwanted challengers)."""
        try:
            # Use JavaScript to find and click the decline/X button
            js_result = await self.page.evaluate("""
                () => {
                    const allEls = document.querySelectorAll('*');
                    let container = null;

                    for (const el of allEls) {
                        if (el.offsetHeight > 200 || el.offsetWidth > 600) continue;
                        if (el.offsetParent === null) continue;
                        const text = (el.textContent || '').trim();
                        if (text.includes('Challenge') && text.includes('min')) {
                            let cur = el;
                            for (let i = 0; i < 5; i++) {
                                if (!cur.parentElement) break;
                                cur = cur.parentElement;
                                const buttons = cur.querySelectorAll('button, [role="button"]');
                                if (buttons.length >= 2) {
                                    container = cur;
                                    break;
                                }
                            }
                            if (container) break;
                        }
                    }

                    if (!container) return false;

                    // Decline is typically the second button or the one with X/close/decline
                    const buttons = container.querySelectorAll('button, [role="button"]');
                    for (const btn of buttons) {
                        const aria = (btn.getAttribute('aria-label') || '').toLowerCase();
                        const cls = (btn.className || '').toLowerCase();
                        if (aria.includes('decline') || aria.includes('reject') ||
                            aria.includes('close') || cls.includes('decline') ||
                            cls.includes('reject') || cls.includes('close') ||
                            cls.includes('red') || cls.includes('negative')) {
                            btn.click();
                            return true;
                        }
                    }

                    // Click the last button (usually decline is second/last)
                    if (buttons.length >= 2) {
                        buttons[buttons.length - 1].click();
                        return true;
                    }

                    return false;
                }
            """)

            if js_result:
                logger.info("Declined challenge")
                return True

            return False

        except Exception as e:
            logger.warning("Failed to decline challenge: %s", e)
            return False
