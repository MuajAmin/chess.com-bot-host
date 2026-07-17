"""
Configuration loader for chess.com Lc0 bot.
Loads settings from config.yaml with defaults and validation.
"""

import os
import sys
import logging
import copy
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
    "server": {
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

        with open(self.config_path, "r", encoding="utf-8") as f:
            user_config = yaml.safe_load(f) or {}

        # Deep merge: defaults ← user config
        self._data = self._expand_env_values(self._deep_merge(DEFAULTS, user_config))
        self._apply_legacy_humanizer_timing(user_config)

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

    def __repr__(self):
        return (
            f"Config(user={self.username}, engine={self.engine_type}, "
            f"mode={self.challenge_mode}, headless={self.headless})"
        )
