"""
Configuration loader for chess.com Lc0 bot.
Loads settings from config.yaml with defaults and validation.
"""

import os
import sys
import logging
import copy
import urllib.parse as urlparse
import yaml

logger = logging.getLogger(__name__)

# Default configuration values
DEFAULTS = {
    "account": {
        "username": "",
        "password": "",
        "login_mode": "auto",  # cookie_only | credentials | auto
    },
    "challenge": {
        "mode": "whitelist",
        "allowed_users": [],
    },
    "engine": {
        "type": "auto",  # maia | lc0 | auto (auto-detect from weights filename)
        "path": "/usr/local/bin/lc0",
        "weights": "",
        "backend": "blas",
        "threads": 1,
        "nn_cache_size": 10000,
        "time_per_move": 1.5,
    },
    "timing": {
        "enabled": True,
        "delay_min": 0.3,
        "delay_max": 1.5,
        "opening_delay_max": 0.8,
        "forced_delay_max": 0.22,
        "critical_delay_max": 4.5,
        "premove_chance": 0.05,
    },
    "humanizer": {
        # Legacy/optional move-changing settings. Keep disabled when the
        # engine move should stay exactly the same.
        "enabled": False,
        "delay_min": 0.3,
        "delay_max": 1.5,
        "blunder_chance": 0.0,
        "premove_chance": 0.05,
        "rating_mimic": 1800,
        "change_moves": False,
        "adjust_engine_time": False,
    },
    "notifications": {
        "webhook_url": "",  # Telegram/Discord webhook URL (empty = disabled)
    },
    "control": {
        "telegram": {
            "enabled": False,
            "token": "",
            "chat_id": "",
            "allowed_chat_ids": [],
            "poll_interval": 2.0,
            "overrides_file": "runtime_control.yaml",
            "weights_dir": "/home/bot/chess.com_bot_host/weights",
            "apply_during_game": True,
        },
    },
    "server": {
        "paused": False,
        "check_interval": 3,
        "max_games_per_day": 5,
        "cookie_file": "session_cookies.json",
        "headless": True,
        "log_level": "INFO",
        "max_context_games": 3,  # Recreate browser context every N games
        "worker_timeout_seconds": 7200,
        "browser_no_sandbox": False,
        "blocked_resource_types": ["image", "media", "font"],
        "challenge_broad_scan_interval": 5,
        "memory_log_interval_games": 1,
    },
}


