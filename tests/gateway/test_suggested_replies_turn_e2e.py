"""Suggested replies through a whole gateway turn: ``GatewayRunner._run_agent`` with a live stream consumer.

The turn runs the real runner, the real ``GatewayStreamConsumer`` and the real delivery path
(``_send_final_text`` / reconcile). Only the model (a scripted ``AIAgent``) and the platform transport
(a recording adapter that splits long payloads the way Telegram does) are fakes. Every text the
platform is asked to display is captured, so a footer fragment that reaches any send, edit or chunk
fails the test. The keyboard is observed through ``attach_suggested_replies``.

Scenario 6 (a tap through the real Telegram adapter) lives in its own section at the bottom.
"""

from __future__ import annotations

import importlib
import itertools
import logging
import re
import sys
import types
from types import SimpleNamespace

import pytest

from gateway.config import Platform, PlatformConfig, StreamingConfig
from gateway.platforms.base import BasePlatformAdapter, SendResult, utf16_len
from gateway.platforms.event import MessageEvent
from gateway.session_ingress import deliver_response
from gateway.session import SessionSource
from tests.waiting import async_wait_until

FOOTER_OPTIONS = ("看房型", "找更便宜的", "附近景點")
FOOTER = "[suggestions: " + " | ".join(FOOTER_OPTIONS) + "]"
ANSWER = "您要的五星飯店在台北信義區，含早餐的雙人房每晚約 NT$4,800，可以先比較後再決定。"
CHUNK_LIMIT = 4000  # Telegram's message budget, the same cut the platform's send and finalize use


# ---------------------------------------------------------------------------
# Platform transport: records every displayed payload, splits like Telegram
# ---------------------------------------------------------------------------


def _split(content: str) -> list[str]:
    """Cut at newlines so that no chunk exceeds the platform budget (a single over-long line is cut hard)."""
    chunks: list[str] = []
    current = ""
    for line in content.split("\n"):
        candidate = line if not current else current + "\n" + line
        if current and utf16_len(candidate) > CHUNK_LIMIT:
            chunks.append(current)
            current = line
        else:
            current = candidate
    chunks.append(current)
    out: list[str] = []
    for chunk in chunks:
        while utf16_len(chunk) > CHUNK_LIMIT:
            cut = CHUNK_LIMIT
            out.append(chunk[:cut])
            chunk = chunk[cut:]
        out.append(chunk)
    return out


class TurnCaptureAdapter(BasePlatformAdapter):
    """Telegram-shaped recorder. ``payloads`` is every text the platform was asked to show, in order;
    ``visible`` maps message id to the text on screen; ``attached`` records each keyboard attach."""

    MAX_MESSAGE_LENGTH = CHUNK_LIMIT + 96

    def __init__(self, platform=Platform.TELEGRAM):
        super().__init__(PlatformConfig(enabled=True, token="***"), platform)
        self.payloads: list[str] = []
        self.visible: dict[str, str] = {}
        self.attached: list[tuple[str, str, tuple[str, ...]]] = []
        self._next = 0

    async def connect(self, *, is_reconnect: bool = False) -> bool:
        return True

    async def disconnect(self) -> None:
        return None

    async def get_chat_info(self, chat_id: str):
        return {"id": chat_id}

    async def send_typing(self, chat_id, metadata=None) -> None:
        return None

    async def stop_typing(self, chat_id) -> None:
        return None

    def _show(self, text: str) -> str:
        self._next += 1
        message_id = f"m-{self._next}"
        self.payloads.append(text)
        self.visible[message_id] = text
        return message_id

    async def send(self, chat_id, content, reply_to=None, metadata=None) -> SendResult:
        ids = [self._show(chunk) for chunk in _split(content)]
        raw = {"message_ids": ids} if len(ids) > 1 else None
        return SendResult(success=True, message_id=ids[-1], raw_response=raw)

    async def edit_message(
        self, chat_id, message_id, content, *, finalize: bool = False, metadata=None
    ) -> SendResult:
        chunks = _split(content)
        self.payloads.append(chunks[0])
        self.visible[str(message_id)] = chunks[0]
        ids = [str(message_id)] + [self._show(chunk) for chunk in chunks[1:]]
        raw = {"message_ids": ids} if len(ids) > 1 else None
        return SendResult(success=True, message_id=ids[-1], raw_response=raw)

    async def attach_suggested_replies(self, source, message_id, options) -> bool:
        self.attached.append((source.chat_id, str(message_id), tuple(options)))
        return True

    # -- helpers for assertions -------------------------------------------------

    def visible_text(self) -> str:
        """The answer as a user reads it: chunks in creation order, newlines normalised."""
        shown = "".join(self.visible[key] for key in self.visible).replace("\n", "")
        return re.sub(r" \(\d+/\d+\)", "", shown)  # the stream consumer's own chunk markers

    def last_message_id(self) -> str:
        return list(self.visible)[-1]


