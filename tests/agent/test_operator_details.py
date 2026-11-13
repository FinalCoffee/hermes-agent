"""``gateway.hide_operator_details``: with it on, the system prompt and the replies that name the host, the install or
the model leave those details out and say one generic message; with it off, the stock prompt and replies are what
they were."""

import asyncio
from contextlib import nullcontext
from types import SimpleNamespace

import pytest

from agent.i18n import t
from agent.operator_details import operator_details_hidden
from agent.system_prompt import build_system_prompt_parts
from agent.turn_failure_copy import _SITE_COPY, site_copy
from gateway.platforms.base_turn_notice import notify_turn_error
from gateway.platforms.event import MessageEvent
from gateway.run import _normalize_empty_agent_response
from gateway.session import SessionSource

TRAVEL_TOOLS = {
    "find_places", "get_place_details", "get_weather", "search_hotels", "get_hotel_details",
    "get_hotel_rooms", "search_airports", "search_flights", "get_flight_offer", "search_events",
    "get_event_tickets", "research_web",
}
HOST_MARKERS = ("Host:", "User home directory:", "Current working directory:", "Scratch directory:",
                "Active Hermes profile:")
STOCK_IDENTITY = "You are Hermes Agent, built by Nous Research."


@pytest.fixture
def operator_home(tmp_path, monkeypatch):
    home = tmp_path / "operator-home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("HERMES_HOME", str(home / ".hermes"))
    (home / ".hermes").mkdir()
    monkeypatch.delenv("TERMINAL_ENV", raising=False)
    return home


def _set_hidden(operator_home, hidden: bool) -> None:
    (operator_home / ".hermes" / "config.yaml").write_text(
        f"gateway:\n  hide_operator_details: {'true' if hidden else 'false'}\n")


def _prompt(tool_names=TRAVEL_TOOLS) -> str:
    agent = SimpleNamespace(
        load_soul_identity=False, skip_context_files=True, valid_tool_names=set(tool_names),
        _task_completion_guidance=False, _tool_use_enforcement=False, _environment_probe=False,
        _kanban_worker_guidance="", _memory_store=None, _memory_manager=None, model="",
        provider="", platform="telegram", pass_session_id=False, session_id="",
        _emit_status=lambda *_a, **_k: None,
    )
    return "\n\n".join(build_system_prompt_parts(agent).values())


def test_the_switch_is_off_by_default(operator_home):
    assert operator_details_hidden() is False


def test_the_switch_reads_the_profile_config(operator_home):
    _set_hidden(operator_home, True)
    assert operator_details_hidden() is True


def test_the_prompt_keeps_the_host_profile_and_help_lines_when_the_switch_is_off(operator_home):
    prompt = _prompt()

    for marker in HOST_MARKERS:
        assert marker in prompt
    assert str(operator_home) in prompt
    assert STOCK_IDENTITY in prompt or "You are Hermes Agent" in prompt


def test_the_prompt_drops_host_profile_and_help_lines_when_the_switch_is_on(operator_home):
    _set_hidden(operator_home, True)

    prompt = _prompt()

    for marker in HOST_MARKERS:
        assert marker not in prompt
    assert str(operator_home) not in prompt
    assert "You are Hermes Agent" not in prompt and "hermes-agent.nousresearch" not in prompt
    assert "You are a helpful assistant." in prompt
    assert "Conversation started:" in prompt  # the date line is not an operator detail


@pytest.mark.parametrize("tool", ["terminal", "read_file"])
def test_the_host_block_stays_with_the_switch_off_even_without_a_host_tool(operator_home, tool):
    assert "User home directory:" in _prompt(TRAVEL_TOOLS | {tool})


@pytest.mark.parametrize("code", sorted(_SITE_COPY))
def test_a_failure_site_copy_is_one_generic_message_when_hidden(operator_home, code):
    _set_hidden(operator_home, True)

    assert site_copy(code) == t("gateway.errors.failed_reply_generic")


class _Adapter:
    name = "telegram"

    def __init__(self):
        self.sent = []

    def _media_delivery_scope(self, source):
        return nullcontext()

    def warning_text(self, text, fallback, **_kwargs):
        return text

    async def send(self, chat_id, content, metadata=None):
        self.sent.append(content)


def _event() -> MessageEvent:
    source = SessionSource(platform="telegram", user_id="42", chat_id="42", user_name="t", chat_type="dm")
    return MessageEvent(text="hi", message_id="m1", source=source)


def test_an_unhandled_exception_names_its_type_and_text_only_when_shown(operator_home):
    adapter = _Adapter()
    asyncio.run(notify_turn_error(adapter, _event(), RuntimeError("upstream secret detail")))
    assert "RuntimeError" in adapter.sent[-1] and "upstream secret detail" in adapter.sent[-1]

    _set_hidden(operator_home, True)
    adapter = _Adapter()
    asyncio.run(notify_turn_error(adapter, _event(), RuntimeError("upstream secret detail")))

    assert adapter.sent == [t("gateway.notify.turn_error_generic")]


def test_a_partial_reply_names_no_error_text_when_hidden(operator_home):
    agent_result = {
        "final_response": None, "api_calls": 5, "partial": True, "interrupted": False,
        "error": "Model generated invalid tool call: nonexistent_tool",
    }
    stock = _normalize_empty_agent_response(agent_result, "", history_len=10)
    assert "nonexistent_tool" in stock

    _set_hidden(operator_home, True)
    hidden = _normalize_empty_agent_response(agent_result, "", history_len=10)

    assert "nonexistent_tool" not in hidden
    assert hidden == t("gateway.errors.stopped_before_finishing", reason="")
