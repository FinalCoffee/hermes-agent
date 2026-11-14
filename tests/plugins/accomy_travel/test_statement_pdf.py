"""The PDF of an order's statement: what a reader finds on the page, and which fonts carry it.

The pages are read back with pypdf. ``fixtures/fonts/wqy-microhei-subset.ttf`` stands in for the wide font of the
image: the 28 characters these tests use, cut from WenQuanYi Micro Hei 0.2.0-beta (Apache License 2.0, see the
NOTICE beside it) with fontTools' pyftsubset."""

import io
import re
import unicodedata
from pathlib import Path

import pypdf
import pytest

from plugins.accomy_travel import statement_pdf
from plugins.accomy_travel.statement import Column, Line, Statement

FONT = Path(__file__).parent / "fixtures" / "fonts" / "wqy-microhei-subset.ttf"
REFERENCE = "ACF274KNM28JHV"
A4_POINTS = (595.28, 841.89)


@pytest.fixture
def wide_font(monkeypatch):
    """The test font in the place of the image's."""
    monkeypatch.setattr(statement_pdf, "WIDE_FONT_FILES", ((str(FONT), 0),))
    monkeypatch.setattr(statement_pdf, "_wide_glyphs", None)


@pytest.fixture
def no_wide_font(monkeypatch):
    """A host without the font package."""
    monkeypatch.setattr(statement_pdf, "WIDE_FONT_FILES", ())
    monkeypatch.setattr(statement_pdf, "_wide_glyphs", None)


def _page(*, lines=None, ordered_by=("Mei Lin", "mei@example.com"), travellers=("LIN Mei",),
          trip=("Singapore in November", "16–22 Nov 2026", "Trip TR8K2M4N6P8Q0R")) -> Statement:
    if lines is None:
        lines = (Line("TPE to SIN, SIN to TPE", ("Round trip", "TR897 · Mon 16 Nov · 08:05 TPE to 12:40 SIN · Economy"),
                      "16–22 Nov 2026", "1 passenger", "326.00"),)
    return Statement(
        reference=REFERENCE, issued="14 Nov 2026 UTC", status="Demonstration · paid", status_tone="good",
        parties=(Column("Ordered by", ordered_by), Column("Travellers", travellers),
                 Column("Booked through", ("Accomy AI Travel", "Demonstration service"))),
        lines=tuple(lines), currency="USD", total="326.00",
        summary=(Column("Payment", ("Demonstration payment made", "Nothing was charged")),
                 Column("Order", ("Demonstration order", "Not placed with a supplier", "Placed 14 Nov 2026 UTC")),
                 Column("Trip", trip)))


def _read(page: Statement) -> pypdf.PdfReader:
    return pypdf.PdfReader(io.BytesIO(statement_pdf.render(page)))


def _text(reader: pypdf.PdfReader, page: int = 0) -> str:
    """The words of a page on one line: a PDF keeps no line breaks worth comparing."""
    return " ".join(reader.pages[page].extract_text().split())


def _fonts(reader: pypdf.PdfReader) -> dict[str, bool]:
    """Each font of the first page by name, and whether its outlines are in the file."""
    found = {}
    for font in reader.pages[0]["/Resources"]["/Font"].values():
        font = font.get_object()
        found[str(font["/BaseFont"])] = "/FontFile2" in font.get("/FontDescriptor", {})
    return found


# ── The page ──────────────────────────────────────────────────────────────────


def test_the_statement_is_one_a4_page_that_holds_the_order(no_wide_font):
    reader = _read(_page())

    text = _text(reader)
    box = reader.pages[0].mediabox
    assert len(reader.pages) == 1
    assert (round(float(box.width), 2), round(float(box.height), 2)) == A4_POINTS
    for words in (REFERENCE, "Mei Lin", "mei@example.com", "LIN Mei", "TPE to SIN, SIN to TPE",
                  "TR897 · Mon 16 Nov · 08:05 TPE to 12:40 SIN · Economy", "16–22 Nov 2026", "1 passenger",
                  "USD 326.00", "Singapore in November", "Demonstration · paid", "Nothing was charged"):
        assert words in text, words
    assert REFERENCE in reader.metadata.title


# What no edit may take off the page, spelled out here and not read from the module.
NOTICE_ON_EVERY_PAGE = "Demonstration order. It is not a ticket or a booking"


