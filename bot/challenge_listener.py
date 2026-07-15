"""
Challenge listener for chess.com.
Monitors for incoming challenges and accepts them based on configuration.
Supports whitelist mode (specific users) and open mode (accept all).

IMPORTANT: chess.com challenge notifications use ICON buttons (✓ ✗),
not text buttons. The detection must account for this.
"""

import logging
import re
from urllib.parse import unquote

logger = logging.getLogger(__name__)

# How many times to dump debug info (avoid spamming logs)
_MAX_DEBUG_DUMPS = 3
_CHALLENGE_MARKER_ATTR = "data-bot-challenge-id"
_NAVIGATION_SETTLE_MS = 500
_POST_ACCEPT_SETTLE_MS = 250
_GAME_VERIFY_TIMEOUT_MS = 8000
_GAME_VERIFY_POLL_MS = 250


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
                await self.page.wait_for_timeout(_NAVIGATION_SETTLE_MS)

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
                await self.page.wait_for_timeout(_POST_ACCEPT_SETTLE_MS)

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
            challenge_info = await self.page.evaluate(r"""
                (markerAttr) => {
                    const clickableSelector =
                        'button, [role="button"], a[href], a[class*="btn"], [class*="icon"]';

                    const isVisible = (el) => {
                        if (!el) return false;
                        const rect = el.getBoundingClientRect();
                        const style = window.getComputedStyle(el);
                        return rect.width > 0 &&
                            rect.height > 0 &&
                            style.display !== 'none' &&
                            style.visibility !== 'hidden';
                    };

                    const getClass = (el) => {
                        if (!el) return '';
                        return (
                            el.getAttribute?.('class') ||
                            (typeof el.className === 'string' ? el.className : el.className?.baseVal) ||
                            ''
                        ).toString();
                    };

                    const makeMarker = () => (
                        'challenge-' + Date.now().toString(36) + '-' +
                        Math.random().toString(36).slice(2)
                    );

                    const extractUsername = (container) => {
                        const selectors = [
                            'a[href*="/member/"]',
                            '[data-username]',
                            '[data-user-name]',
                            '[data-test-user]',
                            '[class*="username"]',
                            '[class*="user-name"]',
                        ];

                        for (const selector of selectors) {
                            const node = container.querySelector(selector);
                            if (!node) continue;

                            const href = node.getAttribute?.('href') || '';
                            const hrefMatch = href.match(/\/member\/([^"'/?#\s]+)/i);
                            if (hrefMatch) return decodeURIComponent(hrefMatch[1]);

                            for (const attr of ['data-username', 'data-user-name', 'data-test-user', 'aria-label', 'title']) {
                                const value = (node.getAttribute?.(attr) || '').trim();
                                const attrMatch = value.match(/[A-Za-z0-9_-]{3,25}/);
                                if (attrMatch) return attrMatch[0];
                            }

                            const textMatch = (node.textContent || '').trim().match(/[A-Za-z0-9_-]{3,25}/);
                            if (textMatch) return textMatch[0];
                        }

                        const img = container.querySelector('img[alt]');
                        if (img) {
                            const alt = (img.getAttribute('alt') || '').trim();
                            const altMatch = alt.match(/^([A-Za-z0-9_-]{3,25})(?:'s| profile| avatar|$)/i);
                            if (altMatch) return altMatch[1];
                        }

                        return '';
                    };

                    const challengeTextRe = /\bchallenge\b/i;
                    const timeTextRe =
                        /\b(\d+\s*(min|mins|minute|minutes|sec|secs|second|seconds|hr|hrs|hour|hours)|daily|bullet|blitz|rapid|classical|correspondence|rated|unrated)\b/i;
                    const looksLikeChallengeText = (text) =>
                        challengeTextRe.test(text) && timeTextRe.test(text);

                    const buildInfo = (container, method, sourceText) => {
                        const marker = makeMarker();
                        container.setAttribute(markerAttr, marker);
                        const buttons = container.querySelectorAll(clickableSelector);
                        const containerText = (container.textContent || '').trim();

                        return {
                            found: true,
                            method,
                            marker,
                            username: extractUsername(container),
                            containerTag: container.tagName,
                            containerClass: getClass(container).substring(0, 200),
                            containerHTML: container.outerHTML.substring(0, 5000),
                            text: (sourceText || containerText).substring(0, 200),
                            containerText: containerText.substring(0, 1000),
                            buttonCount: buttons.length,
                        };
                    };

                    const attrsFor = (el) => [
                        el.getAttribute?.('aria-label') || '',
                        el.getAttribute?.('title') || '',
                        el.getAttribute?.('data-cy') || '',
                        el.getAttribute?.('data-icon') || '',
                        getClass(el),
                        (el.textContent || '').trim(),
                    ].join(' ').toLowerCase();

                    const hasAcceptSignal = (el) => {
                        const attrs = attrsFor(el);
                        return attrs.includes('accept') ||
                            attrs.includes('confirm') ||
                            attrs.includes('check') ||
                            attrs.includes('success') ||
                            attrs.includes('positive') ||
                            attrs.includes('green');
                    };

                    const hasDeclineSignal = (el) => {
                        const attrs = attrsFor(el);
                        return attrs.includes('decline') ||
                            attrs.includes('reject') ||
                            attrs.includes('cancel') ||
                            attrs.includes('close') ||
                            attrs.includes('negative') ||
                            attrs.includes('red');
                    };

                    const containerLooksLikeChallenge = (container) => {
                        const text = (container.textContent || '').trim();
                        const cls = getClass(container).toLowerCase();
                        return looksLikeChallengeText(text) ||
                            text.toLowerCase().includes('challenged') ||
                            cls.includes('challenge') ||
                            !!container.querySelector('a[href*="/member/"]');
                    };

                    // Fast path: find an accept/decline pair and walk to its challenge container.
                    const possibleActions = document.querySelectorAll(
                        'button, [role="button"], a[href], [aria-label], [title], [data-cy], [data-icon], ' +
                        '[class*="accept"], [class*="decline"], [class*="reject"], [class*="challenge"]'
                    );
                    for (const action of possibleActions) {
                        if (!isVisible(action) || !hasAcceptSignal(action)) continue;

                        let container = action;
                        for (let i = 0; i < 7; i++) {
                            if (!container.parentElement) break;
                            container = container.parentElement;
                            if (!isVisible(container)) continue;

                            const controls = Array.from(container.querySelectorAll(clickableSelector))
                                .filter(isVisible);
                            const hasDecline = controls.some(hasDeclineSignal);

                            if (
                                controls.length >= 2 &&
                                hasDecline &&
                                containerLooksLikeChallenge(container)
                            ) {
                                return buildInfo(
                                    container,
                                    'action_pair',
                                    (container.textContent || '').trim()
                                );
                            }
                        }
                    }

                    // Strategy 1: Look for challenge text with nearby action controls.
                    for (const el of document.querySelectorAll('*')) {
                        if (!isVisible(el)) continue;

                        const rect = el.getBoundingClientRect();
                        if (rect.height > 250 || rect.width > 900) continue;

                        const text = (el.textContent || '').trim();
                        if (!looksLikeChallengeText(text)) continue;

                        let container = el;
                        for (let i = 0; i < 5; i++) {
                            if (!container.parentElement) break;
                            container = container.parentElement;

                            const buttons = container.querySelectorAll(clickableSelector);
                            if (buttons.length >= 2 && isVisible(container)) {
                                return buildInfo(container, 'text_scan', text);
                            }
                        }
                    }

                    // Strategy 2: Look for challenge-related containers with controls.
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
                        if (!isVisible(el)) continue;
                        const buttons = el.querySelectorAll(clickableSelector);
                        if (buttons.length < 1) continue;
                        return buildInfo(el, 'class_match', (el.textContent || '').trim());
                    }

                    return { found: false };
                }
            """, _CHALLENGE_MARKER_ATTR)

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

                # Try to extract challenger username from structured fields, HTML, then text.
                username = self._extract_username(challenge_info, container_html)

                # Create a locator for the challenge container
                element = await self._find_challenge_element(container_class, challenge_info)

                return {
                    "info": challenge_info,
                    "element": element,
                    "username": username,
                    "marker": challenge_info.get("marker"),
                    "container_html": container_html,
                }

            return None

        except Exception as e:
            logger.debug("Challenge search error: %s", e)
            return None

    def _extract_username(self, challenge_info, html):
        """Extract username from structured fields, HTML, then visible text."""
        candidates = [
            challenge_info.get("username"),
            self._extract_username_from_html(html),
            self._extract_username_from_text(challenge_info.get("containerText", "")),
            self._extract_username_from_text(challenge_info.get("text", "")),
        ]

        for candidate in candidates:
            username = self._clean_username(candidate)
            if username != "unknown":
                return username

        logger.warning(
            "Could not extract challenger username from challenge text: %s",
            challenge_info.get("containerText") or challenge_info.get("text", ""),
        )
        return "unknown"

    def _extract_username_from_html(self, html):
        """Extract username from challenge notification HTML."""
        html = html or ""
        patterns = [
            r'/member/([^"\'\s<>/?#]+)',
            r'data-username=["\']([^"\']+)["\']',
            r'data-user-name=["\']([^"\']+)["\']',
            r'data-test-user=["\']([^"\']+)["\']',
            r'(?:aria-label|title)=["\']([A-Za-z0-9_-]{3,25})(?:\'s| profile| avatar|$)',
            r'alt=["\']([A-Za-z0-9_-]{3,25})(?:\'s| profile| avatar|$)',
        ]

        for pattern in patterns:
            match = re.search(pattern, html, flags=re.IGNORECASE)
            if match:
                return match.group(1)

        return "unknown"

    def _extract_username_from_text(self, text):
        """Best-effort fallback for notification text that contains the username."""
        text = " ".join((text or "").split())
        if not text:
            return "unknown"

        patterns = [
            r'\bfrom\s+([A-Za-z0-9_-]{3,25})\b',
            r'\b([A-Za-z0-9_-]{3,25})\b\s+(?:challenged|is challenging|sent)',
            r'^([A-Za-z0-9_-]{3,25})\b.*\bchallenge\b',
        ]
        skip_words = {
            "accept",
            "blitz",
            "bullet",
            "challenge",
            "challenged",
            "classical",
            "correspondence",
            "daily",
            "decline",
            "game",
            "hour",
            "hours",
            "min",
            "mins",
            "minute",
            "minutes",
            "play",
            "rapid",
            "rated",
            "sec",
            "secs",
            "second",
            "seconds",
            "unrated",
            "you",
        }

        own_username = self._clean_username(getattr(self.config, "username", ""))
        for pattern in patterns:
            match = re.search(pattern, text, flags=re.IGNORECASE)
            if not match:
                continue

            username = self._clean_username(match.group(1))
            if (
                username != "unknown" and
                username.lower() not in skip_words and
                username.lower() != own_username.lower() and
                not username.isdigit()
            ):
                return username

        return "unknown"

    @staticmethod
    def _clean_username(username):
        """Normalize extracted Chess.com usernames."""
        username = unquote((username or "").strip().strip("@"))
        match = re.match(r"^[A-Za-z0-9_-]{3,25}$", username)
        return username if match else "unknown"

    async def _find_challenge_element(self, container_class, challenge_info):
        """Create a Playwright locator for the challenge container."""
        try:
            marker = challenge_info.get("marker")
            if marker:
                loc = self.page.locator(f'[{_CHALLENGE_MARKER_ATTR}="{marker}"]')
                if await loc.count() > 0:
                    return loc.first

            # Try using the first specific class from the container
            if container_class:
                classes = container_class.split()
                for cls in classes:
                    cls = cls.strip()
                    if not cls or len(cls) < 3:
                        continue
                    selector = f'[class~="{cls}"]'
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
            challenge_el = challenge.get("element")
            marker = challenge.get("marker")

            # ── Method 1: JavaScript-based accept ──
            # Find the first green/accept button within the challenge notification
            # and click it. This is the most reliable approach.
            js_result = await self.page.evaluate(r"""
                ({ markerAttr, marker }) => {
                    const actionSelector = 'button, [role="button"], a[href]';
                    const clickableSelector = `${actionSelector}, [class*="icon"], svg`;

                    const getClass = (el) => {
                        if (!el) return '';
                        return (
                            el.getAttribute?.('class') ||
                            (typeof el.className === 'string' ? el.className : el.className?.baseVal) ||
                            ''
                        ).toString();
                    };

                    const getClickable = (el) => (
                        el?.closest?.(actionSelector) || el
                    );

                    const isVisible = (el) => {
                        if (!el) return false;
                        const rect = el.getBoundingClientRect();
                        const style = window.getComputedStyle(el);
                        return rect.width > 0 &&
                            rect.height > 0 &&
                            style.display !== 'none' &&
                            style.visibility !== 'hidden';
                    };

                    const attrsFor = (node, target) => {
                        const values = [];
                        for (const el of [node, target]) {
                            if (!el) continue;
                            values.push(
                                el.getAttribute?.('aria-label') || '',
                                el.getAttribute?.('title') || '',
                                el.getAttribute?.('data-cy') || '',
                                el.getAttribute?.('data-icon') || '',
                                getClass(el),
                                (el.textContent || '').trim()
                            );
                        }
                        return values.join(' ').toLowerCase();
                    };

                    const colorIsGreen = (value) => {
                        const match = (value || '').match(/rgba?\((\d+),\s*(\d+),\s*(\d+)/);
                        if (!match) return false;
                        const [, r, g, b] = match.map(Number);
                        return g > r * 1.3 && g > b * 1.3;
                    };

                    const describe = (node, target) => (
                        attrsFor(node, target).substring(0, 80)
                    );

                    const clickNode = (node) => {
                        const target = getClickable(node);
                        if (typeof target.click === 'function') {
                            target.click();
                        } else {
                            target.dispatchEvent(new MouseEvent('click', {
                                bubbles: true,
                                cancelable: true,
                                view: window,
                            }));
                        }
                        return target;
                    };

                    const findMarkedContainer = () => {
                        if (!marker) return null;
                        return document.querySelector(`[${markerAttr}="${marker}"]`);
                    };

                    const findTextContainer = () => {
                        const challengeTextRe = /\bchallenge\b/i;
                        const timeTextRe =
                            /\b(\d+\s*(min|mins|minute|minutes|sec|secs|second|seconds|hr|hrs|hour|hours)|daily|bullet|blitz|rapid|classical|correspondence|rated|unrated)\b/i;

                        for (const el of document.querySelectorAll('*')) {
                            if (!isVisible(el)) continue;

                            const rect = el.getBoundingClientRect();
                            if (rect.height > 250 || rect.width > 900) continue;

                            const text = (el.textContent || '').trim();
                            if (!challengeTextRe.test(text) || !timeTextRe.test(text)) continue;

                            let container = el;
                            for (let i = 0; i < 5; i++) {
                                if (!container.parentElement) break;
                                container = container.parentElement;

                                const buttons = container.querySelectorAll(actionSelector);
                                if (buttons.length >= 2 && isVisible(container)) {
                                    return container;
                                }
                            }
                        }
                        return null;
                    };

                    const container = findMarkedContainer() || findTextContainer();
                    if (!container) {
                        return { clicked: false, reason: 'no_container' };
                    }

                    const clickables = Array.from(container.querySelectorAll(clickableSelector))
                        .filter((node) => isVisible(node) || isVisible(getClickable(node)));

                    // Strategy A: accept-related attributes/classes/text.
                    for (const node of clickables) {
                        const target = getClickable(node);
                        const attrs = attrsFor(node, target);
                        if (
                            attrs.includes('accept') ||
                            attrs.includes('confirm')
                        ) {
                            clickNode(node);
                            return {
                                clicked: true,
                                method: 'aria/class match',
                                detail: describe(node, target),
                            };
                        }
                    }

                    // Strategy B: green/check/success action controls.
                    for (const node of clickables) {
                        const target = getClickable(node);
                        const attrs = attrsFor(node, target);
                        const styleTargets = [node, target].filter(Boolean);
                        const isGreen = styleTargets.some((el) => {
                            const style = window.getComputedStyle(el);
                            return colorIsGreen(style.backgroundColor) || colorIsGreen(style.color);
                        });

                        if (
                            isGreen ||
                            attrs.includes('green') ||
                            attrs.includes('success') ||
                            attrs.includes('positive') ||
                            attrs.includes('check')
                        ) {
                            clickNode(node);
                            return {
                                clicked: true,
                                method: 'green/check button',
                                detail: describe(node, target),
                            };
                        }
                    }

                    // Strategy C: click the first real action control in the container.
                    const buttons = Array.from(container.querySelectorAll(actionSelector))
                        .filter(isVisible);
                    if (buttons.length >= 2) {
                        buttons[0].click();
                        return {
                            clicked: true,
                            method: 'first button (positional)',
                            detail: getClass(buttons[0]).substring(0, 50),
                        };
                    }
                    if (buttons.length === 1) {
                        buttons[0].click();
                        return {
                            clicked: true,
                            method: 'only button',
                            detail: getClass(buttons[0]).substring(0, 50),
                        };
                    }

                    // Strategy D: icon-only controls where SVG is nested inside the button.
                    const svgTargets = Array.from(container.querySelectorAll('svg'))
                        .map(getClickable)
                        .filter((el, index, arr) => el && isVisible(el) && arr.indexOf(el) === index);
                    if (svgTargets.length >= 2) {
                        clickNode(svgTargets[0]);
                        return {
                            clicked: true,
                            method: 'first svg action (icon button)',
                            detail: getClass(svgTargets[0]).substring(0, 50),
                        };
                    }

                    return { clicked: false, reason: 'no_accept_button_found' };
                }
            """, {"markerAttr": _CHALLENGE_MARKER_ATTR, "marker": marker})

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
            if not challenge_el:
                logger.warning("No challenge container locator available; skipping broad accept fallback.")
                await self._debug_challenge_dom(None)
                return False

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
                    btn = challenge_el.locator(selector)
                    count = await btn.count()
                    for index in range(count):
                        candidate = btn.nth(index)
                        if await candidate.is_visible():
                            await candidate.click()
                            logger.info("Accepted via scoped Playwright selector: %s", selector)
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
            if "/game/live/" in url or "/game/daily/" in url or "/play/game/" in url:
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
            challenge_el = challenge.get("element")
            marker = challenge.get("marker")

            # Use JavaScript to find and click the decline/X button
            js_result = await self.page.evaluate(r"""
                ({ markerAttr, marker }) => {
                    const actionSelector = 'button, [role="button"], a[href]';

                    const getClass = (el) => {
                        if (!el) return '';
                        return (
                            el.getAttribute?.('class') ||
                            (typeof el.className === 'string' ? el.className : el.className?.baseVal) ||
                            ''
                        ).toString();
                    };

                    const isVisible = (el) => {
                        if (!el) return false;
                        const rect = el.getBoundingClientRect();
                        const style = window.getComputedStyle(el);
                        return rect.width > 0 &&
                            rect.height > 0 &&
                            style.display !== 'none' &&
                            style.visibility !== 'hidden';
                    };

                    const clickElement = (el) => {
                        if (typeof el.click === 'function') {
                            el.click();
                        } else {
                            el.dispatchEvent(new MouseEvent('click', {
                                bubbles: true,
                                cancelable: true,
                                view: window,
                            }));
                        }
                    };

                    const attrsFor = (el) => [
                        el.getAttribute?.('aria-label') || '',
                        el.getAttribute?.('title') || '',
                        el.getAttribute?.('data-cy') || '',
                        el.getAttribute?.('data-icon') || '',
                        getClass(el),
                        (el.textContent || '').trim(),
                    ].join(' ').toLowerCase();

                    const findMarkedContainer = () => {
                        if (!marker) return null;
                        return document.querySelector(`[${markerAttr}="${marker}"]`);
                    };

                    const findTextContainer = () => {
                        const challengeTextRe = /\bchallenge\b/i;
                        const timeTextRe =
                            /\b(\d+\s*(min|mins|minute|minutes|sec|secs|second|seconds|hr|hrs|hour|hours)|daily|bullet|blitz|rapid|classical|correspondence|rated|unrated)\b/i;

                        for (const el of document.querySelectorAll('*')) {
                            if (!isVisible(el)) continue;

                            const rect = el.getBoundingClientRect();
                            if (rect.height > 250 || rect.width > 900) continue;

                            const text = (el.textContent || '').trim();
                            if (!challengeTextRe.test(text) || !timeTextRe.test(text)) continue;

                            let container = el;
                            for (let i = 0; i < 5; i++) {
                                if (!container.parentElement) break;
                                container = container.parentElement;

                                const buttons = container.querySelectorAll(actionSelector);
                                if (buttons.length >= 2 && isVisible(container)) {
                                    return container;
                                }
                            }
                        }
                        return null;
                    };

                    const container = findMarkedContainer() || findTextContainer();
                    if (!container) return false;

                    const buttons = Array.from(container.querySelectorAll(actionSelector))
                        .filter(isVisible);

                    for (const btn of buttons) {
                        const attrs = attrsFor(btn);
                        if (
                            attrs.includes('decline') ||
                            attrs.includes('reject') ||
                            attrs.includes('close') ||
                            attrs.includes('cancel') ||
                            attrs.includes('red') ||
                            attrs.includes('negative')
                        ) {
                            clickElement(btn);
                            return true;
                        }
                    }

                    // Decline is typically the second/last action control.
                    if (buttons.length >= 2) {
                        clickElement(buttons[buttons.length - 1]);
                        return true;
                    }

                    return false;
                }
            """, {"markerAttr": _CHALLENGE_MARKER_ATTR, "marker": marker})

            if js_result:
                logger.info("Declined challenge")
                return True

            if not challenge_el:
                return False

            decline_selectors = [
                '[aria-label*="decline" i]',
                '[aria-label*="reject" i]',
                '[aria-label*="close" i]',
                '[aria-label*="cancel" i]',
                '[title*="Decline"]',
                '[title*="Reject"]',
                '[data-cy*="decline" i]',
                'button[class*="decline"]',
                'button[class*="reject"]',
                'button[class*="red"]',
                'button[class*="negative"]',
            ]

            for selector in decline_selectors:
                try:
                    btn = challenge_el.locator(selector)
                    count = await btn.count()
                    for index in range(count):
                        candidate = btn.nth(index)
                        if await candidate.is_visible():
                            await candidate.click()
                            logger.info("Declined via scoped Playwright selector: %s", selector)
                            return True
                except Exception:
                    continue

            try:
                buttons = challenge_el.locator('button, [role="button"]')
                count = await buttons.count()
                if count >= 2:
                    await buttons.nth(count - 1).click()
                    logger.info("Declined via scoped positional fallback")
                    return True
            except Exception:
                pass

            return False

        except Exception as e:
            logger.warning("Failed to decline challenge: %s", e)
            return False
