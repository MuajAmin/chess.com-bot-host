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
import json

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
        self._webhook_url = config.webhook_url
        self._enabled = bool(self._webhook_url) and _HTTPX_AVAILABLE
        self._username = config.username
        self._is_telegram = "api.telegram.org" in self._webhook_url if self._webhook_url else False
        self._is_discord = "discord.com/api/webhooks" in self._webhook_url if self._webhook_url else False

        if self._enabled:
            logger.info(
                "Notifier enabled (%s)",
                "Telegram" if self._is_telegram else
                "Discord" if self._is_discord else "Generic webhook",
            )
        else:
            if self._webhook_url and not _HTTPX_AVAILABLE:
                logger.warning(
                    "Notifier: webhook_url configured but httpx not installed. "
                    "Install with: pip install httpx"
                )

    async def notify(self, message):
        """
        Send a notification message. Silently skips if not configured.

        Args:
            message: Text message to send
        """
        if not self._enabled:
            return

        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                if self._is_telegram:
                    # Telegram Bot API: extract chat_id from URL or use as-is
                    # Expected URL: https://api.telegram.org/bot<TOKEN>/sendMessage
                    payload = {
                        "text": message,
                        "parse_mode": "HTML",
                    }
                    # If URL already has chat_id param, use it
                    if "chat_id=" not in self._webhook_url:
                        logger.warning("Telegram webhook: chat_id not in URL. Include ?chat_id=YOUR_CHAT_ID")
                        return
                    response = await client.post(self._webhook_url, json=payload)

                elif self._is_discord:
                    # Discord webhook: POST with content field
                    payload = {"content": message}
                    response = await client.post(self._webhook_url, json=payload)

                else:
                    # Generic webhook: POST JSON with message field
                    payload = {
                        "message": message,
                        "bot": self._username,
                        "source": "chess.com-bot",
                    }
                    response = await client.post(self._webhook_url, json=payload)

                if response.status_code >= 400:
                    logger.warning(
                        "Webhook returned %d: %s",
                        response.status_code, response.text[:200],
                    )
                else:
                    logger.debug("Webhook notification sent (%d)", response.status_code)

        except httpx.TimeoutException:
            logger.warning("Webhook notification timed out")
        except Exception as e:
            # Never let notification failure crash the bot
            logger.warning("Webhook notification failed: %s", e)

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
