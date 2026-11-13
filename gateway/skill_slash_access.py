"""Slash-access gate for skill and bundle slash commands (``/<skill>``, ``/<bundle>``).

A skill or bundle is asked of the same policy as a registry command, under its slash name (the
``/slug`` key without the slash, which is how ``resolve_command`` spells names). An operator allows one
by listing it in ``user_allowed_commands`` (``group_user_allowed_commands`` in a group), either as
``audit-demo`` or as the Telegram-menu spelling ``audit_demo``.

A refused skill gets the reply a missing one gets (``gateway.unknown_command``). A policy read that
raises refuses too: the raw ``/skill args`` text is never forwarded to the model.
"""

from __future__ import annotations

import logging
from typing import Iterable, Optional

from agent.i18n import t
from gateway.platforms.event import MessageEvent
from gateway.session import SessionSource

logger = logging.getLogger("gateway.run")


def skill_slash_denial(runner, source, keys: Iterable[str]) -> Optional[str]:
    """The refusal for the first skill or bundle ``/slug`` key in *keys* that *source* may not run."""
    for key in keys:
        name = key.lstrip("/")
        if not _may_run(runner, source, name):
            return t("gateway.unknown_command", command=name)
    return None


def bundle_slash_denial(runner, source, command: str) -> Optional[str]:
    """The refusal when *command* names a bundle *source* may not run. A bundle scan that fails names no
    bundle (``bundle_slash_rewrite`` fails the same way), so the skill path runs."""
    try:
        from agent.skill_bundles import resolve_bundle_command_key
        bundle_key = resolve_bundle_command_key(command)
    except Exception as exc:
        logger.warning("Bundle lookup failed: %s", exc, exc_info=True)
        return None
    return None if bundle_key is None else skill_slash_denial(runner, source, [bundle_key])


def _may_run(runner, source, name: str) -> bool:
    spellings = dict.fromkeys((name, name.replace("-", "_")))
    try:
        return any(runner._check_slash_access(source, spelling) is None for spelling in spellings)
    except Exception:
        logger.exception("Slash-access check for /%s failed; refusing it", name)
        return False


def bundle_slash_rewrite(event: MessageEvent, source: SessionSource, _quick_key: str, command: str) -> bool:
    """Rewrite ``/<bundle>`` to the bundle invocation message; True when handled.
    Skill bundles take precedence over individual skill commands (mirrors CLI dispatch)."""
    try:
        from agent.skill_bundles import (
            build_bundle_invocation_message, resolve_bundle_command_key
        )
        bundle_key = resolve_bundle_command_key(command)
        if bundle_key is None:
            return False
        # Pass the platform explicitly: bundle skill loading bypasses get_skill_commands()'
        # scan-time disabled filter, and one gateway process serves several platforms, so
        # env-var platform resolution can't be trusted here.
        # Mirrors the stacked-skill gate (#58888).
        bundle_result = build_bundle_invocation_message(
            bundle_key, event.get_command_args().strip(), task_id=_quick_key,
            platform=source.platform.value if source.platform else None,
        )
        if not bundle_result:
            return False
        event.text, _loaded, missing = bundle_result
        if missing:
            logger.info("Bundle %s skipped missing skills: %s", bundle_key, ", ".join(missing))
        return True  # Fall through to normal message processing with bundle content
    except Exception as exc:
        logger.warning("Bundle dispatch failed: %s", exc, exc_info=True)
        return False