def _assert_no_footer_on_screen(adapter: TurnCaptureAdapter) -> None:
    for payload in adapter.payloads:
        assert "[sugg" not in payload.lower(), payload
        assert "suggestions" not in payload.lower(), payload
        for option in FOOTER_OPTIONS:
            assert option not in payload, payload


# ---------------------------------------------------------------------------
# Scripted model and runner wiring (same boundary as test_stale_finalize_suppression.py)
# ---------------------------------------------------------------------------


def _scripted_agent(deltas, final_response, *, transform=None):
    """An AIAgent stand-in that streams ``deltas`` through the real callback and returns a final.

    ``transform`` (optional) runs the real ``apply_llm_output_transform`` on the final, the way the
    turn finalizer does, so a plugin ``transform_llm_output`` hook rewrites it before delivery.
    """

    class ScriptedAgent:
        def __init__(self, **kwargs):
            self.stream_delta_callback = kwargs.get("stream_delta_callback")
            self.tools = []
            self.platform = "telegram"
            self.session_id = kwargs.get("session_id", "s")
            self.model = "scripted"

        def __getattr__(self, name):
            return None

        def run_conversation(self, message, conversation_history=None, task_id=None, **kwargs):
            if self.stream_delta_callback:
                for delta in deltas:
                    self.stream_delta_callback(delta)
            response = {
                "final_response": final_response,
                "response_previewed": False,
                "messages": [],
                "api_calls": 1,
                "completed": True,
            }
            if transform is not None:
                from agent.turn_finalizer import apply_llm_output_transform

                final, transformed, pre = apply_llm_output_transform(
                    self, final_response, turn_id="turn-1"
                )
                response.update(
                    final_response=final,
                    response_transformed=transformed,
                    pre_transform_response=pre,
                )
            return response

    return ScriptedAgent


def _chunked(text: str, cut_points: list[int]) -> list[str]:
    bounds = [0, *sorted(set(p for p in cut_points if 0 < p < len(text))), len(text)]
    return [text[a:b] for a, b in itertools.pairwise(bounds)]


def _char_deltas(text: str) -> list[str]:
    return list(text)


def _runner(adapter, *, streaming: bool):
    gateway_run = importlib.import_module("gateway.run")
    runner = object.__new__(gateway_run.GatewayRunner)
    runner.adapters = {adapter.platform: adapter}
    runner._voice_mode = {}
    runner._prefill_messages = []
    runner._ephemeral_system_prompt = ""
    runner._reasoning_config = None
    runner._provider_routing = {}
    runner._fallback_model = None
    runner._session_db = None
    runner._running_agents = {}
    runner.session_store = SimpleNamespace(_entries={}, _save=lambda: None)
    runner.hooks = SimpleNamespace(loaded_hooks=False)
    runner.config = SimpleNamespace(
        thread_sessions_per_user=False,
        group_sessions_per_user=False,
        stt_enabled=False,
        streaming=StreamingConfig.from_dict(
            {"enabled": streaming, "edit_interval": 0.01, "buffer_threshold": 1}
        ),
    )
    return runner


async def _run_turn(monkeypatch, tmp_path, agent_cls, *, streaming: bool, session_id: str):
    import hermes_yaml as yaml

    (tmp_path / "config.yaml").write_text(
        yaml.safe_dump(
            {
                "display": {"tool_progress": "off", "interim_assistant_messages": False},
                "streaming": {
                    "enabled": streaming,
                    "edit_interval": 0.01,
                    "buffer_threshold": 1,
                },
            }
        ),
        encoding="utf-8",
    )
    fake_dotenv = types.ModuleType("dotenv")
    fake_dotenv.load_dotenv = lambda *args, **kwargs: None
    monkeypatch.setitem(sys.modules, "dotenv", fake_dotenv)
    fake_run_agent = types.ModuleType("run_agent")
    fake_run_agent.AIAgent = agent_cls
    monkeypatch.setitem(sys.modules, "run_agent", fake_run_agent)

    gateway_run = importlib.import_module("gateway.run")
    monkeypatch.setattr(gateway_run, "_hermes_home", tmp_path)
    monkeypatch.setattr(gateway_run, "_resolve_runtime_agent_kwargs", lambda: {"api_key": "***"})

    adapter = TurnCaptureAdapter()
    runner = _runner(adapter, streaming=streaming)
    source = SessionSource(platform=Platform.TELEGRAM, chat_id="-1001", chat_type="group")
    session_key = "agent:main:telegram:group:-1001"
    event = MessageEvent(text="找台北五星飯店", source=source, message_id="in-1")
    result = await runner._run_agent(
        message="找台北五星飯店",
        context_prompt="",
        history=[],
        source=source,
        session_id=session_id,
        session_key=session_key,
    )
    if not result.get("already_sent"):
        # The turn caller's delivery of an unstreamed reply: extraction strips the footer, then the
        # ledgered final send and the keyboard attach run as they do in production.
        await deliver_response(adapter, event, session_key, result["final_response"])
    return adapter, result



