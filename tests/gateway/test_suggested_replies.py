"""Suggested-reply footer parsing: what is stripped, what becomes options, what stays visible."""

from types import SimpleNamespace

import pytest

from gateway.suggested_replies import (
    final_message_id,
    hide_partial_suggestions,
    split_suggestions,
    strip_suggestions,
)


def test_last_line_footer_is_removed_and_yields_options():
    text, options = split_suggestions(
        "Here is the plan.\n\n[suggestions: Book it | Show more | Change dates]"
    )
    assert text == "Here is the plan."
    assert options == ("Book it", "Show more", "Change dates")


def test_crlf_and_trailing_whitespace_are_tolerated():
    text, options = split_suggestions("Answer.\r\n[suggestions: Yes | No]  \r\n")
    assert text == "Answer."
    assert options == ("Yes", "No")


def test_marker_matches_case_insensitively():
    _, options = split_suggestions("Answer.\n[Suggestions: Yes | No]")
    assert options == ("Yes", "No")


def test_one_usable_option_is_stripped_but_not_offered():
    text, options = split_suggestions("Answer.\n[suggestions: Only one]")
    assert text == "Answer."
    assert options == ()


def test_options_are_capped_at_four_and_forty_characters():
    footer = (
        "[suggestions: " + " | ".join(f"option {i} " + "x" * 60 for i in range(6)) + "]"
    )
    _, options = split_suggestions("Answer.\n" + footer)
    assert len(options) == 4
    assert all(len(option) <= 40 for option in options)


def test_empty_options_are_dropped_before_the_count():
    _, options = split_suggestions("Answer.\n[suggestions:  | Yes |  | No | ]")
    assert options == ("Yes", "No")


def test_unicode_options_survive():
    _, options = split_suggestions("答案。\n[suggestions: 订机票 | 换个日期]")
    assert options == ("订机票", "换个日期")


def test_marker_in_the_middle_of_the_text_stays_visible():
    text = "Use [suggestions: A | B] as a template.\nThat is all."
    assert split_suggestions(text) == (text, ())
    assert strip_suggestions(text) == text


def test_text_without_the_word_is_returned_unchanged():
    text = "Plain answer with [brackets] and | pipes."
    assert split_suggestions(text) == (text, ())


def test_strip_matches_split_on_the_text():
    text = "Answer.\n[suggestions: Yes | No]"
    assert strip_suggestions(text) == split_suggestions(text)[0] == "Answer."


def test_empty_text_is_safe():
    assert split_suggestions("") == ("", ())


def test_partial_opener_is_hidden_while_streaming_but_short_prefixes_are_not():
    assert hide_partial_suggestions("Answer.\n[sugg") == "Answer."
    assert hide_partial_suggestions("Answer.\n[s") == "Answer.\n[s"
    assert hide_partial_suggestions("Answer [sugg") == "Answer [sugg"


def test_complete_footer_line_is_hidden_while_streaming():
    assert hide_partial_suggestions("Answer.\n[suggestions: Yes | No") == "Answer."
    assert hide_partial_suggestions("Answer.\n  [suggestions: Yes | No]") == "Answer."


def test_final_message_id_prefers_the_last_chunk_of_a_split():
    split = SimpleNamespace(
        message_id="30", raw_response={"message_ids": ["10", "20", "30"]}
    )
    assert final_message_id(split) == "30"


def test_final_message_id_for_a_single_message():
    single = SimpleNamespace(message_id="42", raw_response=None)
    assert final_message_id(single) == "42"
    assert final_message_id(SimpleNamespace(message_id=None, raw_response={})) is None


# A footer is a line of its own, wherever it sits. The stream view and the final must agree on every
# text, so each case here is checked both ways.
TRICKY_TEXTS = [
    "Here are hotels.\n[suggestions: 看第一間 | 找更便宜的]\n\nNote: prices are indicative.",
    "One.\n[suggestions: A | B]\nTwo.\n[suggestions: C | D]",
    "To add buttons, write:\n```\n[suggestions: A | B]\n```\nThat is all.",
    "[suggestions: A | B]",
    "Answer.\r\n[suggestions: Yes | No]\r\n",
    "Answer.\n[suggestions: Yes | No]\n\n\n",
    "Answer.\n[suggestions: Yes | No",
    "Answer.\n[sugg",
    "Answer.\n[suggestions: Yes | No]\nmore prose\n[suggestions: Only one]",
    "Answer.\n[suggestions: A | B\nmore prose",
]


@pytest.mark.parametrize("text", TRICKY_TEXTS)
def test_stream_view_and_final_agree_on_every_text(text):
    final, _ = split_suggestions(text)
    assert hide_partial_suggestions(text).rstrip() == final.rstrip()


@pytest.mark.parametrize("text", TRICKY_TEXTS)
def test_no_prefix_streamed_character_by_character_shows_the_opener(text):
    for end in range(1, len(text) + 1):
        assert "[sugg" not in hide_partial_suggestions(text[:end]).lower(), text[:end]


def test_footer_mid_text_is_removed_and_the_prose_after_it_is_kept():
    text, options = split_suggestions(
        "Here are hotels.\n[suggestions: 看第一間 | 找更便宜的]\n\nNote: prices are indicative."
    )
    assert text == "Here are hotels.\n\nNote: prices are indicative."
    assert options == ("看第一間", "找更便宜的")


def test_two_footers_are_both_removed_and_the_last_one_gives_the_options():
    text, options = split_suggestions(
        "One.\n[suggestions: A | B]\nTwo.\n[suggestions: C | D]"
    )
    assert text == "One.\nTwo."
    assert options == ("C", "D")


def test_a_footer_line_in_a_fenced_block_is_removed_and_the_text_after_it_is_kept():
    text, _ = split_suggestions(
        "To add buttons, write:\n```\n[suggestions: A | B]\n```\nThat is all."
    )
    assert "[suggestions" not in text
    assert text.endswith("That is all.")


def test_streaming_a_mid_text_footer_keeps_the_text_after_it():
    text = "Here.\n[suggestions: A | B]\nAfter."
    assert hide_partial_suggestions(text) == "Here.\nAfter."


def test_an_unterminated_footer_line_mid_text_is_hidden_in_both_views():
    text = "Answer.\n[suggestions: Yes | No\nmore prose"
    assert split_suggestions(text) == ("Answer.\nmore prose", ())
    assert hide_partial_suggestions(text) == "Answer.\nmore prose"


def test_a_footer_with_one_usable_option_removes_the_line_and_gives_no_buttons():
    text, options = split_suggestions("Answer.\n[suggestions: A | B]\nMore.\n[suggestions: Solo]")
    assert text == "Answer.\nMore."
    assert options == ()
