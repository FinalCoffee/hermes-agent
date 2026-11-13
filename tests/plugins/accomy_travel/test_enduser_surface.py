"""What an end user of the shipped accomy profile can read: every overlay message, the generic core messages, the
welcome texts and the Telegram menu are free of operator terms, and an upstream gateway string that names Hermes
cannot reach an end user without being overridden or allowlisted with a reason."""

import asyncio
import re
import shutil
import string
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import hermes_yaml as yaml
import pytest

from agent.i18n import t
from gateway.config import PlatformConfig
from gateway.session import SessionSource
from gateway.config import Platform
from hermes_cli import plugins as pmod

REPO = Path(__file__).resolve().parents[3]
DIST = REPO / "distributions" / "accomy"
OVERLAY = DIST / "locales" / "en.yaml"
BUNDLED = REPO / "locales" / "en.yaml"

FORBIDDEN = re.compile(
    r"hermes|nous|/sethome|/retry|/model|/login|/commands|/whoami|config|gateway|provider|admin|gpt-|[/\\](?:opt|Users)",
    re.IGNORECASE,
)
GENERIC_KEYS = (
    "gateway.notify.turn_error_generic",
    "gateway.errors.empty_reply_generic",
    "gateway.errors.failed_reply_generic",
)

# Upstream gateway strings that name Hermes but reach only operators: each family is refused for an end user
# (the admin-only refusal names no command), or it goes to the home channel, which this profile does not set.
# A new family that names Hermes must be overridden in the overlay or listed here with its reason.
ALLOWLIST = {
    "gateway.topic.": "/topic is admin-only here (refused with gateway.unauthorized.admin_only)",
    "gateway.update.": "/update is admin-only here",
    "gateway.debug.": "/debug is admin-only here",
    "gateway.kanban.": "the kanban feature and /kanban are admin-only here",
    "gateway.restart.": "/restart is admin-only here",
    "gateway.resume.": "/resume is admin-only here",
    "gateway.status.": "/status is admin-only here",
    "gateway.verbose.": "/verbose is admin-only here",
    "gateway.exec_approval.": "terminal approvals; the terminal toolset is disabled in this profile",
    "gateway.skills.": "skills toolset disabled; /skills is admin-only (the disabled-skill lines are overridden)",
    "gateway.busy.behavior_": "busy_input_mode is queue; the steer and interrupt descriptions are never shown",
    "gateway.pause.": "/pause is admin-only here",
    "gateway.bundles.": "/bundles is admin-only here",
    "gateway.model.": "/model is admin-only here",
    "gateway.background.": "/bg is admin-only here",
    "gateway.platform.": "/platform is admin-only here",
    "gateway.heartbeat.": "/heartbeat is admin-only here",
    "gateway.startup.": "startup and handoff notices go to the home channel or the admin chat; this profile sets neither",
    "gateway.unauthorized.owner_notice": "sent to the home channel (the owner), never to the stranger; none is set",
}


def _flatten(node, prefix=""):
    out = {}
    if isinstance(node, dict):
        for key, value in node.items():
            out.update(_flatten(value, f"{prefix}.{key}" if prefix else str(key)))
    else:
        out[prefix] = node
    return out


def _overlay() -> dict[str, str]:
    return _flatten(yaml.safe_load(OVERLAY.read_text()))


def _fields(template: str) -> dict[str, str]:
    return {name: "1" for _lit, name, _spec, _conv in string.Formatter().parse(template) if name}


@pytest.fixture
def profile_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    shutil.copy(DIST / "config.yaml", home / "config.yaml")
    shutil.copy(DIST / "SOUL.md", home / "SOUL.md")
    shutil.copytree(DIST / "locales", home / "locales")
    monkeypatch.setenv("HERMES_HOME", str(home))
    manager = pmod.PluginManager()
    manager.discover_and_load()
    return home


def test_every_overlay_message_renders_without_an_operator_term(profile_home):
    overlay = _overlay()
    assert overlay, "the overlay must not be empty"
    for key, template in overlay.items():
        rendered = t(key, lang="en", **_fields(template))
        assert rendered == template.format(**_fields(template)), key
        if rendered:
            assert not FORBIDDEN.search(rendered), (key, rendered)


