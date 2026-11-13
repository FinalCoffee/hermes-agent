"""The ingress gate of the Accomy travel bot: turn limits per sender and in total, the text length cap, the
exact session-command exemptions, one notice per window, and the real gateway hook end to end."""

import asyncio
import functools
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from gateway.config import Platform
from gateway.platforms.event import MessageEvent
from gateway.session import SessionSource
from plugins.accomy_travel import _common, ingress

SKIP_MINUTE = {"action": "skip", "reason": "accomy_minute_limit"}
SKIP_DAY = {"action": "skip", "reason": "accomy_day_limit"}
SKIP_GLOBAL = {"action": "skip", "reason": "accomy_global_limit"}
SKIP_LENGTH = {"action": "skip", "reason": "accomy_length_limit"}


def _run(test):
    """This plugin's tests run without an asyncio plugin: each async test runs on its own loop."""

    @functools.wraps(test)
    def run(*args, **kwargs):
        return asyncio.run(test(*args, **kwargs))

    return run


class _Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture(autouse=True)
def clock(monkeypatch):
    clock = _Clock()
    monkeypatch.setattr(ingress, "_gate_limiter", _common.RateLimiter(clock=clock))
    monkeypatch.setattr(ingress, "_notice_limiter", _common.RateLimiter(clock=clock))
    return clock


class _Gateway:
    """The three things the hook uses from GatewayRunner: auth, the answering adapter, and its send. ``allowed``
    lists the user IDs auth admits; None admits everyone, as an open profile does."""

    def __init__(self, allowed=None) -> None:
        self.adapter = SimpleNamespace(send=AsyncMock())
        self.allowed = allowed

    def _is_user_authorized_for_source(self, source) -> bool:
        return self.allowed is None or source.user_id in self.allowed

    def _delivery_adapter_for(self, source):
        return self.adapter

    def _thread_metadata_for_source(self, source):
        return {"thread_id": source.thread_id} if source.thread_id else None


def _event(text="看第一間的房型", user="42", chat="42", platform=Platform.TELEGRAM) -> MessageEvent:
    source = SessionSource(platform=platform, user_id=user, chat_id=chat, user_name="t", chat_type="dm")
    return MessageEvent(text=text, message_id="m1", source=source)


async def _gate(event, gateway=None):
    return await ingress.gate_inbound(event=event, gateway=gateway)


async def _exhaust(sender="42", gateway=None):
    for _ in range(ingress.TURNS_PER_MINUTE):
        assert await _gate(_event(user=sender, chat=sender), gateway) is None


@_run
async def test_a_sender_is_held_to_its_turns_per_minute_and_admitted_again_after_the_window(clock):
    gateway = _Gateway()
    await _exhaust("42", gateway)

    assert await _gate(_event(user="42", chat="42"), gateway) == SKIP_MINUTE
    assert await _gate(_event(user="7", chat="7"), gateway) is None  # another sender is untouched

    clock.now += 60.0
    assert await _gate(_event(user="42", chat="42"), gateway) is None