def _only_option_set(adapter) -> tuple[str, ...]:
    """The single non-empty keyboard the turn attached (retirements carry ``()``)."""
    live = [options for _, _, options in adapter.attached if options]
    assert len(live) == 1, adapter.attached
    return live[0]


# ---------------------------------------------------------------------------
# Scenario 1: streaming on
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_streamed_turn_char_by_char_footer_never_reaches_the_screen(monkeypatch, tmp_path):
    full = f"{ANSWER}\n{FOOTER}"
    agent = _scripted_agent(_char_deltas(full), full)
    adapter, _ = await _run_turn(monkeypatch, tmp_path, agent, streaming=True, session_id="e2e-s1-char")

    _assert_no_footer_on_screen(adapter)
    assert adapter.visible_text() == ANSWER
    assert len(adapter.attached) == 1
    chat_id, message_id, options = adapter.attached[0]
    assert chat_id == "-1001"
    assert message_id == adapter.last_message_id()
    assert adapter.visible[message_id] == ANSWER
    assert options == FOOTER_OPTIONS


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "cut_offsets",
    [
        # Cuts inside the footer: before the bracket, inside "sugg", inside "estions:", at a separator,
        # right before the closing bracket. Offsets are relative to the start of the footer line.
        [0, 1, 5, 11, 12, 20],
        [1, 3, 9, 16, 26, 33, len(FOOTER) - 1],
        [len(FOOTER) // 2],
    ],
)
async def test_streamed_turn_chunks_splitting_the_footer_never_reach_the_screen(
    monkeypatch, tmp_path, cut_offsets
):
    full = f"{ANSWER}\n{FOOTER}"
    footer_start = len(ANSWER) + 1
    cuts = [footer_start + off for off in cut_offsets] + [len(ANSWER) + 1 - 1]
    deltas = _chunked(full, cuts)
    assert "".join(deltas) == full

    agent = _scripted_agent(deltas, full)
    adapter, _ = await _run_turn(
        monkeypatch, tmp_path, agent, streaming=True, session_id=f"e2e-s1-chunk-{cut_offsets[0]}"
    )

    _assert_no_footer_on_screen(adapter)
    assert adapter.visible_text() == ANSWER
    assert _only_option_set(adapter) == FOOTER_OPTIONS
    _, message_id, _ = next(row for row in adapter.attached if row[2])
    assert message_id == adapter.last_message_id()


# ---------------------------------------------------------------------------
# Scenario 2: streaming off
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_non_streamed_turn_sends_stripped_text_once_and_attaches_the_options(monkeypatch, tmp_path):
    full = f"{ANSWER}\n{FOOTER}"
    agent = _scripted_agent([], full)
    adapter, _ = await _run_turn(monkeypatch, tmp_path, agent, streaming=False, session_id="e2e-s2")

    _assert_no_footer_on_screen(adapter)
    assert adapter.payloads == [ANSWER]
    assert adapter.attached == [("-1001", adapter.last_message_id(), FOOTER_OPTIONS)]