def test_the_page_says_that_the_order_is_a_demonstration_and_what_it_is_not(no_wide_font):
    text = _text(_read(_page()))

    assert NOTICE_ON_EVERY_PAGE in text
    assert "no airline, hotel or organiser holds it, and nothing was charged." in text
    assert "This is not a ticket, a booking confirmation, a receipt or a tax invoice." in text
    assert "Demonstration: nothing was charged" in text  # under the total


def test_an_order_of_several_items_shows_a_subtotal_over_the_total(no_wide_font):
    hotel = Line("The Standard, Bangkok Mahanakhon", ("Deluxe King Room · 1 room · 2 guests",), "18–20 Nov 2026",
                 "2 nights", "360.00")
    one, two = _text(_read(_page())), _text(_read(_page(lines=(hotel, hotel))))

    assert "Subtotal" in two and "Subtotal" not in one


def test_a_long_order_runs_on_to_more_pages_that_each_carry_the_reference_and_the_notice(no_wide_font):
    # One item alone taller than a page: forty flights' worth of notes.
    endless = Line("TPE to SIN", tuple(f"TR{number:03} · Mon 16 Nov · 08:05 TPE to 12:40 SIN" for number in range(80)),
                   "16 Nov 2026", "1 passenger", "326.00")

    reader = _read(_page(lines=(endless, endless)))

    assert len(reader.pages) > 2
    for number in range(len(reader.pages)):
        text = _text(reader, number)
        assert f"{REFERENCE} · page {number + 1}" in text
        assert NOTICE_ON_EVERY_PAGE in text
    assert "TR079" in " ".join(_text(reader, number) for number in range(len(reader.pages)))


def test_nine_travellers_with_the_longest_names_an_order_takes_still_get_their_statement(no_wide_font):
    # 100 characters for each part of a name, without a space to wrap at: taller than a page as one block.
    names = tuple(f"{number}{'Z' * 99} {number}{'z' * 99}" for number in range(1, 10))
    assert "z" not in _text(_read(_page(travellers=("-",)))).lower()  # the page has no z of its own to miscount

    reader = _read(_page(travellers=names))

    everything = "".join(_text(reader, number) for number in range(len(reader.pages)))
    assert len(reader.pages) == 2
    # Every letter of every name: nothing is lost where the page turns.
    assert (everything.count("Z"), everything.count("z")) == (9 * 99, 9 * 99)


def test_an_amount_in_the_tens_of_millions_stays_on_one_line(no_wide_font):
    line = Line("TPE to SIN", ("One way",), "16 Nov 2026", "9 passengers", "12,345,678.90")

    assert "9 passengers 12,345,678.90" in _text(_read(_page(lines=(line,))))


# ── Text that came from a user or a supplier ──────────────────────────────────


def test_text_from_a_user_is_shown_as_typed_and_adds_nothing_to_the_page(no_wide_font, tmp_path):
    picture = tmp_path / "picture.png"
    picture.write_bytes(bytes.fromhex(
        "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c4890000000d49444154789c63f8cfc0f01f00050001"
        "ff5d7e6b8f0000000049454e44ae426082"))
    typed = f'<img src="{picture}" width="40" height="40"/> <a href="https://example.com/x">Grand</a> <b>Hotel</b> & Co'
    line = Line(typed, (typed,), "18–20 Nov 2026", "2 nights", "360.00")

    reader = _read(_page(lines=(line,), ordered_by=(typed,), travellers=(typed,), trip=(typed,)))

    page = reader.pages[0]
    assert _text(reader).count('<b>Hotel</b> & Co') == 5
    assert "/XObject" not in page["/Resources"]  # no picture was pulled in
    assert "/Annots" not in page  # and no link


def test_spaces_and_line_breaks_in_a_name_become_single_spaces(no_wide_font):
    assert "Mei Lin" in _text(_read(_page(ordered_by=("Mei \n\t  Lin",))))


def test_a_character_that_is_never_meant_to_show_is_not_drawn(no_wide_font):
    # A soft hyphen is in the standard fonts and a zero-width joiner is in some wide ones: neither is text.
    assert "Grand Hotel Central" in _text(_read(_page(ordered_by=("Gr\u00adand Hotel Cen\u200dtral",))))


# ── Characters beyond Latin ───────────────────────────────────────────────────