@_run
async def test_a_sender_is_held_to_its_turns_per_day_across_minutes(clock):
    for _ in range(ingress.TURNS_PER_DAY // ingress.TURNS_PER_MINUTE):
        await _exhaust("42")
        clock.now += 61.0  # every minute is fresh, so only the day can refuse the next turn

    assert await _gate(_event(user="42", chat="42")) == SKIP_DAY

    clock.now += 86400.0
    assert await _gate(_event(user="42", chat="42")) is None


@_run
async def test_the_global_minute_limit_holds_when_many_accounts_each_stay_under_their_own(clock):
    per_account = ingress.GLOBAL_TURNS_PER_MINUTE // 5
    assert per_account < ingress.TURNS_PER_MINUTE  # every account is within its own limit

    for account in range(5):
        for _ in range(per_account):
            assert await _gate(_event(user=str(account), chat=str(account))) is None

    assert await _gate(_event(user="99", chat="99")) == SKIP_GLOBAL


@_run
async def test_text_longer_than_one_telegram_message_is_refused_and_the_limit_itself_is_not(clock):
    assert await _gate(_event(text="a" * (ingress.MAX_TEXT_CHARS + 1))) == SKIP_LENGTH
    assert await _gate(_event(text="a" * ingress.MAX_TEXT_CHARS)) is None


@_run
async def test_refused_long_text_never_spends_the_sender_budget(clock):
    for _ in range(ingress.TURNS_PER_MINUTE * 2):
        assert await _gate(_event(text="a" * (ingress.MAX_TEXT_CHARS + 1))) == SKIP_LENGTH

    await _exhaust("42")  # the full allowance is still there


@pytest.mark.parametrize("text", ["/stop", "/new", "/start", "/help", "/whoami", "/stop@AccomyAIBot", "/stop@OtherBot", " /help "])
@_run
async def test_session_commands_pass_uncounted_even_when_the_sender_is_limited(text):
    gateway = _Gateway()
    await _exhaust("42", gateway)

    assert await _gate(_event(text=text, user="42", chat="42"), gateway) is None


@pytest.mark.parametrize("text", ["/unknown text", "/stop please", "/start please", "/stop@OtherBot please", "/model gpt", "/STOP"])
@_run
async def test_any_other_slash_text_is_an_ordinary_counted_turn(text):
    gateway = _Gateway()
    await _exhaust("42", gateway)

    assert await _gate(_event(text=text, user="42", chat="42"), gateway) == SKIP_MINUTE


@_run
async def test_a_button_tap_is_an_ordinary_counted_turn():
    gateway = _Gateway()
    await _exhaust("42", gateway)

    assert await _gate(_event(text="找更便宜的", user="42", chat="42"), gateway) == SKIP_MINUTE


@_run
async def test_one_notice_is_sent_per_window_per_sender_and_never_per_dropped_message(clock):
    gateway = _Gateway()
    await _exhaust("42", gateway)

    for _ in range(10):
        assert await _gate(_event(user="42", chat="42"), gateway) == SKIP_MINUTE
    assert gateway.adapter.send.await_count == 1
    assert gateway.adapter.send.await_args.args[0] == "42"

    clock.now += 60.0
    await _exhaust("42", gateway)
    assert await _gate(_event(user="42", chat="42"), gateway) == SKIP_MINUTE
    assert gateway.adapter.send.await_count == 2


@_run
async def test_the_daily_notice_is_sent_once_for_the_whole_day(clock):
    gateway = _Gateway()
    for _ in range(ingress.TURNS_PER_DAY // ingress.TURNS_PER_MINUTE):
        await _exhaust("42", gateway)
        clock.now += 61.0
    assert await _gate(_event(user="42", chat="42"), gateway) == SKIP_DAY

    for _ in range(3):
        assert await _gate(_event(user="42", chat="42"), gateway) == SKIP_DAY
    assert gateway.adapter.send.await_count == 1


@_run
async def test_a_message_without_a_user_id_is_bucketed_by_chat_so_one_chat_cannot_starve_another():
    gateway = _Gateway()
    for _ in range(ingress.TURNS_PER_MINUTE):
        assert await _gate(_event(user=None, chat="c1"), gateway) is None

    assert await _gate(_event(user=None, chat="c1"), gateway) == SKIP_MINUTE
    assert await _gate(_event(user=None, chat="c2"), gateway) is None


@_run
async def test_a_message_with_no_sender_or_chat_still_has_a_bucket():
    for _ in range(ingress.TURNS_PER_MINUTE):
        assert await _gate(_event(user=None, chat=None)) is None

    assert await _gate(_event(user=None, chat=None)) == SKIP_MINUTE


@pytest.mark.parametrize("platform", [Platform.LOCAL, Platform.WEBHOOK, Platform.API_SERVER])
@_run
async def test_non_messaging_sources_are_never_limited(platform):
    for _ in range(ingress.TURNS_PER_MINUTE * 3):
        assert await _gate(_event(platform=platform)) is None


@_run
async def test_a_failed_notice_still_drops_the_message():
    gateway = _Gateway()
    gateway.adapter.send.side_effect = RuntimeError("telegram is down")
    await _exhaust("42", gateway)

    assert await _gate(_event(user="42", chat="42"), gateway) == SKIP_MINUTE


@_run
async def test_a_failed_adapter_lookup_still_drops_the_message():
    gateway = _Gateway()
    gateway._delivery_adapter_for = lambda source: 1 / 0
    await _exhaust("42", gateway)

    assert await _gate(_event(user="42", chat="42"), gateway) == SKIP_MINUTE


@_run
async def test_the_notice_is_sent_into_the_topic_the_message_came_from():
    gateway = _Gateway()
    await _exhaust("42", gateway)
    event = _event(user="42", chat="-1001")
    event.source.thread_id = "77"

    await _gate(event, gateway)

    assert gateway.adapter.send.await_args.kwargs["metadata"] == {"thread_id": "77"}


@_run
async def test_a_flood_of_session_commands_stops_being_answered_and_spends_no_turns(clock):
    for _ in range(ingress.COMMANDS_PER_MINUTE):
        assert await _gate(_event(text="/start")) is None

    assert await _gate(_event(text="/start")) == {"action": "skip", "reason": "accomy_command_flood"}
    await _exhaust("42")  # the model-turn allowance is untouched

    clock.now += 60.0
    assert await _gate(_event(text="/stop")) is None


@_run
async def test_a_sender_with_no_delivery_adapter_is_dropped_silently():
    gateway = SimpleNamespace(
        _is_user_authorized_for_source=lambda source: True, _delivery_adapter_for=lambda source: None)
    await _exhaust("42")

    assert await _gate(_event(user="42", chat="42"), gateway) == SKIP_MINUTE


@_run
async def test_the_registered_hook_on_a_real_gateway_drops_over_limit_turns_and_replies_once(accomy_manager, monkeypatch):
    """Through GatewayRunner._hm_pre_gateway_dispatch_hook, the plugin manager that loaded the plugin, the real
    auth check (closed profile: only user 42 is allowed) and the real adapter lookup, with a fake adapter."""
    from gateway.run import GatewayRunner
    from hermes_cli import plugins

    monkeypatch.setattr(plugins, "_plugin_manager", accomy_manager)
    for name in ("TELEGRAM_ALLOW_ALL_USERS", "GATEWAY_ALLOW_ALL_USERS"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("TELEGRAM_ALLOWED_USERS", "42")
    adapter = SimpleNamespace(send=AsyncMock())
    runner = object.__new__(GatewayRunner)
    runner.pairing_store = None
    runner.adapters = {Platform.TELEGRAM: adapter}

    for _ in range(ingress.TURNS_PER_MINUTE):
        event = _event(user="42", chat="42")
        assert await runner._hm_pre_gateway_dispatch_hook(event, event.source) is event

    event = _event(user="42", chat="42")
    assert await runner._hm_pre_gateway_dispatch_hook(event, event.source) is None
    event = _event(user="42", chat="42")
    assert await runner._hm_pre_gateway_dispatch_hook(event, event.source) is None

    adapter.send.assert_awaited_once()
    assert adapter.send.await_args.args[0] == "42"


@_run
async def test_strangers_on_a_closed_profile_cannot_use_up_the_budget_that_serves_the_allowlisted(clock):
    """Closed mode: the gate runs before auth, so a stranger flood must not refuse the one allowlisted sender."""
    gateway = _Gateway(allowed={"999"})
    for stranger in range(4):
        for _ in range(ingress.GLOBAL_TURNS_PER_MINUTE // 4 + 5):
            assert await _gate(_event(user=f"stranger{stranger}", chat=f"stranger{stranger}"), gateway) is None

    assert await _gate(_event(user="999", chat="999"), gateway) is None


@_run
async def test_refused_strangers_get_no_notice_and_draw_on_no_budget(clock):
    gateway = _Gateway(allowed={"999"})
    for _ in range(ingress.TURNS_PER_MINUTE * 3):
        assert await _gate(_event(text="a" * (ingress.MAX_TEXT_CHARS + 1), user="stranger", chat="stranger"),
                           gateway) is None

    gateway.adapter.send.assert_not_awaited()


@_run
async def test_a_turn_refused_by_the_global_limit_spends_nothing_from_the_sender_day(clock, monkeypatch):
    """A flood of fresh accounts holds the global minute for a long stretch while one victim keeps sending.
    Every refused turn must leave the victim's day budget intact once the flood stops."""
    monkeypatch.setattr(ingress, "GLOBAL_TURNS_PER_DAY", 10**9)  # this flood is about the minute total only
    for minute in range(ingress.TURNS_PER_DAY + 10):
        for account in range(ingress.GLOBAL_TURNS_PER_MINUTE):  # fresh one-message accounts fill the minute
            await _gate(_event(user=f"atk{minute}-{account}", chat=f"atk{minute}-{account}"))
        assert await _gate(_event(user="victim", chat="victim")) == SKIP_GLOBAL
        clock.now += 60.0

    for _ in range(ingress.TURNS_PER_MINUTE):
        assert await _gate(_event(user="victim", chat="victim")) is None


@_run
async def test_the_bot_wide_daily_total_holds_however_many_accounts_take_part(clock, monkeypatch):
    monkeypatch.setattr(ingress, "GLOBAL_TURNS_PER_DAY", 6)
    for account in range(6):
        assert await _gate(_event(user=str(account), chat=str(account))) is None
        clock.now += 61.0

    assert await _gate(_event(user="late", chat="late")) == {"action": "skip", "reason": "accomy_global_day_limit"}

    clock.now += 86400.0
    assert await _gate(_event(user="late", chat="late")) is None


@_run
async def test_notices_have_a_bot_wide_total_so_many_limited_accounts_are_not_all_answered(monkeypatch):
    monkeypatch.setattr(ingress, "GLOBAL_NOTICES_PER_MINUTE", 3)
    gateway = _Gateway()
    too_long = "a" * (ingress.MAX_TEXT_CHARS + 1)

    for account in range(5):
        assert await _gate(_event(text=too_long, user=str(account), chat=str(account)), gateway) == SKIP_LENGTH

    assert gateway.adapter.send.await_count == 3


@_run
async def test_session_commands_have_a_bot_wide_total(monkeypatch):
    monkeypatch.setattr(ingress, "GLOBAL_COMMANDS_PER_MINUTE", 4)
    for account in range(4):
        assert await _gate(_event(text="/help", user=str(account), chat=str(account))) is None

    assert await _gate(_event(text="/help", user="9", chat="9")) == {"action": "skip", "reason": "accomy_command_flood"}
