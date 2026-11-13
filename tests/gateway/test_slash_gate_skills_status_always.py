"""Slash-access gate on three paths the registry gate never covered (driven through
``GatewayRunner._handle_message``, the real dispatch):

- skill and bundle slash commands (``/<skill>``, ``/<skill_name>``, stacked ``/a /b``) are asked of the
  same ``user_allowed_commands`` policy as registry commands, so a non-admin never gets a skill body;
- busy-path ``/status`` and ``/context`` are gated like the cold path (they answer mid-run for those
  the policy admits, and refuse everyone else);
- an ``always`` answer to a destructive slash confirm writes ``approvals.destructive_slash_confirm``
  into config.yaml, so only an admin of a gated scope may give it; ``once`` and ``cancel`` stay open.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from agent.i18n import t
from gateway.config import Platform
from gateway.session import build_session_key
from tests.gateway.test_slash_access_dispatch import _make_event, _make_runner, _make_source

ADMIN, MEMBER = "111", "999"
GATED = {"allow_admin_from": [ADMIN], "user_allowed_commands": ["start", "new", "stop"]}


def _src(user_id):
    return _make_source(platform=Platform.TELEGRAM, user_id=user_id, chat_type="dm")


def _ev(text, user_id):
    return _make_event(text, _src(user_id))


@pytest.fixture
def skills_env(tmp_path, monkeypatch):
    skills_dir = tmp_path / "skills"
    for name in ("audit-demo", "other-skill"):
        sd = skills_dir / name
        sd.mkdir(parents=True)
        (sd / "SKILL.md").write_text(
            f"---\nname: {name}\ndescription: desc {name}\n---\n\n# {name}\n\nSECRET-BODY-{name}\n"
        )
    import tools.skills_tool as skills_tool_module
    monkeypatch.setattr(skills_tool_module, "SKILLS_DIR", skills_dir)
    import agent.skill_commands as skill_commands_mod
    monkeypatch.setattr(skill_commands_mod, "_skill_commands_by_key", {})
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
    (tmp_path / "home").mkdir()
    return skills_dir


def _agent_seen(runner):
    """Replace the agent turn with a recorder: returns the list of message texts that reached the model."""
    seen = []

    async def fake_turn(event, *_args, **_kwargs):
        seen.append(event.text)
        return "agent-ran"

    runner._handle_message_with_agent = fake_turn
    return seen


@pytest.mark.asyncio
@pytest.mark.parametrize("text", ["/audit-demo do it", "/audit_demo do it", "/other-skill /audit-demo do it"])
async def test_non_admin_never_gets_skill_body_on_idle_path(skills_env, text):
    runner = _make_runner(platform=Platform.TELEGRAM, platform_extra=dict(GATED))
    seen = _agent_seen(runner)

    result = await runner._handle_message(_ev(text, MEMBER))

    assert result is not None
    assert seen == []
    assert not any("SECRET-BODY" in t for t in seen)


def _unknown(name):
    """The reply a non-admin gets for a skill they may not run: the same one a missing name gets."""
    return t("gateway.unknown_command", command=name.lstrip("/"))


@pytest.mark.asyncio
async def test_denied_skill_reads_like_a_missing_one(skills_env):
    """A non-admin learns nothing about which skill names are installed: a denied installed skill and a
    name that does not exist get the same reply."""
    runner = _make_runner(platform=Platform.TELEGRAM, platform_extra=dict(GATED))
    _agent_seen(runner)

    assert await runner._handle_message(_ev("/audit-demo do it", MEMBER)) == _unknown("/audit-demo")
    assert await runner._handle_message(_ev("/no-such-skill do it", MEMBER)) == _unknown("/no-such-skill")


@pytest.mark.asyncio
async def test_underscore_spelling_of_listed_skill_runs_for_member(skills_env):
    """Telegram menus spell skills with underscores; an operator who lists either spelling allows the skill."""
    runner = _make_runner(platform=Platform.TELEGRAM, platform_extra={
        "allow_admin_from": [ADMIN], "user_allowed_commands": ["start", "audit_demo"]})
    seen = _agent_seen(runner)

    assert await runner._handle_message(_ev("/audit_demo do it", MEMBER)) == "agent-ran"
    assert "SECRET-BODY-audit-demo" in seen[-1]
    assert await runner._handle_message(_ev("/audit-demo do it", MEMBER)) == "agent-ran"
    assert "SECRET-BODY-audit-demo" in seen[-1]


@pytest.mark.asyncio
async def test_gate_error_refuses_skill_instead_of_forwarding_it(skills_env):
    """A failed policy read denies the skill; the raw ``/skill args`` text never reaches the model."""
    runner = _make_runner(platform=Platform.TELEGRAM, platform_extra=dict(GATED))
    seen = _agent_seen(runner)

    def broken(*_args, **_kwargs):
        raise RuntimeError("policy unreadable")

    runner._check_slash_access = broken

    assert await runner._handle_message(_ev("/audit-demo do it", MEMBER)) == _unknown("/audit-demo")
    assert seen == []


@pytest.mark.asyncio
async def test_non_admin_refused_for_bundle_and_listed_bundle_runs_whole_bundle(skills_env, tmp_path, monkeypatch):
    from agent.skill_bundles import save_bundle

    monkeypatch.setenv("HERMES_BUNDLES_DIR", str(tmp_path / "bundles"))
    save_bundle("audit-bundle", ["audit-demo"], description="demo bundle")

    runner = _make_runner(platform=Platform.TELEGRAM, platform_extra=dict(GATED))
    seen = _agent_seen(runner)
    assert await runner._handle_message(_ev("/audit-bundle do it", MEMBER)) == _unknown("/audit-bundle")
    assert seen == []

    listed = _make_runner(platform=Platform.TELEGRAM, platform_extra={
        "allow_admin_from": [ADMIN], "user_allowed_commands": ["start", "audit-bundle"]})
    seen = _agent_seen(listed)
    assert await listed._handle_message(_ev("/audit-bundle do it", MEMBER)) == "agent-ran"
    assert "SECRET-BODY-audit-demo" in seen[-1]


@pytest.mark.asyncio
async def test_busy_skill_turn_refused_for_non_admin(skills_env, monkeypatch):
    """Mid-run /<skill> (queued as a turn under a session authority) is gated like the idle path."""
    from gateway import session_authorities

    monkeypatch.setattr(session_authorities, "active_authority",
                        lambda _runner: SimpleNamespace(route_epoch=lambda _key: 0))
    runner = _make_runner(platform=Platform.TELEGRAM, platform_extra=dict(GATED))
    admitted = AsyncMock(return_value=(True, "agent-ran"))
    runner._admit_command_turn = admitted
    src = _src(MEMBER)
    event = _ev("/audit-demo do it", MEMBER)

    handled, reply = await runner._busy_skill_turn(event, src, build_session_key(src))

    assert handled is True
    assert reply == _unknown("/audit-demo")
    admitted.assert_not_awaited()
    assert "SECRET-BODY" not in event.text


@pytest.mark.asyncio
async def test_admin_gets_skill_body_and_listed_skill_runs_for_member(skills_env):
    runner = _make_runner(platform=Platform.TELEGRAM, platform_extra={
        "allow_admin_from": [ADMIN], "user_allowed_commands": ["start", "new", "stop", "audit-demo"]})
    seen = _agent_seen(runner)

    assert await runner._handle_message(_ev("/audit-demo do it", ADMIN)) == "agent-ran"
    assert "SECRET-BODY-audit-demo" in seen[-1]
    # Listing the skill's name lets a non-admin run that one skill and no other.
    assert await runner._handle_message(_ev("/audit-demo do it", MEMBER)) == "agent-ran"
    assert "SECRET-BODY-audit-demo" in seen[-1]
    assert await runner._handle_message(_ev("/other-skill do it", MEMBER)) == _unknown("/other-skill")


@pytest.mark.asyncio
async def test_stacked_skills_refused_when_any_one_is_denied(skills_env):
    """`/allowed /denied text` runs nothing: the denied second skill refuses the whole command."""
    runner = _make_runner(platform=Platform.TELEGRAM, platform_extra={
        "allow_admin_from": [ADMIN], "user_allowed_commands": ["start", "other-skill"]})
    seen = _agent_seen(runner)

    assert await runner._handle_message(_ev("/other-skill /audit-demo do it", MEMBER)) == _unknown("/audit-demo")
    assert seen == []


@pytest.mark.asyncio
async def test_gating_disabled_default_runs_skill_for_everyone(skills_env):
    runner = _make_runner(platform=Platform.TELEGRAM, platform_extra={})
    seen = _agent_seen(runner)

    assert await runner._handle_message(_ev("/audit-demo do it", MEMBER)) == "agent-ran"
    assert "SECRET-BODY-audit-demo" in seen[-1]


@pytest.mark.asyncio
async def test_busy_status_and_context_refused_for_non_admin_answered_for_admin():
    runner = _make_runner(platform=Platform.TELEGRAM, platform_extra=dict(GATED))
    key = build_session_key(_src(ADMIN))
    runner._running_agents[key] = MagicMock()
    runner._session_state(key).turn.started_ts = 0
    runner._handle_status_command = AsyncMock(return_value="STATUS-PAGE")
    runner._handle_context_command = AsyncMock(return_value="CONTEXT-PAGE")

    for text in ("/status", "/context"):
        refused = await runner._handle_message(_ev(text, MEMBER))
        assert "⛔" in (refused or "")
    runner._handle_status_command.assert_not_awaited()
    runner._handle_context_command.assert_not_awaited()

    assert await runner._handle_message(_ev("/status", ADMIN)) == "STATUS-PAGE"


def _pending_destructive_confirm(runner, key, execute):
    """Register a /new confirm the way the gateway does, with its real config-writing handler."""
    from gateway.run_busy import GatewayBusySessionMixin
    from tools import slash_confirm

    async def handler(choice):
        return await GatewayBusySessionMixin._run_confirmed_destructive_slash(choice, "new", execute, key)

    slash_confirm.register(key, "c1", "new", handler, authority={
        "requester_id": MEMBER, "requester_name": "member", "chat_type": "dm", "approvers": ["*"]})
    return slash_confirm


@pytest.fixture
def config_home(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    (home / "config.yaml").write_text("approvals:\n  mode: manual\n", encoding="utf-8")
    monkeypatch.setenv("HERMES_HOME", str(home))
    from tools import slash_confirm
    slash_confirm._pending.clear()
    yield home / "config.yaml"
    slash_confirm._pending.clear()


@pytest.mark.asyncio
async def test_non_admin_always_refused_and_config_unchanged(config_home):
    runner = _make_runner(platform=Platform.TELEGRAM, platform_extra=dict(GATED))
    key = build_session_key(_src(MEMBER))
    execute = AsyncMock(return_value="fresh session")
    slash_confirm = _pending_destructive_confirm(runner, key, execute)
    before = config_home.read_bytes()

    reply = await runner._handle_message(_ev("always", MEMBER))

    assert "⛔" in (reply or "")
    assert config_home.read_bytes() == before
    execute.assert_not_awaited()
    assert slash_confirm.get_pending(key) is not None  # still pending for an admin

    # The requester's own "once" still works.
    assert await runner._handle_message(_ev("once", MEMBER)) == "fresh session"
    execute.assert_awaited_once()


@pytest.mark.asyncio
async def test_admin_always_persists_opt_out(config_home):
    runner = _make_runner(platform=Platform.TELEGRAM, platform_extra=dict(GATED))
    key = build_session_key(_src(ADMIN))
    execute = AsyncMock(return_value="fresh session")
    _pending_destructive_confirm(runner, key, execute)

    await runner._handle_message(_ev("always", ADMIN))

    assert "destructive_slash_confirm: false" in config_home.read_text(encoding="utf-8")
    execute.assert_awaited_once()


@pytest.mark.asyncio
async def test_gating_disabled_always_still_persists(config_home):
    runner = _make_runner(platform=Platform.TELEGRAM, platform_extra={})
    key = build_session_key(_src(MEMBER))
    execute = AsyncMock(return_value="fresh session")
    _pending_destructive_confirm(runner, key, execute)

    await runner._handle_message(_ev("always", MEMBER))

    assert "destructive_slash_confirm: false" in config_home.read_text(encoding="utf-8")


@pytest.mark.asyncio
async def test_telegram_always_button_from_non_admin_refused_config_unchanged(config_home, monkeypatch):
    """The main real path for the config write: a non-admin's sc:always button tap. Refused, nothing
    written; the requester's own sc:once tap still runs the command."""
    from tests.gateway.test_telegram_approval_buttons import _make_adapter

    monkeypatch.setenv("TELEGRAM_ALLOWED_USERS", "*")
    adapter = _make_adapter(extra=dict(GATED))
    adapter._bot.send_message = AsyncMock(
        return_value=SimpleNamespace(chat_id=-100, message_id=42, message_thread_id=None))
    key = build_session_key(_src(MEMBER))
    execute = AsyncMock(return_value="fresh session")
    slash_confirm = _pending_destructive_confirm(MagicMock(), key, execute)
    await adapter.send_slash_confirm("-100", "Confirm", "start a new session?", key, "c1")
    before = config_home.read_bytes()

    def tap(choice):
        query = AsyncMock(data=f"sc:{choice}:c1",
                          message=MagicMock(chat_id=-100, message_id=42, message_thread_id=None, text="x"),
                          from_user=MagicMock(first_name="Member", id=MEMBER))
        return adapter._handle_callback_query(MagicMock(callback_query=query), MagicMock())

    await tap("always")
    assert config_home.read_bytes() == before
    execute.assert_not_awaited()
    assert slash_confirm.get_pending(key) is not None

    await tap("once")
    execute.assert_awaited_once()
    assert config_home.read_bytes() == before


@pytest.mark.asyncio
async def test_a_failing_bundle_scan_does_not_stop_a_skill_command(skills_env, monkeypatch):
    from agent import skill_bundles

    def broken_scan(_command):
        raise OSError("bad bundle manifest")

    monkeypatch.setattr(skill_bundles, "resolve_bundle_command_key", broken_scan)
    runner = _make_runner(platform=Platform.TELEGRAM, platform_extra=GATED)
    seen = _agent_seen(runner)

    assert await runner._handle_message(_ev("/audit-demo do it", ADMIN)) == "agent-ran"
    assert "SECRET-BODY-audit-demo" in seen[-1]
