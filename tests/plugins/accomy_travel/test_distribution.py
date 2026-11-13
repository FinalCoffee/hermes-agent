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
    "ACCOMY_DATABASE_URL",
)
ORDER_TOOLS = {"place_order", "list_trips", "get_order", "cancel_order"}
REQUIRED_ENV_NAMES = {
    "OPENAI_API_KEY",
    "TMC_APP_KEY",
    "TMC_APP_SECRET",
    "SEATUNIQUE_API_TOKEN",
    "GOOGLE_MAPS_SERVER_API_KEY",
    "BRAVE_SEARCH_API_KEY",
    "TELEGRAM_BOT_TOKEN",
    "TELEGRAM_ALLOWED_USERS",
    "ACCOMY_TRANSCRIBE_ACCESS_KEY_ID",
    "ACCOMY_TRANSCRIBE_SECRET_ACCESS_KEY",
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


def test_telegram_surface_is_exactly_the_travel_and_order_tools(dist_home):
    names = _resolved_telegram_tool_names()
    assert names == _manifest_tools()
    assert ORDER_TOOLS <= names
    assert not names & FORBIDDEN_TOOLS


def test_without_the_order_database_the_surface_is_the_twelve_travel_tools_and_the_file_reader(dist_home, monkeypatch):
    monkeypatch.delenv("ACCOMY_DATABASE_URL")

    names = _resolved_telegram_tool_names()

    assert len(names) == 13 and "read_attachment" in names
    assert not names & ORDER_TOOLS


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
@pytest.mark.parametrize("command", ["help", "whoami", "start"])
def test_non_admin_can_run_the_public_commands(dist_home, chat_type, command):
    assert _slash_policy(chat_type).can_run("424242", command)


@pytest.mark.parametrize("command", ["new", "stop"])
def test_session_commands_run_in_a_private_chat_only(dist_home, command):
    """In a group /new and /stop also act on other members' conversations."""
    assert _slash_policy("dm").can_run("424242", command)
    assert not _slash_policy("group").can_run("424242", command)


def test_a_reply_cannot_attach_a_host_file(dist_home, tmp_path, monkeypatch):
    """A model talked into writing MEDIA:/path must not make the gateway upload that file."""
    from gateway.media_policy import apply_media_policy_env
    from gateway.platforms.base import validate_media_delivery_path

    for name in ("HERMES_MEDIA_DELIVERY_STRICT", "HERMES_MEDIA_TRUST_RECENT_FILES", "HERMES_MEDIA_ALLOW_DIRS"):
        monkeypatch.delenv(name, raising=False)
    host_file = tmp_path / "notes.txt"
    host_file.write_text("just written, so the recency rule would trust it")

    apply_media_policy_env(load_config())

    assert validate_media_delivery_path(str(host_file)) is None


def test_attachments_are_downloaded(dist_home):
    from plugins.platforms.telegram import telegram_attachments

    telegram = load_gateway_config().platforms[Platform.TELEGRAM]

    assert not telegram_attachments.refused(SimpleNamespace(config=telegram))


def test_a_photo_goes_to_the_model_as_a_picture_with_no_separate_vision_call(dist_home):
    """Both the gateway (which attaches the picture) and the agent (which would otherwise replace it with a
    description from a second model call) ask whether the model reads pictures."""
    from agent.image_routing import _lookup_supports_vision, decide_image_input_mode

    cfg = load_config()
    model = cfg["model"]["default"]

    assert decide_image_input_mode("custom", model, cfg, requested_provider="newapi") == "native"
    assert _lookup_supports_vision("custom", model, cfg) is True


def test_a_voice_note_is_transcribed_by_the_plugins_provider_and_no_other(dist_home, monkeypatch, tmp_path):
    """The gateway's own entry point, with the shipped profile: the clip reaches Amazon Transcribe through the
    plugin (the stream is faked here) and no built-in provider, whose hosts the proxy would refuse anyway."""
    from agent.transcription_registry import get_provider
    from hermes_cli.plugins import _ensure_plugins_discovered
    from tools.transcription_tools import transcribe_audio

    assert load_gateway_config().stt_enabled
    _ensure_plugins_discovered()
    # The module the registered provider runs in: discovery loads the plugin under a namespace of its own.
    speech = type(get_provider("accomy-transcribe")).transcribe.__globals__
    monkeypatch.setenv(speech["ACCESS_KEY_ENV"], "TESTKEYID")
    monkeypatch.setenv(speech["SECRET_KEY_ENV"], "test-secret")
    monkeypatch.setitem(speech, "_pcm", lambda _path: (b"\x00" * (4 * speech["_BYTES_PER_SECOND"]), False))
    streamed = []
    monkeypatch.setitem(speech, "_stream", lambda pcm, query: streamed.append(query) or ("找下週五飛東京的機票", "zh-TW"))
    clip = tmp_path / "audio_0123456789ab.ogg"
    clip.write_bytes(b"OggS" + b"\x00" * 64)

    result = transcribe_audio(str(clip), None, "gateway")

    assert result == {"success": True, "transcript": "找下週五飛東京的機票", "provider": "accomy-transcribe"}
    assert streamed[0]["language-options"] == "zh-TW,en-US,ja-JP,ko-KR"


def test_the_proxy_allows_the_transcribe_host_of_the_profiles_region_and_only_its_streaming_port(dist_home):
    region = load_config()["plugins"]["entries"]["accomy_travel"]["settings"]["transcribe_region"]
    rules = [line.split() for line in (REPO / "deploy" / "accomy" / "squid.conf").read_text().splitlines()]

    assert ["acl", "transcribe_host", "dstdomain", f"transcribestreaming.{region}.amazonaws.com"] in rules
    assert ["acl", "transcribe_port", "port", "8443"] in rules
    access = [rule for rule in rules if rule[:1] == ["http_access"]]
    assert access.index(["http_access", "allow", "transcribe_host", "transcribe_port"]) < access.index(
        ["http_access", "deny", "!https_port"])
    assert [rule for rule in rules if rule[:2] == ["acl", "bot_hosts"] and "amazonaws.com" in rule[-1]
            and "transcribe" in rule[-1]] == []


def test_members_of_a_group_cannot_disturb_one_another(dist_home):
    from gateway.run import GatewayRunner

    gateway = load_gateway_config()

    assert gateway.thread_sessions_per_user  # one session per member inside a forum topic
    assert GatewayRunner._load_busy_input_mode() == "queue"  # a new message never aborts a running reply


def test_new_runs_without_the_confirm_card(dist_home):
    """The card offers "Always approve", which writes config.yaml."""
    assert load_config()["approvals"]["destructive_slash_confirm"] is False


def test_gateway_is_closed_by_default(dist_home):
    gateway = load_gateway_config()
    assert gateway.unauthorized_dm_behavior == "decline"
    assert not os.environ.get("GATEWAY_ALLOW_ALL_USERS")


def test_an_api_server_key_starts_no_api_server(dist_home, monkeypatch):
    # The container image writes this key into .env on first boot.
    monkeypatch.setenv("API_SERVER_KEY", "0123456789abcdef0123456789abcdef")
    api_server = load_gateway_config().platforms.get(Platform.API_SERVER)
    assert api_server is None or not api_server.enabled


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
