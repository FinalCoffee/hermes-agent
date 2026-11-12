"""Real plugin discovery: the plugin's tools register under the accomy_travel toolset and are gated by secrets.

Expectations are computed from plugin.yaml and from each tool module's TOOLS, so they stay correct as the
other tool modules gain tools.
"""

from pathlib import Path

import hermes_yaml as yaml
import pytest

import plugins.accomy_travel as package
from model_tools import get_tool_definitions
from plugins.accomy_travel import (
    tools_events,
    tools_flights,
    tools_hotels,
    tools_places,
    tools_research,
)
from tools.registry import registry

SECRETS = (
    "TMC_APP_KEY",
    "TMC_APP_SECRET",
    "SEATUNIQUE_API_TOKEN",
    "GOOGLE_MAPS_SERVER_API_KEY",
    "BRAVE_SEARCH_API_KEY",
)
TOOL_MODULES = (tools_places, tools_hotels, tools_flights, tools_events, tools_research)
TOOLSET = "accomy_travel"


def _manifest_tools() -> set[str]:
    manifest = yaml.safe_load(
        (Path(package.__file__).parent / "plugin.yaml").read_text()
    )
    return set(manifest["provides_tools"])


def _exported_specs() -> list:
    return [spec for module in TOOL_MODULES for spec in module.TOOLS]


def _exported_names() -> set[str]:
    return {spec.name for spec in _exported_specs()}


def _visible_names() -> set[str]:
    defs = get_tool_definitions(enabled_toolsets=[TOOLSET], quiet_mode=True)
    return {d["function"]["name"] for d in defs}


@pytest.fixture
def secrets_set(monkeypatch):
    for name in SECRETS:
        monkeypatch.setenv(name, f"test-{name.lower()}")


@pytest.fixture
def secrets_unset(monkeypatch):
    for name in SECRETS:
        monkeypatch.delenv(name, raising=False)


def test_registered_tools_sit_between_exported_and_manifest(accomy_manager):
    registered = set(registry.get_tool_names_for_toolset(TOOLSET))
    assert _exported_names() <= registered <= _manifest_tools()


def test_exported_tool_names_are_unique_and_declared_in_the_manifest():
    names = [spec.name for spec in _exported_specs()]
    assert len(names) == len(set(names))
    assert set(names) <= _manifest_tools()


def test_all_secrets_present_expose_every_exported_tool(accomy_manager, secrets_set):
    assert _visible_names() == _exported_names()


def test_missing_secrets_hide_every_tool(accomy_manager, secrets_unset):
    assert _visible_names() == set()


@pytest.mark.parametrize("missing", SECRETS)
def test_one_missing_secret_hides_only_the_tools_that_need_it(
    accomy_manager, monkeypatch, secrets_set, missing
):
    monkeypatch.delenv(missing)
    expected = {
        spec.name for spec in _exported_specs() if missing not in spec.requires_env
    }
    assert _visible_names() == expected
