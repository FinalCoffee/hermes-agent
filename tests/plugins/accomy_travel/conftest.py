"""Discovery fixtures for the Accomy travel plugin: a temp profile that enables the plugin through config."""

import pytest

import hermes_yaml as yaml


@pytest.fixture
def accomy_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    # Tool search would defer the plugin's tools behind tool_describe/tool_call; the profile ships with it off.
    config = {
        "plugins": {"enabled": ["accomy_travel"]},
        "tools": {"tool_search": {"enabled": "off"}},
    }
    (home / "config.yaml").write_text(yaml.safe_dump(config))
    monkeypatch.setenv("HERMES_HOME", str(home))
    return home


@pytest.fixture
def accomy_manager(accomy_home):
    from hermes_cli import plugins as pmod

    manager = pmod.PluginManager()
    manager.discover_and_load()
    loaded = manager._plugins["accomy_travel"]
    assert loaded.enabled, loaded.error
    return manager
