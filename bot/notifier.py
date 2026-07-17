"""
Async webhook notifier for chess.com bot.
Sends game events (start, end, errors, daily limit) to a webhook URL.

Supports:
- Telegram Bot API (sendMessage endpoint)
- Discord webhooks (content field)
- Any generic webhook (JSON POST)

Uses httpx (async) — zero event loop blocking.
Silently disabled when webhook_url is not configured.
"""

import logging
import asyncio
import re
import urllib.parse as urlparse

logger = logging.getLogger(__name__)

# Try importing httpx — gracefully degrade if not installed
try:
    import httpx
    _HTTPX_AVAILABLE = True
except ImportError:
    _HTTPX_AVAILABLE = False
    logger.debug("httpx not installed — webhook notifications disabled.")


class Notifier:
    """
    Async webhook notifier for game lifecycle events.

    If webhook_url is empty or httpx is not installed, all methods
    become silent no-ops with zero overhead.
    """

    def __init__(self, config):
        raw_url = config.webhook_url.strip() if config.webhook_url else ""
        self._webhook_url = raw_url

        # Support shorthand scheme formats:
        # - telegram://TOKEN/CHAT_ID
        # - discord://ID/TOKEN
        if raw_url.startswith("telegram://"):
            token_chat = raw_url[11:]
            if "/" in token_chat:
                token, chat_id = token_chat.split("/", 1)
                self._webhook_url = f"https://api.telegram.org/bot{token}/sendMessage?chat_id={chat_id}"
            else:
                logger.warning("Notifier: Invalid telegram:// format. Expected telegram://TOKEN/CHAT_ID")
        elif raw_url.startswith("discord://"):
            token_part = raw_url[10:]
            self._webhook_url = f"https://discord.com/api/webhooks/{token_part}"

        # Clean/normalize Telegram URLs:
        # e.g., api.telegram.org/botTOKEN?chat_id=CHAT_ID -> https://api.telegram.org/botTOKEN/sendMessage?chat_id=CHAT_ID
        if "api.telegram.org" in self._webhook_url:
            if not self._webhook_url.startswith(("http://", "https://")):
                self._webhook_url = "https://" + self._webhook_url
            
            try:
                parsed = urlparse.urlparse(self._webhook_url)
                path = parsed.path
                if path.startswith("/bot"):
                    parts = [p for p in path.split('/') if p]
                    if len(parts) >= 1:
                        token_part = parts[0]
                        new_path = f"/{token_part}/sendMessage"
                        self._webhook_url = urlparse.urlunparse((
                            parsed.scheme,
                            parsed.netloc,
                            new_path,
                            parsed.params,
                            parsed.query,
                            parsed.fragment
                        ))
            except Exception as e:
                logger.debug("Failed to normalize telegram URL path: %s", e)

        self._enabled = bool(self._webhook_url) and _HTTPX_AVAILABLE
        self._username = config.username
        self._is_telegram = "api.telegram.org" in self._webhook_url if self._webhook_url else False
        self._is_discord = "discord.com/api/webhooks" in self._webhook_url if self._webhook_url else False
        self._client = httpx.AsyncClient(timeout=10.0) if self._enabled else None

        if self._enabled:
            logger.info(
                "Notifier enabled (%s): %s",
                "Telegram" if self._is_telegram else
                "Discord" if self._is_discord else "Generic webhook",
                self._webhook_url[:45] + "..." if len(self._webhook_url) > 45 else self._webhook_url
            )
            if self._is_telegram and "chat_id=" not in self._webhook_url:
                logger.warning(
                    "Notifier: Telegram webhook URL configured, but is missing '?chat_id=YOUR_CHAT_ID'. "
                    "Notifications to Telegram will fail."
                )
        else:
            if self._webhook_url and not _HTTPX_AVAILABLE:
                logger.warning(
                    "Notifier: webhook_url configured but httpx not installed. "
                    "Install with: pip install httpx"
                )

    def _format_message(self, message):
        """Converts HTML-formatted message to the correct platform markup."""
        if self._is_telegram:
            # Telegram natively supports HTML
            return message

        if self._is_discord:
            # Convert HTML tags to Discord markdown
            msg = re.sub(r'</?b>', '**', message)
            msg = re.sub(r'</?i>', '*', msg)
            msg = re.sub(r'</?u>', '__', msg)
            msg = re.sub(r'</?code>', '`', msg)
            return msg

        # Generic webhook or fallback: strip all HTML tags
        return re.sub(r'<[^>]*>', '', message)

    async def notify(self, message):
        """
        Send a notification message. Silently skips if not configured.

        Args:
            message: Text message to send
        """
        if not self._enabled:
            return

        formatted_msg = self._format_message(message)
        max_attempts = 2

        for attempt in range(1, max_attempts + 1):
            try:
                if self._is_telegram:
                    payload = {
                        "text": formatted_msg,
                        "parse_mode": "HTML",
                    }
                    if "chat_id=" not in self._webhook_url:
                        logger.warning("Telegram webhook: chat_id not in URL. Include ?chat_id=YOUR_CHAT_ID")
                        return
                    response = await self._client.post(self._webhook_url, json=payload)

                elif self._is_discord:
                    payload = {"content": formatted_msg}
                    response = await self._client.post(self._webhook_url, json=payload)

                else:
                    payload = {
                        "message": formatted_msg,
                        "bot": self._username,
                        "source": "chess.com-bot",
                    }
                    response = await self._client.post(self._webhook_url, json=payload)

                if response.status_code >= 400:
                    logger.warning(
                        "Webhook returned %d (Attempt %d/%d): %s",
                        response.status_code, attempt, max_attempts, response.text[:200],
                    )
                    if response.status_code >= 500 and attempt < max_attempts:
                        await asyncio.sleep(1)
                        continue
                else:
                    logger.debug("Webhook notification sent (%d)", response.status_code)
                    break

            except (httpx.TimeoutException, httpx.NetworkError) as e:
                logger.warning("Webhook connection error (Attempt %d/%d): %s", attempt, max_attempts, e)
                if attempt < max_attempts:
                    await asyncio.sleep(1)
                    continue
            except Exception as e:
                logger.warning("Webhook notification failed: %s", e)
                break

    async def close(self):
        """Close the reusable HTTP client."""
        if self._client:
            await self._client.aclose()
            self._client = None

    # --- Convenience methods for game lifecycle events ---

    async def game_started(self, color, opponent="unknown"):
        """Notify that a game has started."""
        emoji = "⬜" if color == "WHITE" else "⬛"
        await self.notify(
            f"🎮 <b>Game Started</b>\n"
            f"{emoji} Playing as <b>{color}</b>\n"
            f"👤 Opponent: <b>{opponent}</b>"
        )

    async def game_ended(self, result, duration_secs=None, opponent="unknown"):
        """Notify that a game has ended."""
        duration_str = f"{duration_secs:.0f}s" if duration_secs else "?"

        # Determine result emoji
        result_lower = str(result).lower()
        if "1-0" in result_lower or "won" in result_lower or "win" in result_lower:
            emoji = "🏆"
        elif "0-1" in result_lower or "lost" in result_lower or "lose" in result_lower:
            emoji = "💀"
        elif "1/2" in result_lower or "draw" in result_lower or "½" in result_lower:
            emoji = "🤝"
        else:
            emoji = "🏁"

        await self.notify(
            f"{emoji} <b>Game Ended</b>\n"
            f"📊 Result: <b>{result}</b>\n"
            f"⏱ Duration: {duration_str}\n"
            f"👤 Opponent: {opponent}"
        )

    async def error(self, message):
        """Notify about an error."""
        await self.notify(f"🚨 <b>Bot Error</b>\n{message}")

    async def daily_limit_reached(self, games_count):
        """Notify that daily game limit was reached."""
        await self.notify(
            f"🛑 <b>Daily Limit Reached</b>\n"
            f"Games played today: <b>{games_count}</b>\n"
            f"Bot sleeping until midnight."
        )