@pytest.mark.parametrize("key", GENERIC_KEYS)
def test_the_generic_core_messages_read_cleanly_with_the_overlay(profile_home, key):
    rendered = t(key, lang="en")
    assert rendered and not FORBIDDEN.search(rendered), rendered


@pytest.mark.parametrize("key", GENERIC_KEYS)
def test_the_generic_core_messages_read_cleanly_without_an_overlay(tmp_path, monkeypatch, key):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    rendered = t(key, lang="en")
    assert rendered and not FORBIDDEN.search(rendered), rendered


def test_the_welcome_and_help_texts_read_cleanly(profile_home):
    from plugins.accomy_travel import welcome

    for replies in welcome._REPLIES.values():
        for reply in replies.values():
            assert not FORBIDDEN.search(reply.text), reply.text
            for option in reply.options:
                assert not FORBIDDEN.search(option), option


def test_the_telegram_menu_lists_exactly_the_four_end_user_commands(profile_home):
    from hermes_cli.commands_platforms import telegram_menu_commands, telegram_menu_max_commands

    menu, _hidden = telegram_menu_commands(telegram_menu_max_commands())

    assert [name for name, _desc in menu] == ["start", "new", "stop", "help"]
    descriptions = dict(menu)
    for name in ("start", "new", "stop", "help"):
        assert descriptions[name] == _overlay()[f"slash.{name}.description"]
        assert not FORBIDDEN.search(descriptions[name])


def test_a_blank_operator_notice_sends_nothing_on_telegram(profile_home):
    from gateway.run import GatewayRunner
    from plugins.platforms.telegram.adapter import TelegramAdapter

    adapter = TelegramAdapter(PlatformConfig(enabled=True, token="123:abc"))
    adapter._bot = AsyncMock()
    notice = t("gateway.notify.no_home_channel", lang="en", platform="Telegram", sethome_cmd="/sethome")
    assert notice == ""
    runner = SimpleNamespace(
        config=None,
        _delivery_adapter_for=lambda source: adapter,
        _thread_metadata_for_source=lambda source: None,
    )
    source = SessionSource(platform=Platform.TELEGRAM, user_id="42", chat_id="42", user_name="t", chat_type="dm")

    asyncio.run(GatewayRunner._deliver_platform_notice(runner, source, notice))

    adapter._bot.send_message.assert_not_awaited()


def test_a_notice_with_no_overlay_is_the_stock_text(tmp_path, monkeypatch):
    """The overlay is what blanks the notice: without it the stock wording (which names Hermes) is shown."""
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    assert "Hermes" in t("gateway.notify.no_home_channel", lang="en", platform="Telegram", sethome_cmd="/sethome")


def test_every_upstream_gateway_string_that_names_hermes_is_overridden_or_allowlisted():
    overlay = _overlay()
    bundled = _flatten(yaml.safe_load(BUNDLED.read_text()))
    unhandled = []
    for key, value in bundled.items():
        if not key.startswith("gateway.") or not isinstance(value, str) or "hermes" not in value.lower():
            continue
        if key in overlay or any(key.startswith(prefix) for prefix in ALLOWLIST):
            continue
        unhandled.append(key)
    assert not unhandled, f"override these in distributions/accomy/locales/en.yaml or allowlist them: {unhandled}"


def test_every_allowlist_entry_has_a_reason():
    assert all(reason.strip() for reason in ALLOWLIST.values())


def test_the_same_path_sends_a_non_blank_notice(profile_home):
    """The control for the test above: with text, the path reaches the Telegram bot, so the silence is the blank."""
    from gateway.run import GatewayRunner
    from plugins.platforms.telegram.adapter import TelegramAdapter

    adapter = TelegramAdapter(PlatformConfig(enabled=True, token="123:abc"))
    adapter._bot = AsyncMock()
    runner = SimpleNamespace(
        config=None,
        _delivery_adapter_for=lambda source: adapter,
        _thread_metadata_for_source=lambda source: None,
    )
    source = SessionSource(platform=Platform.TELEGRAM, user_id="42", chat_id="42", user_name="t", chat_type="dm")

    asyncio.run(GatewayRunner._deliver_platform_notice(runner, source, "stock notice"))

    adapter._bot.send_message.assert_awaited_once()
