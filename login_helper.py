"""
Chess.com Manual Login Helper

Opens a VISIBLE browser window on your local machine so you can:
1. Solve any CAPTCHA
2. Login manually
3. Save session cookies for the VPS bot

Usage:
    python login_helper.py

After login, the script saves session_cookies.json.
Upload it to the VPS:
    scp session_cookies.json root@YOUR_VPS:/home/bot/chess.com_bot_host/
"""

import asyncio
import os
import sys

# Try to import playwright
try:
    from playwright.async_api import async_playwright
except ImportError:
    print("Playwright not installed. Installing...")
    os.system(f"{sys.executable} -m pip install playwright")
    os.system(f"{sys.executable} -m playwright install chromium")
    from playwright.async_api import async_playwright


CHESS_COM_LOGIN = "https://www.chess.com/login"
CHESS_COM_HOME = "https://www.chess.com"
OUTPUT_FILE = "session_cookies.json"


async def main():
    print("=" * 60)
    print("  Chess.com Manual Login Helper")
    print("=" * 60)
    print()
    print("A browser window will open.")
    print("Please login to chess.com manually (solve any CAPTCHA).")
    print("Once you see the chess.com homepage, press Enter here.")
    print()

    async with async_playwright() as p:
        # Launch VISIBLE browser (not headless)
        browser = await p.chromium.launch(
            headless=False,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--disable-infobars",
            ],
        )

        context = await browser.new_context(
            viewport={"width": 1280, "height": 800},
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
            ),
            locale="en-US",
            timezone_id="America/New_York",
            color_scheme="dark",
        )

        # Inject stealth script
        await context.add_init_script("""
            Object.defineProperty(navigator, 'webdriver', { get: () => false });
            if (window.chrome) { window.chrome.runtime = undefined; }
            delete window.__playwright;
            delete window.__pw_manual;
        """)

        page = await context.new_page()

        print("Opening chess.com login page...")
        await page.goto(CHESS_COM_LOGIN, wait_until="domcontentloaded")

        print()
        print(">>> Browser is open. Login to chess.com now! <<<")
        print(">>> Solve any CAPTCHA that appears. <<<")
        print()

        # Wait for user to login — poll until URL changes or user presses Enter
        input("Press ENTER here after you've logged in successfully...")

        # Verify login
        await page.goto(CHESS_COM_HOME, wait_until="domcontentloaded")
        await page.wait_for_timeout(3000)

        current_url = page.url
        title = await page.title()
        print(f"\nCurrent URL: {current_url}")
        print(f"Page title:  {title}")

        # Save storage state (cookies + localStorage)
        output_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), OUTPUT_FILE)
        await context.storage_state(path=output_path)

        print(f"\n✅ Session saved to: {output_path}")
        print(f"   File size: {os.path.getsize(output_path)} bytes")
        print()
        print("Now upload this file to the VPS:")
        print(f"   scp {OUTPUT_FILE} root@YOUR_VPS_IP:/home/bot/chess.com_bot_host/")
        print()
        print("Then restart the bot:")
        print("   systemctl restart chess-bot")

        await browser.close()


if __name__ == "__main__":
    asyncio.run(main())