def test_chinese_japanese_korean_and_vietnamese_are_drawn_with_a_font_inside_the_file(wide_font):
    reader = _read(_page(travellers=("王 小明", "田中 太郎", "김 민준", "Nguyễn Mei"),
                         trip=("2026年11月16日至22日新加坡來回之旅", "東京 大阪 冬の旅")))

    text = _text(reader).replace(" ", "")
    for words in ("王小明", "田中太郎", "김민준", "NguyễnMei", "2026年11月16日至22日新加坡來回之旅", "東京大阪冬の旅"):
        assert words in text, words
    embedded = [name for name, inside in _fonts(reader).items() if inside]
    assert len(embedded) == 1 and "WenQuanYi" in embedded[0]


def test_a_letter_written_as_a_base_and_its_marks_is_drawn_as_the_one_letter_the_font_holds(wide_font):
    apart = unicodedata.normalize("NFD", "Nguyễn Mei")  # as text pasted on a Mac arrives: e and two marks

    assert "Nguyễn Mei" in _text(_read(_page(travellers=(apart,))))


def test_a_letter_that_no_font_holds_shows_as_a_question_mark_so_a_name_is_seen_to_be_missing(wide_font):
    reader = _read(_page(travellers=("محمد علي", "Țara 東京"), ordered_by=("สมชาย",)))

    text = _text(reader)
    assert "???? ???" in text and "?ara 東京" in text
    assert "?????" in text  # the marks over Thai letters are not letters: they leave nothing
    assert not any("Dingbats" in name for name in _fonts(reader))  # reportlab's own stand-in is a black box


def test_a_symbol_that_no_font_holds_is_left_out(wide_font):
    reader = _read(_page(trip=("Winter 🎄 trip to 東京 ✈️",)))

    assert "Winter trip to 東京 Trip" in _text(reader) or "Winter trip to 東京" in _text(reader)
    assert "?" not in _text(reader)
    assert not any("Dingbats" in name for name in _fonts(reader))


def test_a_host_without_the_wide_font_draws_the_latin_text_and_marks_the_rest(no_wide_font):
    reader = _read(_page(ordered_by=("王小明 Wang Hsiao-Ming",), trip=("Zoë's trip to 東京",)))

    text = _text(reader)
    assert "??? Wang Hsiao-Ming" in text and "Zoë's trip to ??" in text
    assert not any(inside for inside in _fonts(reader).values())
    assert not any("Dingbats" in name for name in _fonts(reader))


# ── The logo ──────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("svg", [
    '<svg viewBox="0 0 10 10"><path d="M0 0 A5 5 0 0 1 10 10Z"/></svg>',  # an arc
    '<svg viewBox="0 0 10 10"><path d="m0 0 l5 5z"/></svg>',  # relative commands
    '<svg viewBox="0 0 10 10"><path d="M0 0 L5"/></svg>',  # half a point
    '<svg viewBox="0 0 10 10"><rect width="10" height="10"/></svg>',  # no path at all
    '<svg width="10" height="10"><path d="M0 0 L5 5Z"/></svg>',  # no viewBox to scale by
])
def test_a_logo_file_the_page_cannot_draw_exactly_is_refused(svg, tmp_path, monkeypatch, no_wide_font):
    logo = tmp_path / "logo.svg"
    logo.write_text(svg, encoding="utf-8")
    monkeypatch.setattr(statement_pdf, "LOGO_FILE", logo)
    monkeypatch.setattr(statement_pdf, "_logo", None)

    with pytest.raises(ValueError, match="logo"):
        statement_pdf.render(_page())


def _curves(logo_paths: str, tmp_path, monkeypatch) -> int:
    """How many curves the page draws when its logo is an SVG with *logo_paths*."""
    logo = tmp_path / "logo.svg"
    logo.write_text(f'<svg viewBox="0 0 30 10">{logo_paths}</svg>', encoding="utf-8")
    monkeypatch.setattr(statement_pdf, "LOGO_FILE", logo)
    monkeypatch.setattr(statement_pdf, "_logo", None)
    drawing = _read(_page()).pages[0].get_contents().get_data()
    return len(re.findall(rb" c\s", drawing))


def test_the_logo_drawn_is_the_one_in_the_file(tmp_path, monkeypatch, no_wide_font):
    box = '<path d="M0 0 H30 V10 L0 10Z" fill="white"/>'
    leaf = '<path d="M5,5 C6 6 7 6 8 5 C9 4 9 3 8 2C7 1 6 1 5,2Z"/>'  # three curves, written as a design tool does

    assert _curves(box + leaf, tmp_path, monkeypatch) - _curves(box, tmp_path, monkeypatch) == 3
