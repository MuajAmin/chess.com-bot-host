"""
Session manager for chess.com.
Handles login, cookie/localStorage persistence, session health checks,
and headless browser stealth using Playwright.

Anti-detection features:
- WebDriver property masking
- navigator.webdriver = false
- Realistic viewport, fonts, WebGL fingerprint
- Context recreation to prevent memory leaks
"""

import json
import os
import asyncio
import logging
import socket
import urllib.request
from playwright.async_api import async_playwright, Browser, BrowserContext, Page

logger = logging.getLogger(__name__)

# JavaScript to inject for stealth — hides headless browser indicators
STEALTH_SCRIPTS = """
// Override navigator.webdriver
Object.defineProperty(navigator, 'webdriver', {
    get: () => false,
});

// Override chrome automation indicators
if (window.chrome) {
    window.chrome.runtime = undefined;
}

// Override Permissions API (headless detection)
const originalQuery = window.navigator.permissions.query;
window.navigator.permissions.query = (parameters) => (
    parameters.name === 'notifications' ?
        Promise.resolve({ state: Notification.permission }) :
        originalQuery(parameters)
);

// Override plugins (headless has 0 plugins)
Object.defineProperty(navigator, 'plugins', {
    get: () => [1, 2, 3, 4, 5],
});

// Override languages
Object.defineProperty(navigator, 'languages', {
    get: () => ['en-US', 'en'],
});

// Override platform
Object.defineProperty(navigator, 'platform', {
    get: () => 'Linux x86_64',
});

// Override hardware concurrency
Object.defineProperty(navigator, 'hardwareConcurrency', {
    get: () => 4,
});

// Override device memory
Object.defineProperty(navigator, 'deviceMemory', {
    get: () => 8,
});

// Remove automation-related properties
delete window.__playwright;
delete window.__pw_manual;
"""


