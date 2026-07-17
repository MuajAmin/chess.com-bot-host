"""
Telegram Bot API control plane for runtime bot settings.

The controller uses long polling, accepts commands only from configured chat
IDs, and writes small runtime overrides instead of rewriting config.yaml.
"""

import asyncio
import glob
import html
import logging
import os
import shlex

import yaml

try:
    import httpx
    _HTTPX_AVAILABLE = True
except ImportError:
    httpx = None
    _HTTPX_AVAILABLE = False


logger = logging.getLogger(__name__)

WEIGHT_EXTENSIONS = (".pb.gz", ".pb", ".onnx")


class CommandError(ValueError):
    """User-facing command validation error."""


class TelegramController:
    """Poll Telegram for authorized runtime-control commands."""

    def __init__(self, config):
        self.config = config
        self._token = config.telegram_control_token
        self._allowed_chat_ids = set(config.telegram_control_allowed_chat_ids)
        self._enabled = (
            config.telegram_control_enabled
            and bool(self._token)
            and bool(self._allowed_chat_ids)
            and _HTTPX_AVAILABLE
        )
        self._client = None
        self._task = None
        self._stop_event = None
        self._offset = None

        if config.telegram_control_enabled and not _HTTPX_AVAILABLE:
            logger.warning("Telegram control disabled: httpx is not installed.")
        elif config.telegram_control_enabled and not self._token:
            logger.warning("Telegram control disabled: missing bot token.")
        elif config.telegram_control_enabled and not self._allowed_chat_ids:
            logger.warning("Telegram control disabled: missing authorized chat_id.")

    async def start(self):
        if not self._enabled:
            return

        self._client = httpx.AsyncClient(
            base_url=f"https://api.telegram.org/bot{self._token}",
            timeout=30.0,
        )
        self._stop_event = asyncio.Event()
        await self._discard_pending_updates()
        self._task = asyncio.create_task(self._poll_loop(), name="telegram-control")
        logger.info(
            "Telegram control enabled for %d authorized chat(s).",
            len(self._allowed_chat_ids),
        )

    async def close(self):
        if self._stop_event:
            self._stop_event.set()
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        if self._client:
            await self._client.aclose()
            self._client = None

    async def _poll_loop(self):
        while not self._stop_event.is_set():
            try:
                await self._poll_once(timeout=20)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.warning("Telegram control polling failed: %s", e)
                await asyncio.sleep(max(1.0, self.config.telegram_control_poll_interval))

    async def _discard_pending_updates(self):
        try:
            updates = await self._api_get(
                "getUpdates",
                {"timeout": 0, "limit": 100, "allowed_updates": '["message"]'},
            )
        except Exception as e:
            logger.warning("Could not prime Telegram control offset: %s", e)
            return

        if updates:
            self._offset = max(int(update["update_id"]) for update in updates) + 1
            logger.info("Telegram control skipped %d old update(s).", len(updates))

    async def _poll_once(self, timeout=20):
        params = {
            "timeout": timeout,
            "limit": 25,
            "allowed_updates": '["message"]',
        }
        if self._offset is not None:
            params["offset"] = self._offset

        updates = await self._api_get("getUpdates", params)
        for update in updates:
            update_id = int(update.get("update_id", 0))
            self._offset = max(self._offset or 0, update_id + 1)
            await self._handle_update(update)

    async def _api_get(self, method, params):
        response = await self._client.get(method, params=params)
        data = response.json()
        if response.status_code >= 400 or not data.get("ok"):
            description = data.get("description") if isinstance(data, dict) else response.text
            raise RuntimeError(f"{method} failed: {description}")
        return data.get("result", [])

    async def _api_post(self, method, payload):
        response = await self._client.post(method, json=payload)
        data = response.json()
        if response.status_code >= 400 or not data.get("ok"):
            description = data.get("description") if isinstance(data, dict) else response.text
            raise RuntimeError(f"{method} failed: {description}")
        return data.get("result")

    async def _handle_update(self, update):
        message = update.get("message") or {}
        text = str(message.get("text") or "").strip()
        chat = message.get("chat") or {}
        chat_id = str(chat.get("id") or "").strip()
        if not text or not chat_id:
            return

        if chat_id not in self._allowed_chat_ids:
            logger.warning("Ignoring Telegram command from unauthorized chat_id=%s", chat_id)
            return

        try:
            reply = await self._dispatch(text)
        except CommandError as e:
            reply = f"Error: {html.escape(str(e), quote=False)}"
        except Exception as e:
            logger.exception("Telegram command failed: %s", text)
            reply = f"Command failed: {html.escape(str(e), quote=False)}"

        if reply:
            await self._send_message(chat_id, reply)

    async def _send_message(self, chat_id, text):
        text = text[:3900]
        await self._api_post(
            "sendMessage",
            {
                "chat_id": chat_id,
                "text": text,
                "parse_mode": "HTML",
                "disable_web_page_preview": True,
            },
        )

    async def _dispatch(self, text):
        args = self._split_command(text)
        if not args:
            return ""

        command = args[0].lower()
        if "@" in command:
            command = command.split("@", 1)[0]
        rest = args[1:]

        if command in ("/start", "/help"):
            return self._help_text()
        if command == "/status":
            return self._status_text()
        if command == "/weights":
            return self._weights_text()
        if command in ("/setweights", "/useweights"):
            return self._set_weights(rest)
        if command == "/strength":
            return self._set_strength(rest)
        if command == "/setengine":
            return self._set_engine(rest)
        if command == "/settime":
            return self._set_time(rest)
        if command == "/setthreads":
            return self._set_threads(rest)
        if command == "/setcache":
            return self._set_cache(rest)
        if command == "/delay":
            return self._set_delay(rest)
        if command == "/blunder":
            return self._set_blunder(rest)
        if command == "/challenge":
            return self._set_challenge(rest)
        if command == "/maxgames":
            return self._set_max_games(rest)
        if command == "/pause":
            self._update_overrides({"server.paused": True})
            return "Challenge listener paused. Current game, if any, can finish."
        if command == "/resume":
            self._update_overrides({"server.paused": False})
            return "Challenge listener resumed."
        if command == "/reload":
            changed = self.config.reload()
            return "Config reloaded." if changed else "Config already current."
        if command == "/clearoverrides":
            return self._clear_overrides()

        raise CommandError("Unknown command. Send /help for commands.")

    @staticmethod
    def _split_command(text):
        try:
            return shlex.split(text)
        except ValueError:
            return text.split()

    def _help_text(self):
        return (
            "<b>Chess Bot Control</b>\n"
            "/status - show current settings\n"
            "/weights - list weight files\n"
            "/setweights &lt;file-or-path&gt; [auto|maia|lc0]\n"
            "/strength &lt;1100-1900|fast|normal|strong&gt;\n"
            "/setengine &lt;auto|maia|lc0&gt;\n"
            "/settime &lt;seconds&gt;\n"
            "/setthreads &lt;count&gt;\n"
            "/setcache &lt;size&gt;\n"
            "/delay &lt;min&gt; &lt;max&gt;\n"
            "/blunder &lt;0-100&gt;\n"
            "/challenge &lt;open|whitelist&gt;\n"
            "/maxgames &lt;count&gt;\n"
            "/pause, /resume, /reload, /clearoverrides"
        )

    def _status_text(self):
        cfg = self.config
        weights = cfg.engine_weights
        weights_name = os.path.basename(weights) or weights
        mode = "maia" if self._is_maia_config() else "lc0"
        change_moves = "on" if cfg.humanizer_change_moves else "off"
        paused = "yes" if cfg.server_paused else "no"
        apply_live = "yes" if cfg.telegram_control_apply_during_game else "no"
        return (
            "<b>Chess Bot Status</b>\n"
            f"User: <code>{self._e(cfg.username or 'cookie_only')}</code>\n"
            f"Paused: <b>{paused}</b>\n"
            f"Engine: <b>{self._e(cfg.engine_type)}</b> (runtime {mode})\n"
            f"Weights: <code>{self._e(weights_name)}</code>\n"
            f"Move time: <b>{self._e(cfg.engine_time_per_move)}s</b>\n"
            f"Threads/cache: <b>{self._e(cfg.engine_threads)}</b> / "
            f"<b>{self._e(cfg.engine_nn_cache_size)}</b>\n"
            f"Delay: <b>{cfg.timing_delay_min:.2f}-{cfg.timing_delay_max:.2f}s</b>\n"
            f"Move changes: <b>{change_moves}</b>, "
            f"blunder <b>{cfg.humanizer_blunder_chance * 100:.1f}%</b>\n"
            f"Challenge: <b>{self._e(cfg.challenge_mode)}</b>, "
            f"max/day <b>{self._e(cfg.max_games_per_day)}</b>\n"
            f"Apply during game: <b>{apply_live}</b>\n"
            f"Overrides: <code>{self._e(cfg.telegram_control_overrides_file)}</code>"
        )

    def _weights_text(self):
        files = self._find_weight_files()
        weights_dir = self.config.telegram_control_weights_dir
        if not files:
            return (
                "No weight files found in "
                f"<code>{self._e(weights_dir)}</code>."
            )

        names = [os.path.basename(path) for path in files[:40]]
        suffix = "" if len(files) <= 40 else f"\n...and {len(files) - 40} more"
        return (
            f"<b>Weights in {self._e(weights_dir)}</b>\n"
            + "\n".join(f"<code>{self._e(name)}</code>" for name in names)
            + suffix
        )

    def _set_weights(self, args):
        if not args:
            raise CommandError("Usage: /setweights <file-or-path> [auto|maia|lc0]")

        path = self._normalize_weight_path(args[0])
        engine_type = "auto"
        if len(args) >= 2:
            engine_type = self._normalize_engine_type(args[1])

        self._update_overrides(
            {
                "engine.weights": path,
                "engine.type": engine_type,
            }
        )
        return (
            "Weights updated.\n"
            f"Type: <b>{self._e(engine_type)}</b>\n"
            f"File: <code>{self._e(path)}</code>\n"
            "Engine will restart before the next move/game when live apply is enabled."
        )

    def _set_strength(self, args):
        if not args:
            raise CommandError("Usage: /strength <1100-1900|fast|normal|strong>")

        value = args[0].lower()
        profiles = {
            "fast": 0.3,
            "normal": 1.5,
            "strong": 5.0,
        }
        if value in profiles:
            seconds = profiles[value]
            self._update_overrides(
                {
                    "engine.type": "lc0",
                    "engine.time_per_move": seconds,
                }
            )
            return f"Strength profile set to <b>{value}</b> ({seconds}s search)."

        try:
            rating = int(value)
        except ValueError:
            raise CommandError("Strength must be a Maia rating or fast/normal/strong.")

        if not 1100 <= rating <= 1900:
            raise CommandError("Maia rating must be between 1100 and 1900.")

        path = self._find_maia_weight_for_rating(rating)
        if not path:
            raise CommandError(
                f"No Maia weight for {rating} found in {self.config.telegram_control_weights_dir}."
            )

        self._update_overrides(
            {
                "engine.type": "maia",
                "engine.weights": path,
                "humanizer.rating_mimic": rating,
            }
        )
        return (
            f"Maia strength set to <b>{rating}</b>.\n"
            f"File: <code>{self._e(path)}</code>"
        )

    def _set_engine(self, args):
        if not args:
            raise CommandError("Usage: /setengine <auto|maia|lc0>")
        engine_type = self._normalize_engine_type(args[0])
        self._update_overrides({"engine.type": engine_type})
        return f"Engine type set to <b>{self._e(engine_type)}</b>."

    def _set_time(self, args):
        if not args:
            raise CommandError("Usage: /settime <seconds>")
        seconds = self._parse_float(args[0], "seconds", 0.05, 60.0)
        self._update_overrides({"engine.time_per_move": seconds})
        return f"Engine search time set to <b>{seconds:g}s</b>."

    def _set_threads(self, args):
        if not args:
            raise CommandError("Usage: /setthreads <count>")
        threads = self._parse_int(args[0], "threads", 1, 64)
        self._update_overrides({"engine.threads": threads})
        return f"Engine threads set to <b>{threads}</b>."

    def _set_cache(self, args):
        if not args:
            raise CommandError("Usage: /setcache <size>")
        cache_size = self._parse_int(args[0], "cache size", 1, 10000000)
        self._update_overrides({"engine.nn_cache_size": cache_size})
        return f"Engine NN cache size set to <b>{cache_size}</b>."

    def _set_delay(self, args):
        if len(args) < 2:
            raise CommandError("Usage: /delay <min> <max>")
        min_delay = self._parse_float(args[0], "min delay", 0.0, 30.0)
        max_delay = self._parse_float(args[1], "max delay", min_delay, 60.0)
        self._update_overrides(
            {
                "timing.delay_min": min_delay,
                "timing.delay_max": max_delay,
            }
        )
        return f"Move delay set to <b>{min_delay:g}-{max_delay:g}s</b>."

    def _set_blunder(self, args):
        if not args:
            raise CommandError("Usage: /blunder <0-100>")
        percent = self._parse_float(args[0], "blunder percent", 0.0, 100.0)
        self._update_overrides(
            {
                "humanizer.change_moves": percent > 0,
                "humanizer.blunder_chance": percent / 100.0,
            }
        )
        state = "enabled" if percent > 0 else "disabled"
        return f"Move-changing humanizer {state}; blunder chance <b>{percent:g}%</b>."

    def _set_challenge(self, args):
        if not args:
            raise CommandError("Usage: /challenge <open|whitelist>")
        mode = args[0].lower()
        if mode not in ("open", "whitelist"):
            raise CommandError("Challenge mode must be open or whitelist.")
        self._update_overrides({"challenge.mode": mode})
        return f"Challenge mode set to <b>{mode}</b>."

    def _set_max_games(self, args):
        if not args:
            raise CommandError("Usage: /maxgames <count>")
        count = self._parse_int(args[0], "max games", 1, 500)
        self._update_overrides({"server.max_games_per_day": count})
        return f"Max games per day set to <b>{count}</b>."

    def _clear_overrides(self):
        path = self.config.telegram_control_overrides_file
        try:
            os.remove(path)
        except FileNotFoundError:
            pass
        self.config.reload()
        return "Runtime overrides cleared. Base config is active."

    def _update_overrides(self, updates):
        path = self.config.telegram_control_overrides_file
        data = self._read_overrides(path)
        for dotted_path, value in updates.items():
            self._set_nested(data, dotted_path.split("."), value)
        self._write_overrides(path, data)
        self.config.reload()

    @staticmethod
    def _read_overrides(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = yaml.safe_load(f) or {}
        except FileNotFoundError:
            return {}

        if not isinstance(data, dict):
            raise CommandError("Runtime override file is not a YAML mapping.")
        return data

    @staticmethod
    def _write_overrides(path, data):
        directory = os.path.dirname(path)
        if directory:
            os.makedirs(directory, exist_ok=True)
        temp_path = f"{path}.tmp"
        with open(temp_path, "w", encoding="utf-8") as f:
            yaml.safe_dump(data, f, sort_keys=False)
        os.replace(temp_path, path)

    @staticmethod
    def _set_nested(data, path_parts, value):
        current = data
        for part in path_parts[:-1]:
            child = current.get(part)
            if not isinstance(child, dict):
                child = {}
                current[part] = child
            current = child
        current[path_parts[-1]] = value

    def _normalize_weight_path(self, value):
        value = os.path.expanduser(os.path.expandvars(str(value).strip()))
        if not value:
            raise CommandError("Weight file path is empty.")

        if os.path.isabs(value):
            path = value
        else:
            path = os.path.join(self.config.telegram_control_weights_dir, value)
        path = os.path.abspath(path)

        lower_path = path.lower()
        if not lower_path.endswith(WEIGHT_EXTENSIONS):
            raise CommandError("Weight file must end with .pb.gz, .pb, or .onnx.")
        if not os.path.exists(path):
            raise CommandError(f"Weight file does not exist: {path}")
        if not os.path.isfile(path):
            raise CommandError(f"Weight path is not a file: {path}")
        return path

    def _find_weight_files(self):
        weights_dir = self.config.telegram_control_weights_dir
        if not os.path.isdir(weights_dir):
            return []
        files = []
        for pattern in ("*.pb.gz", "*.pb", "*.onnx"):
            files.extend(glob.glob(os.path.join(weights_dir, pattern)))
        return sorted(set(files), key=lambda path: os.path.basename(path).lower())

    def _find_maia_weight_for_rating(self, rating):
        rating_text = str(rating)
        candidates = [
            path
            for path in self._find_weight_files()
            if rating_text in os.path.basename(path).lower()
            and "maia" in os.path.basename(path).lower()
        ]
        if not candidates:
            return None
        return sorted(candidates, key=lambda path: (len(os.path.basename(path)), path))[0]

    def _is_maia_config(self):
        engine_type = self.config.engine_type
        if engine_type == "maia":
            return True
        if engine_type == "lc0":
            return False
        return "maia" in str(self.config.engine_weights).lower()

    @staticmethod
    def _normalize_engine_type(value):
        engine_type = str(value).strip().lower()
        if engine_type not in ("auto", "maia", "lc0"):
            raise CommandError("Engine type must be auto, maia, or lc0.")
        return engine_type

    @staticmethod
    def _parse_float(value, label, min_value, max_value):
        try:
            parsed = float(value)
        except (TypeError, ValueError):
            raise CommandError(f"{label} must be a number.")
        if parsed < min_value or parsed > max_value:
            raise CommandError(f"{label} must be between {min_value:g} and {max_value:g}.")
        return parsed

    @staticmethod
    def _parse_int(value, label, min_value, max_value):
        try:
            parsed = int(value)
        except (TypeError, ValueError):
            raise CommandError(f"{label} must be an integer.")
        if parsed < min_value or parsed > max_value:
            raise CommandError(f"{label} must be between {min_value} and {max_value}.")
        return parsed

    @staticmethod
    def _e(value):
        return html.escape(str(value), quote=False)
