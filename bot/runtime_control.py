"""Helpers for applying runtime configuration changes."""

import logging


logger = logging.getLogger(__name__)


async def apply_runtime_config_updates(config, engine=None, context="runtime"):
    """
    Reload changed runtime config and restart the engine when needed.

    When an engine is provided, changes are applied only if
    control.telegram.apply_during_game is enabled.
    """
    if engine is not None and not config.telegram_control_apply_during_game:
        return True

    if not config.reload_if_changed():
        return True

    logger.info("Runtime config reloaded (%s).", context)
    if engine is None:
        return True

    if await engine.restart_if_config_changed():
        return True

    logger.error("Engine restart failed after runtime config change.")
    return False
