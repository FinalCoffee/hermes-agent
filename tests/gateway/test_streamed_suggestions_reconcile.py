"""A streamed final that carries a suggested-reply footer, through the turn's reconcile branches.

The footer must never reach an edit (stale and plugin-transformed branches included), and the keyboard
must be built from the text the user is meant to see: the transformed final when a plugin rewrote it.
"""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from gateway.run_turn import GatewayTurnMixin
from gateway.stream_consumer import GatewayStreamConsumer

ANSWER = "Your flight leaves at 09:40."


def _streamed_consumer(delivered_text: str) -> tuple[GatewayStreamConsumer, MagicMock]:
    """A consumer whose preview is on screen as message ``m1`` with *delivered_text* recorded as the final."""
    adapter = MagicMock()
    adapter.REQUIRES_EDIT_FINALIZE = False
    adapter.MAX_MESSAGE_LENGTH = 4096
    adapter.send = AsyncMock(
        return_value=SimpleNamespace(success=True, message_id="m1")
    )
    adapter.edit_message = AsyncMock(
        return_value=SimpleNamespace(success=True, message_id="m1")
    )
    adapter.attach_suggested_replies = AsyncMock(return_value=True)
    consumer = GatewayStreamConsumer(adapter=adapter, chat_id="c1")
    asyncio.run(consumer._send_or_edit(delivered_text))
    consumer._mark_final_delivered(record=delivered_text)
    return consumer, adapter


def _run_mark(consumer, response: dict) -> dict:
    turn_ctx = SimpleNamespace(
        stream_consumer_holder=[consumer],
        source=SimpleNamespace(platform="telegram", chat_id="c1"),
        session_key="s1",
    )
    asyncio.run(
        GatewayTurnMixin()._run_agent_mark_streamed_delivery(response, turn_ctx)
    )
    return response


def test_confirmed_stream_with_footer_is_marked_delivered_and_gets_buttons_from_the_footer():
    footer_final = f"{ANSWER}\n[suggestions: Book it | Show more]"
    consumer, adapter = _streamed_consumer(ANSWER)
    response = _run_mark(consumer, {"final_response": footer_final})

    assert response["already_sent"] is True
    adapter.attach_suggested_replies.assert_awaited_once()
    _, message_id, options = adapter.attach_suggested_replies.await_args.args
    assert message_id == "m1"
    assert options == ("Book it", "Show more")


def test_stale_finalize_edit_strips_the_footer_before_it_reaches_the_platform():
    # The stream recorded a different text than the completed answer: the reconcile edit runs.
    consumer, adapter = _streamed_consumer("An older snapshot.")
    response = _run_mark(
        consumer, {"final_response": f"{ANSWER}\n[suggestions: Yes | No]"}
    )

    edits = [call.kwargs["content"] for call in adapter.edit_message.await_args_list]
    assert edits and all("suggestions" not in text for text in edits)
    assert edits[-1] == ANSWER
    assert response["already_sent"] is True
    assert adapter.attach_suggested_replies.await_args.args[2] == ("Yes", "No")


def test_plugin_transform_that_appends_a_marker_edits_the_visible_text_only():
    # A transform_llm_output hook appended the footer to a streamed final that had none.
    consumer, adapter = _streamed_consumer(ANSWER)
    transformed = f"{ANSWER}\n[suggestions: Book it | Change dates]"
    response = _run_mark(
        consumer, {"final_response": transformed, "response_transformed": True}
    )

    edits = [call.kwargs["content"] for call in adapter.edit_message.await_args_list]
    assert (
        edits and edits[-1] == ANSWER
    )  # same text as on screen: the platform treats it as a no-op
    adapter.send.assert_awaited_once()  # only the original preview send; no duplicate final
    assert response["already_sent"] is True
    assert adapter.attach_suggested_replies.await_args.args[1:] == (
        "m1",
        ("Book it", "Change dates"),
    )


def test_transformed_final_without_any_footer_retires_the_old_keyboard_with_no_options():
    consumer, adapter = _streamed_consumer(ANSWER)
    _run_mark(
        consumer,
        {"final_response": ANSWER + " Rebooked.", "response_transformed": True},
    )

    assert adapter.attach_suggested_replies.await_args.args[2] == ()