# ---------------------------------------------------------------------------
# Scenario 3: streamed final without a footer, footer appended by the real accomy policy hook
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_plugin_footer_appended_after_a_streamed_answer_is_buttons_only(
    monkeypatch, tmp_path, caplog
):
    from plugins.accomy_travel import policy

    def invoke_hook(name, **kwargs):
        if name != "transform_llm_output":
            return []
        return [policy._ensure_suggestions_footer(kwargs["response_text"], kwargs.get("platform"))]

    from hermes_cli import lifecycle

    monkeypatch.setattr(lifecycle, "invoke_hook", invoke_hook)
    agent = _scripted_agent(_char_deltas(ANSWER), ANSWER, transform=True)

    with caplog.at_level(logging.DEBUG):
        adapter, _ = await _run_turn(
            monkeypatch, tmp_path, agent, streaming=True, session_id="e2e-s3"
        )

    _assert_no_footer_on_screen(adapter)
    assert adapter.visible_text() == ANSWER
    assert [row for row in adapter.attached if row[2]] == [
        ("-1001", adapter.last_message_id(), policy._DEFAULT_OPTIONS["zh"])
    ]
    error_records = [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert not error_records, [r.getMessage() for r in error_records]


# ---------------------------------------------------------------------------
# Scenario 4: a long answer that needs several Telegram messages
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_long_streamed_answer_splits_and_the_keyboard_sits_on_the_last_chunk(monkeypatch, tmp_path):
    paragraphs = [
        f"第{i}段：台北信義區的飯店房價會因季節與活動而變動，建議比較退房政策與取消條件。"
        for i in range(1, 260)
    ]
    answer = "\n\n".join(paragraphs)
    assert utf16_len(answer) > 9000
    full = f"{answer}\n{FOOTER}"
    deltas = [full[i : i + 300] for i in range(0, len(full), 300)]

    agent = _scripted_agent(deltas, full)
    adapter, _ = await _run_turn(monkeypatch, tmp_path, agent, streaming=True, session_id="e2e-s4")

    _assert_no_footer_on_screen(adapter)
    assert len(adapter.visible) >= 3  # the answer really needed several messages
    assert adapter.visible_text() == answer.replace("\n", "")
    live = [row for row in adapter.attached if row[2]]
    assert live == [("-1001", adapter.last_message_id(), FOOTER_OPTIONS)]


# ---------------------------------------------------------------------------
# Scenario 5: degenerate footers
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_reply_that_is_only_a_footer_shows_no_footer_text_and_no_crash(monkeypatch, tmp_path):
    full = FOOTER
    agent = _scripted_agent(_char_deltas(full), full)
    adapter, _ = await _run_turn(monkeypatch, tmp_path, agent, streaming=True, session_id="e2e-s5-only")

    _assert_no_footer_on_screen(adapter)
    assert adapter.attached == [], adapter.attached  # no answer to put a keyboard on
    assert adapter.payloads == [], adapter.payloads  # nothing visible


@pytest.mark.asyncio
async def test_a_single_option_footer_is_stripped_and_gives_no_buttons(monkeypatch, tmp_path):
    full = f"{ANSWER}\n[suggestions: 只有一個]"
    agent = _scripted_agent(_char_deltas(full), full)
    adapter, _ = await _run_turn(monkeypatch, tmp_path, agent, streaming=True, session_id="e2e-s5-one")

    assert "suggestions" not in adapter.visible_text()
    assert adapter.visible_text() == ANSWER
    assert [row for row in adapter.attached if row[2]] == []


@pytest.mark.asyncio
async def test_an_unterminated_footer_gives_no_buttons(monkeypatch, tmp_path):
    full = f"{ANSWER}\n[suggestions: 看房型 | 找更便宜的"
    agent = _scripted_agent(_char_deltas(full), full)
    adapter, _ = await _run_turn(monkeypatch, tmp_path, agent, streaming=True, session_id="e2e-s5-open")

    assert [row for row in adapter.attached if row[2]] == []
    assert adapter.visible_text() == ANSWER, adapter.payloads


# ---------------------------------------------------------------------------
# Scenario 6: a tap through the real Telegram adapter, into the runner's message handler
# ---------------------------------------------------------------------------


class _Button:
    def __init__(self, text, callback_data):
        self.text, self.callback_data = text, callback_data


class _Markup:
    def __init__(self, inline_keyboard):
        self.inline_keyboard = inline_keyboard


def _telegram_adapter(monkeypatch, extra):
    from unittest.mock import AsyncMock, MagicMock

    from plugins.platforms.telegram import telegram_suggestions
    from plugins.platforms.telegram.adapter import TelegramAdapter

    monkeypatch.setattr(telegram_suggestions, "InlineKeyboardButton", _Button)
    monkeypatch.setattr(telegram_suggestions, "InlineKeyboardMarkup", _Markup)
    adapter = TelegramAdapter(PlatformConfig(enabled=True, token="test-token", extra=extra))
    adapter._bot = AsyncMock()
    adapter._bot.username = "accomy_bot"
    adapter._bot.edit_message_reply_markup = AsyncMock(return_value=True)
    adapter._app = MagicMock()
    adapter._is_callback_user_authorized = lambda *a, **k: True
    # The runner's inbound entry point, recorded instead of run: the real base handle_message runs.
    adapter._message_handler = AsyncMock(return_value=None)
    return adapter


def _tap_query(data, *, tapper_id, chat_id, chat_type, message_id=900):
    from unittest.mock import AsyncMock

    return SimpleNamespace(
        data=data,
        from_user=SimpleNamespace(id=tapper_id, first_name="Ana", full_name="Ana Lee"),
        message=SimpleNamespace(
            message_id=message_id,
            chat_id=int(chat_id),
            message_thread_id=None,
            chat=SimpleNamespace(type=chat_type),
        ),
        answer=AsyncMock(),
        edit_message_reply_markup=AsyncMock(),
    )


async def _attach_then_tap(adapter, *, chat_id, chat_type, tapper_id, option_index=1, asker_id="555"):
    options = ("Book it", "Show more")
    source = SimpleNamespace(chat_id=chat_id, chat_type=chat_type, thread_id=None, user_id=asker_id)
    assert await adapter.attach_suggested_replies(source, "900", options)
    kwargs = adapter._bot.edit_message_reply_markup.await_args.kwargs
    token = kwargs["reply_markup"].inline_keyboard[0][0].callback_data.split(":")[1]
    query = _tap_query(
        f"sg:{token}:{option_index}", tapper_id=tapper_id, chat_id=chat_id, chat_type=chat_type
    )
    await adapter._handle_suggestion_callback(query, query.data, adapter._callback_ctx(query))
    # The base adapter hands the event to the runner from its per-session background task.
    await async_wait_until(lambda: adapter._message_handler.await_count >= 1, timeout=5)
    return options[option_index], adapter._message_handler


@pytest.mark.asyncio
async def test_a_tap_in_a_dm_reaches_the_runner_as_the_tappers_next_message(monkeypatch):
    adapter = _telegram_adapter(monkeypatch, {"suggested_replies": True})
    option, handler = await _attach_then_tap(
        adapter, chat_id="100", chat_type="private", tapper_id=555
    )

    handler.assert_awaited_once()
    event = handler.await_args.args[0]
    assert event.text == option == "Show more"
    assert event.source.user_id == "555"
    assert event.source.chat_id == "100"


@pytest.mark.asyncio
async def test_a_tap_by_the_asker_in_a_group_with_require_mention_reaches_the_runner_as_addressed(monkeypatch):
    """The asker's tap is an explicit interaction with the bot's own message, so it is addressed to the
    bot under require_mention (like a reply to it). The event carries the group identity line."""
    adapter = _telegram_adapter(
        monkeypatch, {"suggested_replies": True, "require_mention": True}
    )
    assert adapter._telegram_require_mention() is True

    option, handler = await _attach_then_tap(
        adapter, chat_id="-100777", chat_type="group", tapper_id=555, asker_id="555"
    )

    handler.assert_awaited_once()
    event = handler.await_args.args[0]
    assert event.text == option == "Show more"
    assert event.source.user_id == "555"
    assert event.source.chat_id == "-100777"
    assert "@accomy_bot" in (event.channel_prompt or "")


@pytest.mark.asyncio
async def test_another_group_member_tap_is_refused_and_the_asker_still_taps(monkeypatch):
    adapter = _telegram_adapter(monkeypatch, {"suggested_replies": True})
    options = ("Book it", "Show more")
    source = SimpleNamespace(chat_id="-100777", chat_type="group", thread_id=None, user_id="555")
    assert await adapter.attach_suggested_replies(source, "900", options)
    token = adapter._bot.edit_message_reply_markup.await_args.kwargs["reply_markup"].inline_keyboard[0][0].callback_data.split(":")[1]

    other = _tap_query(f"sg:{token}:1", tapper_id=777, chat_id="-100777", chat_type="group")
    await adapter._handle_suggestion_callback(other, other.data, adapter._callback_ctx(other))
    other.edit_message_reply_markup.assert_not_called()
    assert adapter._message_handler.await_count == 0

    asker = _tap_query(f"sg:{token}:1", tapper_id=555, chat_id="-100777", chat_type="group")
    await adapter._handle_suggestion_callback(asker, asker.data, adapter._callback_ctx(asker))
    await async_wait_until(lambda: adapter._message_handler.await_count >= 1, timeout=5)
    event = adapter._message_handler.await_args.args[0]
    assert event.text == "Show more"
    assert event.source.user_id == "555"
