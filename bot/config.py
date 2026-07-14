"""
Configuration loader for chess.com Lc0 bot.
Loads settings from config.yaml with defaults and validation.
"""

import os
import sys
import logging
import yaml

logger = logging.getLogger(__name__)

# Default configuration values
DEFAULTS = {
    "account": {
        "username": "",
        "password": "",
    },
    "challenge": {
        "mode": "whitelist",
        "allowed_users": [],
    },
    "engine": {
        "type": "lc0",
        "path": "/usr/local/bin/lc0",
        "weights": "",
        "backend": "blas",
        "threads": 1,
        "nn_cache_size": 200000,
        "time_per_move": 1.5,
    },
    "humanizer": {
        "enabled": True,
        "delay_min": 0.3,
        "delay_max": 1.5,
        "blunder_chance": 0.08,
        "premove_chance": 0.05,
        "rating_mimic": 1800,
    },
    "server": {
        "check_interval": 12,
        "max_games_per_day": 5,
        "cookie_file": "session_cookies.json",
        "headless": True,
        "log_level": "INFO",
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
        self._data = self._deep_merge(DEFAULTS, user_config)

    def _deep_merge(self, base, override):
        """Recursively merge override dict into base dict."""
        result = base.copy()
        for key, value in override.items():
            if key in result and isinstance(result[key], dict) and isinstance(value, dict):
                result[key] = self._deep_merge(result[key], value)
            else:
                result[key] = value
        return result

    def _validate(self):
        """Validate critical config values."""
        errors = []

        if not self.username:
            errors.append("account.username is required")
        if not self.password:
            errors.append("account.password is required")
        if not self.engine_weights:
            errors.append("engine.weights path is required")
        if self.challenge_mode not in ("whitelist", "open"):
            errors.append(f"challenge.mode must be 'whitelist' or 'open', got: {self.challenge_mode}")
        if self.humanizer_delay_min > self.humanizer_delay_max:
            errors.append("humanizer.delay_min must be <= humanizer.delay_max")

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

    # --- Humanizer ---
    @property
    def humanizer_enabled(self):
        return self._data["humanizer"]["enabled"]

    @property
    def humanizer_delay_min(self):
        return self._data["humanizer"]["delay_min"]

    @property
    def humanizer_delay_max(self):
        return self._data["humanizer"]["delay_max"]

    @property
    def humanizer_blunder_chance(self):
        return self._data["humanizer"]["blunder_chance"]

    @property
    def humanizer_premove_chance(self):
        return self._data["humanizer"]["premove_chance"]

    @property
    def humanizer_rating_mimic(self):
        return self._data["humanizer"]["rating_mimic"]

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

    def __repr__(self):
        return (
            f"Config(user={self.username}, engine={self.engine_type}, "
            f"mode={self.challenge_mode}, headless={self.headless})"
        )