class SessionManager:
    """Manages browser session with stealth and memory-conscious design."""

    CHESS_COM_URL = "https://www.chess.com"
    LOGIN_URL = "https://www.chess.com/login"

    def __init__(self, config):
        self.config = config
        self._playwright = None
        self._browser: Browser | None = None
        self._context: BrowserContext | None = None
        self._page: Page | None = None
        self._context_age = 0  # Track how many games this context has served
        self._ws_endpoint = None  # CDP endpoint for subprocess sharing
        self._cdp_port = None
        self.MAX_CONTEXT_AGE = config.max_context_games if hasattr(config, 'max_context_games') else 3

    @property
    def page(self) -> Page:
        """Get the active browser page."""
        if self._page is None:
            raise RuntimeError("Session not initialized. Call login() first.")
        return self._page

    @property
    def ws_endpoint(self) -> str:
        """
        Get the browser's CDP endpoint for subprocess sharing.

        Used to pass the CDP connection URL to subprocess workers
        via the CDP_ENDPOINT environment variable.
        """
        if self._browser is None:
            raise RuntimeError("Browser not started. Call login() first.")
        if self._ws_endpoint:
            return self._ws_endpoint
        raise RuntimeError(
            "WebSocket endpoint not available. "
            "Browser may have been launched without remote debugging."
        )

    @staticmethod
    def _find_free_local_port():
        """Reserve a currently free localhost TCP port for Chrome CDP."""
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.bind(("127.0.0.1", 0))
            return sock.getsockname()[1]

    @staticmethod
    def _fetch_cdp_version(version_url):
        with urllib.request.urlopen(version_url, timeout=1) as response:
            payload = response.read().decode("utf-8")
        return json.loads(payload)

    async def _wait_for_cdp_endpoint(self, endpoint, timeout=5):
        """Wait until Chrome exposes its CDP /json/version endpoint."""
        version_url = f"{endpoint}/json/version"
        deadline = asyncio.get_running_loop().time() + timeout
        while asyncio.get_running_loop().time() < deadline:
            try:
                await asyncio.to_thread(self._fetch_cdp_version, version_url)
                return True
            except Exception:
                await asyncio.sleep(0.1)
        return False

    @staticmethod
    def _running_as_root():
        return hasattr(os, "geteuid") and os.geteuid() == 0

    @staticmethod
    def _restrict_file_permissions(path):
        if not path or not os.path.exists(path):
            return
        try:
            os.chmod(path, 0o600)
        except OSError as e:
            logger.debug("Could not restrict permissions on %s: %s", path, e)

    async def start_browser(self):
        """Launch Playwright browser with stealth configuration."""
        logger.info("Launching browser (headless=%s)...", self.config.headless)
        self._playwright = await async_playwright().start()

        if self._running_as_root() and not self.config.browser_no_sandbox:
            await self._playwright.stop()
            self._playwright = None
            raise RuntimeError(
                "Refusing to launch Chromium as root with the sandbox enabled. "
                "Run the bot as a non-root user, or explicitly set "
                "server.browser_no_sandbox=true if you accept that risk."
            )

        self._cdp_port = self._find_free_local_port()
        self._ws_endpoint = f"http://127.0.0.1:{self._cdp_port}"
        chromium_args = [
            "--disable-dev-shm-usage",
            "--disable-gpu",
            "--disable-extensions",
            "--disable-background-networking",
            "--disable-default-apps",
            "--disable-sync",
            "--disable-translate",
            "--no-first-run",
            "--disable-blink-features=AutomationControlled",  # Hide automation
            "--disable-infobars",
            "--single-process",              # Reduce memory
            "--disable-features=site-per-process",  # Reduce memory
            "--js-flags=--max-old-space-size=256",   # Limit V8 heap
            "--remote-debugging-address=127.0.0.1",
            f"--remote-debugging-port={self._cdp_port}",
        ]
        if self.config.browser_no_sandbox:
            logger.warning(
                "Chromium sandbox disabled by config. Do not run this as root."
            )
            chromium_args.append("--no-sandbox")

        self._browser = await self._playwright.chromium.launch(
            headless=self.config.headless,
            args=chromium_args,
        )

        if await self._wait_for_cdp_endpoint(self._ws_endpoint):
            logger.info("CDP endpoint ready for subprocess sharing: %s", self._ws_endpoint)
        else:
            logger.warning(
                "CDP endpoint did not become ready; subprocess mode unavailable."
            )
            self._ws_endpoint = None

        await self._create_context()
        logger.info("Browser launched with stealth configuration.")

    async def _create_context(self):
        """Create a new browser context with stealth and optional cookie restore."""
        # Close existing context if any
        if self._context:
            try:
                await self._context.close()
            except Exception:
                pass

        # Context options
        context_opts = {
            "viewport": {"width": 1280, "height": 800},
            "user_agent": (
                "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
            ),
            "locale": "en-US",
            "timezone_id": "America/New_York",
            "color_scheme": "dark",
            "java_script_enabled": True,
        }

        # Restore cookies/localStorage if available
        if os.path.exists(self.config.cookie_file):
            try:
                self._restrict_file_permissions(self.config.cookie_file)
                context_opts["storage_state"] = self.config.cookie_file
                logger.info("Restoring session from storage state...")
            except Exception as e:
                logger.warning("Could not load storage state: %s", e)

        self._context = await self._browser.new_context(**context_opts)

        # Inject stealth scripts into every new page
        await self._context.add_init_script(STEALTH_SCRIPTS)

        self._page = await self._context.new_page()
        self._context_age = 0
        logger.debug("New browser context created with stealth scripts.")

    async def login(self):
        """Login to chess.com. Cookie restore first, credential login fallback."""
        await self.start_browser()

        # Check if saved session is still valid
        if os.path.exists(self.config.cookie_file):
            if await self._is_logged_in():
                logger.info("Session restored — already logged in.")
                return True
            logger.info("Saved session expired. Logging in with credentials...")

        return await self._credential_login()

    async def _credential_login(self):
        """Login using username and password."""
        logger.info("Navigating to login page...")
        await self._page.goto(self.LOGIN_URL, wait_until="domcontentloaded", timeout=30000)
        await self._page.wait_for_timeout(2000)

        try:
            # Fill username
            username_input = self._page.locator(
                'input[id="username"], input[name="username"], '
                'input[autocomplete="username"]'
            )
            await username_input.wait_for(state="visible", timeout=10000)
            # Type like a human (with delays between keystrokes)
            await username_input.click()
            await self._page.wait_for_timeout(200)
            await username_input.fill("")
            for char in self.config.username:
                await username_input.press(char)
                await self._page.wait_for_timeout(int(50 + 80 * __import__('random').random()))

            # Fill password
            password_input = self._page.locator(
                'input[id="password"], input[name="password"], '
                'input[type="password"]'
            )
            await password_input.wait_for(state="visible", timeout=10000)
            await password_input.click()
            await self._page.wait_for_timeout(200)
            await password_input.fill("")
            for char in self.config.password:
                await password_input.press(char)
                await self._page.wait_for_timeout(int(30 + 60 * __import__('random').random()))

            # Click login button
            login_button = self._page.locator(
                'button[id="login"], button[type="submit"], '
                'button:has-text("Log In"), button:has-text("Sign In")'
            )
            await login_button.wait_for(state="visible", timeout=10000)
            await self._page.wait_for_timeout(500)
            await login_button.click()
            logger.info("Login submitted. Waiting for redirect...")

            # Wait for redirect (away from /login)
            await self._page.wait_for_url(
                lambda url: "/login" not in url,
                timeout=20000,
            )
            await self._page.wait_for_timeout(3000)

            if await self._is_logged_in():
                logger.info("Login successful: %s", self.config.username)
                await self._save_storage_state()
                return True
            else:
                logger.error("Login failed — could not verify logged-in state.")
                return False

        except Exception as e:
            logger.error("Login failed: %s", e)
            return False

    async def _is_logged_in(self):
        """Check if currently logged in."""
        try:
            await self._page.goto(self.CHESS_COM_URL, wait_until="domcontentloaded", timeout=20000)
            await self._page.wait_for_timeout(2000)

            is_logged = await self._page.evaluate("""
                () => {
                    const isVisible = (el) => {
                        if (!el) return false;
                        const style = window.getComputedStyle(el);
                        if (style.visibility === 'hidden' || style.display === 'none') {
                            return false;
                        }
                        return !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length);
                    };

                    const loginIndicators = [
                        'a[href="/login"]',
                        'a[href*="/login"]',
                        'input[name="username"]',
                        'input[autocomplete="username"]',
                        'input[type="password"]',
                    ];
                    for (const sel of loginIndicators) {
                        const el = document.querySelector(sel);
                        if (isVisible(el)) return false;
                    }

                    const indicators = [
                        '[data-cy="user-menu"]',
                        '.user-username-component',
                        '.home-username-link',
                        '.nav-link-profile',
                        'a[href*="/member/"]',
                        '.profile-popup-component',
                        'a[href*="/settings"]',
                        'button[aria-label*="Account"]',
                        'button[aria-label*="account"]',
                    ];
                    for (const sel of indicators) {
                        const el = document.querySelector(sel);
                        if (isVisible(el)) return true;
                    }
                    return false;
                }
            """)

            return bool(is_logged)

        except Exception as e:
            logger.warning("Login check failed: %s", e)
            return False

    async def _save_storage_state(self):
        """Save cookies AND localStorage to file for session persistence."""
        try:
            await self._context.storage_state(path=self.config.cookie_file)
            self._restrict_file_permissions(self.config.cookie_file)
            logger.info("Storage state saved (cookies + localStorage).")
        except Exception as e:
            logger.warning("Failed to save storage state: %s", e)

    async def recreate_context(self):
        """
        Recreate the browser context to prevent Chromium memory leaks.
        Saves current session state, closes old context, creates new one.
        Should be called after every game or periodically.
        """
        logger.info("Recreating browser context (memory leak prevention)...")

        # Save current state before destroying context
        await self._save_storage_state()

        # Close old context (frees Chromium memory)
        if self._context:
            try:
                await self._page.close()
                await self._context.close()
            except Exception as e:
                logger.warning("Error closing old context: %s", e)

        self._page = None
        self._context = None

        # Create fresh context with saved state
        await self._create_context()

        # Verify session is still valid
        if await self._is_logged_in():
            logger.info("Context recreated successfully — session still valid.")
            return True
        else:
            logger.warning("Session lost after context recreation. Re-logging in...")
            return await self._credential_login()

    async def maybe_recreate_context(self):
        """
        Recreate context if it's been used for too many games.
        Call this after each game ends.
        """
        self._context_age += 1
        if self._context_age >= self.MAX_CONTEXT_AGE:
            logger.info(
                "Context age (%d) >= max (%d). Recreating...",
                self._context_age, self.MAX_CONTEXT_AGE,
            )
            return await self.recreate_context()
        return True

    async def refresh_session(self):
        """Refresh session — re-login if needed."""
        if not await self._is_logged_in():
            logger.warning("Session expired. Re-logging in...")
            return await self._credential_login()
        return True

    async def navigate_to(self, url, wait_until="domcontentloaded"):
        """Navigate to a URL with error handling."""
        try:
            await self._page.goto(url, wait_until=wait_until, timeout=30000)
            await self._page.wait_for_timeout(1000)
            return True
        except Exception as e:
            logger.error("Navigation to %s failed: %s", url, e)
            return False

    async def close(self):
        """Close browser and cleanup all resources."""
        try:
            if self._page:
                await self._page.close()
            if self._context:
                await self._context.close()
            if self._browser:
                await self._browser.close()
            if self._playwright:
                await self._playwright.stop()
            logger.info("Browser session closed.")
        except Exception as e:
            logger.warning("Error during browser cleanup: %s", e)
        finally:
            self._page = None
            self._context = None
            self._browser = None
            self._playwright = None
