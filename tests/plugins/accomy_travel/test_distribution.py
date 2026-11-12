"""The shipped accomy profile, loaded the way a gateway loads it: tool surface, slash access and secret hygiene."""

import os
import re
import shutil
from pathlib import Path
from types import SimpleNamespace

import hermes_yaml as yaml
import pytest

import plugins.accomy_travel as package
from gateway.config import Platform, load_gateway_config
from gateway.slash_access import policy_for_source
from hermes_cli import plugins as pmod
from hermes_cli.config import load_config
from hermes_cli.tools_config import _get_platform_tools
from model_tools import get_tool_definitions
from toolsets import session_disabled_toolsets

REPO = Path(__file__).resolve().parents[3]
DIST = REPO / "distributions" / "accomy"
SECRETS = (
    "TMC_APP_KEY",
    "TMC_APP_SECRET",
    "SEATUNIQUE_API_TOKEN",
    "GOOGLE_MAPS_SERVER_API_KEY",
    "BRAVE_SEARCH_API_KEY",
)
REQUIRED_ENV_NAMES = {
    "OPENAI_API_KEY",
    "TMC_APP_KEY",
    "TMC_APP_SECRET",
    "SEATUNIQUE_API_TOKEN",
    "GOOGLE_MAPS_SERVER_API_KEY",
    "BRAVE_SEARCH_API_KEY",
    "TELEGRAM_BOT_TOKEN",
    "TELEGRAM_ALLOWED_USERS",
}
FORBIDDEN_TOOLS = {
    "terminal",
    "read_file",
    "write_file",
    "patch",
    "search_files",
    "web_search",
    "web_extract",
    "browser_navigate",
    "execute_code",
    "delegate_task",
    "memory",
    "session_search",
    "skills_list",
    "todo",
    "cronjob",
    "text_to_speech",
    "vision_analyze",
    "clarify",
}
# Shapes of real credentials; the distribution files may name variables but never carry values.
SECRET_SHAPES = (
    re.compile(r"sk-[A-Za-z0-9_-]{16,}"),
    re.compile(r"AIza[0-9A-Za-z_-]{30,}"),
    re.compile(r"\b\d{6,}:[A-Za-z0-9_-]{30,}\b"),
    re.compile(r"[A-Za-z0-9+/_-]{40,}"),
)


@pytest.fixture
def dist_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    shutil.copy(DIST / "config.yaml", home / "config.yaml")
    shutil.copy(DIST / "SOUL.md", home / "SOUL.md")
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.delenv("GATEWAY_ALLOW_ALL_USERS", raising=False)
    for name in SECRETS:
        monkeypatch.setenv(name, f"test-{name.lower()}")
    manager = pmod.PluginManager()
    manager.discover_and_load()
    assert manager._plugins["accomy_travel"].enabled
    return home


def _manifest_tools() -> set[str]:
    return set(
        yaml.safe_load((Path(package.__file__).parent / "plugin.yaml").read_text())[
            "provides_tools"
        ]
    )


def _resolved_telegram_tool_names() -> set[str]:
    cfg = load_config()
    toolsets = sorted(_get_platform_tools(cfg, "telegram"))
    defs = get_tool_definitions(
        enabled_toolsets=toolsets,
        disabled_toolsets=session_disabled_toolsets(None, "telegram"),
        quiet_mode=True,
    )
    return {d["function"]["name"] for d in defs}


def _slash_policy(chat_type: str):
    gateway = load_gateway_config()
    source = SimpleNamespace(platform=Platform.TELEGRAM, chat_type=chat_type)
    return policy_for_source(gateway, source)


def test_telegram_surface_is_exactly_the_travel_tools(dist_home):
    names = _resolved_telegram_tool_names()
    assert names == _manifest_tools()
    assert not names & FORBIDDEN_TOOLS


def test_cli_surface_matches_telegram(dist_home):
    cfg = load_config()
    assert sorted(_get_platform_tools(cfg, "cli")) == sorted(
        _get_platform_tools(cfg, "telegram")
    )


@pytest.mark.parametrize("chat_type", ["dm", "group"])
@pytest.mark.parametrize("command", ["model", "update", "restart", "yolo"])
def test_non_admin_cannot_run_operator_commands(dist_home, chat_type, command):
    assert not _slash_policy(chat_type).can_run("424242", command)


@pytest.mark.parametrize("chat_type", ["dm", "group"])
@pytest.mark.parametrize("command", ["new", "help", "start", "stop"])
def test_non_admin_can_run_the_public_commands(dist_home, chat_type, command):
    assert _slash_policy(chat_type).can_run("424242", command)


def test_gateway_is_closed_by_default(dist_home):
    gateway = load_gateway_config()
    assert gateway.unauthorized_dm_behavior == "decline"
    assert not os.environ.get("GATEWAY_ALLOW_ALL_USERS")


def test_manifest_names_every_secret_the_profile_needs():
    manifest = yaml.safe_load((DIST / "distribution.yaml").read_text())
    entries = {entry["name"]: entry for entry in manifest["env_requires"]}
    assert REQUIRED_ENV_NAMES <= set(entries)
    assert entries["TELEGRAM_ALLOWED_USERS"].get("required") is False
    assert all(entry.get("description") for entry in entries.values())


def test_distribution_files_carry_no_secret_values(dist_home, monkeypatch):
    for name in SECRETS:
        monkeypatch.setenv(name, f"sentinel-{name.lower()}-value")
    for path in (DIST / "distribution.yaml", DIST / "config.yaml", DIST / "SOUL.md"):
        text = path.read_text(encoding="utf-8")
        for name in SECRETS:
            assert f"sentinel-{name.lower()}-value" not in text
        for shape in SECRET_SHAPES:
            assert not shape.search(text), f"{path.name} matches {shape.pattern}"