class Config:
    """Bot configuration loaded from YAML file."""

    def __init__(self, config_path="config.yaml"):
        self.config_path = config_path
        self._data = {}
        self._load()
        self._validate()
        self._setup_logging()

    def _load(self):
        """Load config from YAML file, merging with defaults."""
        if not os.path.exists(self.config_path):
            print(f"ERROR: Config file not found: {self.config_path}")
            print(f"Copy config.yaml.example to config.yaml and fill in your values.")
            sys.exit(1)

        user_config = self._load_yaml_file(self.config_path, fatal=True)

        # Deep merge: defaults <- user config
        merged = self._expand_env_values(self._deep_merge(DEFAULTS, user_config))
        overrides_path = self._resolve_config_relative_path(
            merged.get("control", {})
            .get("telegram", {})
            .get("overrides_file", "runtime_control.yaml")
        )
        runtime_overrides = self._load_yaml_file(overrides_path, fatal=False)
        if runtime_overrides:
            merged = self._expand_env_values(self._deep_merge(merged, runtime_overrides))

        self._data = merged
        self._source_mtimes = self._current_source_mtimes(overrides_path)
        self._apply_legacy_humanizer_timing(user_config)

    def _load_yaml_file(self, path, fatal=False):
        """Read a YAML file and return a dict; invalid optional files are ignored."""
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = yaml.safe_load(f) or {}
        except FileNotFoundError:
            if fatal:
                raise
            return {}
        except Exception as e:
            if fatal:
                print(f"ERROR: Could not read config file {path}: {e}")
                sys.exit(1)
            logger.warning("Ignoring runtime override file %s: %s", path, e)
            return {}

        if not isinstance(data, dict):
            if fatal:
                print(f"ERROR: Config file {path} must contain a YAML mapping.")
                sys.exit(1)
            logger.warning("Ignoring runtime override file %s: expected YAML mapping.", path)
            return {}
        return data

    def _resolve_config_relative_path(self, path):
        """Resolve relative paths against the directory containing config.yaml."""
        if not path:
            return ""
        path = os.path.expanduser(os.path.expandvars(str(path)))
        if os.path.isabs(path):
            return path
        base_dir = os.path.dirname(os.path.abspath(self.config_path)) or os.getcwd()
        return os.path.join(base_dir, path)

    @staticmethod
    def _mtime(path):
        try:
            return os.path.getmtime(path)
        except OSError:
            return None

    def _current_source_mtimes(self, overrides_path=None):
        if overrides_path is None:
            overrides_path = self.telegram_control_overrides_file
        return {
            os.path.abspath(self.config_path): self._mtime(self.config_path),
            os.path.abspath(overrides_path): self._mtime(overrides_path),
        }

    def reload(self):
        """Reload config.yaml plus runtime overrides into this Config object."""
        previous = copy.deepcopy(self._data)
        self._load()
        self._validate()
        self._setup_logging()
        return previous != self._data

    def reload_if_changed(self):
        """Reload only when config.yaml or the runtime override file changed."""
        current = self._current_source_mtimes()
        if current == getattr(self, "_source_mtimes", None):
            return False
        return self.reload()

    def _apply_legacy_humanizer_timing(self, user_config):
        """
        Support older config files that used humanizer.* for delay settings.
        New config files should use timing.* for delay-only behavior.
        """
        legacy = user_config.get("humanizer")
        explicit_timing = isinstance(user_config.get("timing"), dict)
        if not isinstance(legacy, dict) or explicit_timing:
            return

        timing = self._data.setdefault("timing", {})
        for key in ("enabled", "delay_min", "delay_max", "premove_chance"):
            if key in legacy:
                timing[key] = self._expand_env_values(legacy[key])

    def _deep_merge(self, base, override):
        """Recursively merge override dict into base dict."""
        result = copy.deepcopy(base)
        for key, value in override.items():
            if key in result and isinstance(result[key], dict) and isinstance(value, dict):
                result[key] = self._deep_merge(result[key], value)
            else:
                result[key] = value
        return result

    def _expand_env_values(self, value):
        """Expand environment variables in string config values."""
        if isinstance(value, dict):
            return {k: self._expand_env_values(v) for k, v in value.items()}
        if isinstance(value, list):
            return [self._expand_env_values(v) for v in value]
        if isinstance(value, str):
            expanded = os.path.expandvars(value)
            if expanded == value and value.startswith("${") and value.endswith("}"):
                return ""
            return expanded
        return value

    @staticmethod
    def _as_bool(value):
        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            return value.strip().lower() in ("1", "true", "yes", "on")
        return bool(value)

    @staticmethod
    def _as_positive_int(value, default):
        try:
            parsed = int(value)
        except (TypeError, ValueError):
            return default
        return parsed if parsed > 0 else default

    @staticmethod
    def _as_float(value, default):
        try:
            return float(value)
        except (TypeError, ValueError):
            return default

    def _validate(self):
        """Validate critical config values."""
        errors = []

        if self.login_mode not in ("cookie_only", "credentials", "auto"):
            errors.append(
                f"account.login_mode must be 'cookie_only', 'credentials', or 'auto', "
                f"got: {self.login_mode}"
            )
        elif self.login_mode != "cookie_only":
            if not self.username:
                errors.append("account.username is required unless login_mode is 'cookie_only'")
            if not self.password:
                errors.append("account.password is required unless login_mode is 'cookie_only'")

        if not self.engine_weights:
            errors.append("engine.weights path is required")
        if self.engine_type not in ("lc0", "maia", "auto"):
            errors.append(
                f"engine.type must be 'lc0', 'maia', or 'auto', got: {self.engine_type}"
            )
        if self.challenge_mode not in ("whitelist", "open"):
            errors.append(f"challenge.mode must be 'whitelist' or 'open', got: {self.challenge_mode}")
        if self.timing_delay_min > self.timing_delay_max:
            errors.append("timing.delay_min must be <= timing.delay_max")
        if self.timing_opening_delay_max < 0:
            errors.append("timing.opening_delay_max must be >= 0")
        if self.timing_forced_delay_max <= 0:
            errors.append("timing.forced_delay_max must be > 0")
        if self.timing_critical_delay_max <= 0:
            errors.append("timing.critical_delay_max must be > 0")
        if not 0 <= self.timing_premove_chance <= 1:
            errors.append("timing.premove_chance must be between 0 and 1")
        if not 0 <= self.humanizer_blunder_chance <= 1:
            errors.append("humanizer.blunder_chance must be between 0 and 1")
        if self.worker_timeout_seconds <= 0:
            errors.append("server.worker_timeout_seconds must be > 0")

        if errors:
            print("CONFIG ERRORS:")
            for e in errors:
                print(f"  - {e}")
            sys.exit(1)

        # Warnings (non-fatal)
        if not os.path.exists(self.engine_path):
            logger.warning(f"Engine binary not found at: {self.engine_path}")
        if not os.path.exists(self.engine_weights):
            logger.warning(f"Weights file not found at: {self.engine_weights}")

    def _setup_logging(self):
        """Configure logging based on config."""
        log_level = getattr(logging, self.log_level.upper(), logging.INFO)
        logging.basicConfig(
            level=log_level,
            format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )
        logging.getLogger().setLevel(log_level)

    # --- Account ---
    @property
    def username(self):
        return self._data["account"]["username"]

    @property
    def password(self):
        return self._data["account"]["password"]

    @property
    def login_mode(self):
        return self._data["account"].get("login_mode", "auto")

    # --- Challenge ---
    @property
    def challenge_mode(self):
        return self._data["challenge"]["mode"]

    @property
    def allowed_users(self):
        return self._data["challenge"]["allowed_users"]

    # --- Engine ---
    @property
    def engine_type(self):
        return self._data["engine"]["type"]

    @property
    def engine_path(self):
        return self._data["engine"]["path"]

    @property
    def engine_weights(self):
        return self._data["engine"]["weights"]

    @property
    def engine_backend(self):
        return self._data["engine"]["backend"]

    @property
    def engine_threads(self):
        return self._data["engine"]["threads"]

    @property
    def engine_nn_cache_size(self):
        return self._data["engine"]["nn_cache_size"]

    @property
    def engine_time_per_move(self):
        return self._data["engine"]["time_per_move"]

    @property
    def engine_process_signature(self):
        return (
            self.engine_type,
            self.engine_path,
            self.engine_weights,
            self.engine_backend,
            self.engine_threads,
            self.engine_nn_cache_size,
        )

    # --- Timing ---
    @property
    def timing_enabled(self):
        return self._as_bool(self._data["timing"].get("enabled", True))

    @property
    def timing_delay_min(self):
        return self._as_float(self._data["timing"].get("delay_min", 0.3), 0.3)

    @property
    def timing_delay_max(self):
        return self._as_float(self._data["timing"].get("delay_max", 1.5), 1.5)

    @property
    def timing_opening_delay_max(self):
        return self._as_float(self._data["timing"].get("opening_delay_max", 0.8), 0.8)

    @property
    def timing_forced_delay_max(self):
        return self._as_float(self._data["timing"].get("forced_delay_max", 0.22), 0.22)

    @property
    def timing_critical_delay_max(self):
        return self._as_float(self._data["timing"].get("critical_delay_max", 4.5), 4.5)

    @property
    def timing_premove_chance(self):
        return self._as_float(self._data["timing"].get("premove_chance", 0.05), 0.05)

    # --- Humanizer / legacy compatibility ---
    @property
    def humanizer_enabled(self):
        return self.timing_enabled

    @property
    def humanizer_delay_min(self):
        return self.timing_delay_min

    @property
    def humanizer_delay_max(self):
        return self.timing_delay_max

    @property
    def humanizer_blunder_chance(self):
        return self._as_float(self._data["humanizer"].get("blunder_chance", 0.0), 0.0)

    @property
    def humanizer_premove_chance(self):
        return self.timing_premove_chance

    @property
    def humanizer_rating_mimic(self):
        return self._data["humanizer"].get("rating_mimic", 1800)

    @property
    def humanizer_change_moves(self):
        return self._as_bool(self._data["humanizer"].get("change_moves", False))

    @property
    def humanizer_adjust_engine_time(self):
        return self._as_bool(self._data["humanizer"].get("adjust_engine_time", False))

    # --- Server ---
    @property
    def server_paused(self):
        return self._as_bool(self._data["server"].get("paused", False))

    @property
    def check_interval(self):
        return self._data["server"]["check_interval"]

    @property
    def max_games_per_day(self):
        return self._data["server"]["max_games_per_day"]

    @property
    def cookie_file(self):
        return self._data["server"]["cookie_file"]

    @property
    def headless(self):
        return self._data["server"]["headless"]

    @property
    def log_level(self):
        return self._data["server"]["log_level"]

    @property
    def max_context_games(self):
        return self._data["server"].get("max_context_games", 3)

    @property
    def worker_timeout_seconds(self):
        try:
            return int(self._data["server"].get("worker_timeout_seconds", 7200))
        except (TypeError, ValueError):
            return 0

    @property
    def browser_no_sandbox(self):
        return self._as_bool(self._data["server"].get("browser_no_sandbox", False))

    @property
    def blocked_resource_types(self):
        values = self._data["server"].get("blocked_resource_types", [])
        if not isinstance(values, list):
            return set()
        allowed = {"image", "media", "font"}
        return {str(value).lower() for value in values if str(value).lower() in allowed}

    @property
    def challenge_broad_scan_interval(self):
        return self._as_positive_int(
            self._data["server"].get("challenge_broad_scan_interval", 5),
            5,
        )

    @property
    def memory_log_interval_games(self):
        return self._as_positive_int(
            self._data["server"].get("memory_log_interval_games", 1),
            1,
        )

    # --- Notifications ---
    @property
    def webhook_url(self):
        return self._data.get("notifications", {}).get("webhook_url", "")

    # --- Telegram Control ---
    @property
    def telegram_control_enabled(self):
        return self._as_bool(
            self._data.get("control", {}).get("telegram", {}).get("enabled", False)
        )

    @property
    def telegram_control_token(self):
        configured = self._data.get("control", {}).get("telegram", {}).get("token", "")
        token = os.environ.get("TELEGRAM_BOT_TOKEN") or configured
        if token:
            return str(token).strip()

        webhook_token, _ = self._telegram_credentials_from_url(
            os.environ.get("BOT_WEBHOOK_URL")
            or os.environ.get("TELEGRAM_WEBHOOK_URL")
            or self.webhook_url
        )
        return webhook_token or ""

    @property
    def telegram_control_chat_id(self):
        configured = self._data.get("control", {}).get("telegram", {}).get("chat_id", "")
        chat_id = os.environ.get("TELEGRAM_CHAT_ID") or configured
        if chat_id:
            return str(chat_id).strip()

        _, webhook_chat_id = self._telegram_credentials_from_url(
            os.environ.get("BOT_WEBHOOK_URL")
            or os.environ.get("TELEGRAM_WEBHOOK_URL")
            or self.webhook_url
        )
        return webhook_chat_id or ""

    @property
    def telegram_control_allowed_chat_ids(self):
        telegram = self._data.get("control", {}).get("telegram", {})
        values = telegram.get("allowed_chat_ids", [])
        if isinstance(values, (str, int)):
            values = [values]
        allowed = {str(value).strip() for value in values if str(value).strip()}
        if self.telegram_control_chat_id:
            allowed.add(str(self.telegram_control_chat_id))
        return allowed

    @property
    def telegram_control_poll_interval(self):
        return max(
            0.5,
            self._as_float(
                self._data.get("control", {})
                .get("telegram", {})
                .get("poll_interval", 2.0),
                2.0,
            ),
        )

    @property
    def telegram_control_overrides_file(self):
        path = (
            self._data.get("control", {})
            .get("telegram", {})
            .get("overrides_file", "runtime_control.yaml")
        )
        return self._resolve_config_relative_path(path)

    @property
    def telegram_control_weights_dir(self):
        path = (
            self._data.get("control", {})
            .get("telegram", {})
            .get("weights_dir", "/home/bot/chess.com_bot_host/weights")
        )
        return os.path.expanduser(os.path.expandvars(str(path)))

    @property
    def telegram_control_apply_during_game(self):
        return self._as_bool(
            self._data.get("control", {})
            .get("telegram", {})
            .get("apply_during_game", True)
        )

    @staticmethod
    def _telegram_credentials_from_url(raw_url):
        if not raw_url:
            return "", ""
        raw_url = str(raw_url).strip()

        if raw_url.startswith("telegram://"):
            token_chat = raw_url[11:]
            if "/" not in token_chat:
                return "", ""
            token, chat_id = token_chat.split("/", 1)
            return token.strip(), chat_id.strip()

        if "api.telegram.org" not in raw_url:
            return "", ""

        if not raw_url.startswith(("http://", "https://")):
            raw_url = "https://" + raw_url

        try:
            parsed = urlparse.urlparse(raw_url)
            parts = [part for part in parsed.path.split("/") if part]
            token = ""
            chat_id = ""
            if parts and parts[0].startswith("bot"):
                token = parts[0][3:]
            query = urlparse.parse_qs(parsed.query)
            if query.get("chat_id"):
                chat_id = query["chat_id"][0]
            elif len(parts) >= 2 and parts[1] != "sendMessage":
                chat_id = parts[1]
            return token.strip(), str(chat_id).strip()
        except Exception:
            return "", ""

    def __repr__(self):
        return (
            f"Config(user={self.username}, engine={self.engine_type}, "
            f"mode={self.challenge_mode}, headless={self.headless})"
        )
