"""The text of a document a user sent: each kind is read the way its extension calls for.

The office files and the PDFs under fixtures/attachments were written by python-docx, openpyxl, python-pptx,
reportlab and pypdf: real files of each format, small enough to read whole.
"""

import sys
import time
from pathlib import Path

import pytest

from plugins.accomy_travel import attachments, documents
from plugins.accomy_travel._common import ApiError

FIXTURES = Path(__file__).parent / "fixtures" / "attachments"


def _read(path) -> documents.Document:
    return documents.read(str(path), attachments.kind_of(str(path)))


def _refusal(path) -> str:
    with pytest.raises(ApiError) as refused:
        _read(path)
    assert refused.value.code == "unreadable_file"
    return refused.value.message


def test_a_pdf_gives_its_text_page_by_page_chinese_included():
    document = _read(FIXTURES / "booking.pdf")

    assert document.pages == 2 and document.complete and document.empty_pages == ()
    first, second = document.text.split("[page 2]")
    assert first.startswith("[page 1]\nBooking confirmation ABC123")
    assert "Fare: USD 412.30 economy (Y)" in first
    assert "備註：請於起飛前兩小時抵達機場。" in first
    assert second.strip() == "Page two: cancellation is free until 10 MAR 2027."


def test_a_scanned_pdf_is_refused_with_the_way_out_a_photo():
    assert "photos" in _refusal(FIXTURES / "scan.pdf")


def test_a_password_protected_pdf_says_so():
    assert "password" in _refusal(FIXTURES / "locked.pdf")


def test_a_word_file_gives_its_heading_paragraphs_and_table():
    text = _read(FIXTURES / "itinerary.docx").text

    assert "Tokyo offsite itinerary" in text
    assert "出差目的：東京客戶拜訪。" in text
    assert "| 2027-03-12 | TPE-NRT CI100 08:50 | Shinjuku Granbell |" in text
    assert "Budget ceiling per night: 200 USD." in text


def test_a_spreadsheet_gives_every_sheet_and_shows_dates_as_dates():
    text = _read(FIXTURES / "travellers.xlsx").text

    assert "## Trip" in text and "## Notes" in text
    assert "| Lin Mei-hua | TPE | NRT | 2027-03-12 | 2027-03-15 | 3 | 600.5 |" in text
    assert "| 陳偉 | TPE | LHR | 2027-04-02 23:10:00 | 2027-04-09 | 7 | 2100 |" in text
    assert "window for long haul" in text


def test_a_presentation_gives_each_slide_in_order():
    text = _read(FIXTURES / "kickoff.pptx").text

    assert text.index("Paris kickoff 2027") < text.index("Dates: 14-16 June 2027") < text.index("Hotels")
    assert "與會者 12 人" in text


@pytest.mark.parametrize("name", ["ticket.pdf", "plan.docx", "costs.xlsx"])
def test_a_file_that_is_not_what_its_name_says_is_reported_damaged(tmp_path, name):
    path = tmp_path / name
    path.write_bytes(b"MZ\x90\x00 this is not a document " * 40)

    assert "damaged" in _refusal(path)


def test_a_csv_saved_on_a_chinese_windows_is_read_in_its_own_encoding(tmp_path):
    rows = "旅客,出發地,目的地,出發日期\n" + "".join(
        f"陳偉{number},台北,東京,2027年3月{number}日\n" for number in range(1, 21))
    path = tmp_path / "travellers.csv"
    path.write_bytes(rows.encode("big5"))

    assert _read(path).text == rows


def test_text_that_is_not_utf8_keeps_its_last_character(tmp_path):
    """The last byte being the only one that is not UTF-8 must not look like a read that ended mid-character."""
    text = "Réservation confirmée pour deux personnes à l'hôtel du Marché, arrivée prévue le 12 mars au café"
    path = tmp_path / "confirmation.txt"
    path.write_bytes((text + "é").encode("cp1252"))

    read = _read(path).text

    assert len(read) == len(text) + 1 and read.startswith("R") and "personnes" in read


def test_utf16_text_without_a_byte_order_mark_is_not_read_as_utf8_with_nuls(tmp_path):
    text = "Flight CI100 departs Taipei at 08:50 and lands in Tokyo at 13:15. Hotel: Shinjuku Granbell, 3 nights.\n" * 4
    path = tmp_path / "export.txt"
    path.write_bytes(text.encode("utf-16-le"))

    read = _read(path).text

    assert "\x00" not in read and "Shinjuku Granbell" in read


def test_utf8_text_loses_its_byte_order_mark_and_keeps_everything_else(tmp_path):
    path = tmp_path / "notes.txt"
    path.write_bytes("\ufeff住宿：新宿 3 晚\nflight: CI100".encode())

    document = _read(path)

    assert document.text == "住宿：新宿 3 晚\nflight: CI100" and document.complete


def test_text_longer_than_the_limit_is_cut_and_marked_incomplete_even_inside_a_character(tmp_path, monkeypatch):
    monkeypatch.setattr(documents, "MAX_DOCUMENT_CHARS", 10)
    monkeypatch.setattr(documents, "_MAX_TEXT_BYTES", 31)  # ends inside the eleventh three-byte character
    path = tmp_path / "long.txt"
    path.write_bytes(("東京" * 40).encode())

    document = _read(path)

    assert document.text == "東京" * 5 and not document.complete


def test_a_long_pdf_is_cut_at_the_limit_and_marked_incomplete(monkeypatch):
    whole = _read(FIXTURES / "booking.pdf").text
    monkeypatch.setattr(documents, "MAX_DOCUMENT_CHARS", 40)

    document = _read(FIXTURES / "booking.pdf")

    assert document.text == whole[:40] and not document.complete


def test_a_web_page_loses_its_tags_scripts_and_styles(tmp_path):
    path = tmp_path / "confirmation.html"
    path.write_text(
        "<html><head><title>x</title><style>p{color:red}</style></head><body>"
        "<script>steal()</script><h1>Hotel confirmed</h1><p>Check-in <b>12 March</b></p>"
        "<table><tr><td>Room</td><td>Twin</td></tr></table></body></html>")

    text = _read(path).text

    assert text.splitlines() == ["x", "Hotel confirmed", "Check-in 12 March", "| Room | Twin"]  # the title first


def test_a_web_page_that_never_closes_its_head_still_gives_its_body(tmp_path):
    path = tmp_path / "saved.html"
    path.write_text("<html><head><title>Booking</title><style>b{}</style><body><p>Hello there</p></body></html>")

    assert _read(path).text.splitlines() == ["Booking", "Hello there"]


def test_a_pdf_that_opens_with_hundreds_of_scanned_pages_still_gives_the_text_behind_them(tmp_path):
    import pypdf

    writer = pypdf.PdfWriter()
    for _ in range(320):
        writer.add_blank_page(width=595, height=842)
    for page in pypdf.PdfReader(FIXTURES / "booking.pdf").pages:
        writer.add_page(page)
    path = tmp_path / "long-scan.pdf"
    with open(path, "wb") as handle:
        writer.write(handle)

    document = _read(path)

    assert document.pages == 322 and document.complete
    assert document.text.startswith("[page 321]\nBooking confirmation ABC123")
    assert document.empty_pages == tuple(range(1, 321))


def test_a_saved_mail_gives_its_headers_and_body_and_names_what_is_attached(tmp_path):
    from email.message import EmailMessage

    mail = EmailMessage()
    mail["From"], mail["To"], mail["Subject"] = "airline@example.com", "lin@example.com", "Your e-ticket CI100"
    mail.set_content("Flight CI100 departs 08:50 on 12 March.\n")
    mail.add_attachment(b"%PDF-1.4", maintype="application", subtype="pdf", filename="eticket.pdf")
    path = tmp_path / "ticket.eml"
    path.write_bytes(mail.as_bytes())

    text = _read(path).text

    assert text.startswith("From: airline@example.com\nTo: lin@example.com\nSubject: Your e-ticket CI100")
    assert "Flight CI100 departs 08:50 on 12 March." in text
    assert text.endswith("Attached to the mail (not read): eticket.pdf")
    assert "%PDF" not in text


def test_a_mail_that_names_an_encoding_nobody_knows_is_reported_damaged(tmp_path):
    path = tmp_path / "odd.eml"
    path.write_bytes(b"From: a@example.com\nContent-Type: text/plain; charset=\"no-such-charset\"\n\n\xff\xfe body\n")

    assert "damaged" in _refusal(path)


def _worker(tmp_path, monkeypatch, source: str) -> None:
    script = tmp_path / "worker.py"
    script.write_text(source)
    monkeypatch.setattr(documents, "_WORKER", script)


def test_a_parse_that_runs_too_long_is_stopped(tmp_path, monkeypatch):
    _worker(tmp_path, monkeypatch, "import time\ntime.sleep(60)\n")
    monkeypatch.setattr(documents, "WORKER_TIMEOUT_SECONDS", 0.5)
    started = time.monotonic()

    message = _refusal(FIXTURES / "booking.pdf")

    assert "too large" in message and time.monotonic() - started < 10


def test_a_parser_that_dies_is_reported_as_a_damaged_file(tmp_path, monkeypatch):
    _worker(tmp_path, monkeypatch, "import os\nos._exit(139)\n")

    assert "damaged" in _refusal(FIXTURES / "itinerary.docx")


def test_the_parser_runs_in_another_process_with_the_interpreter_isolated(tmp_path, monkeypatch):
    _worker(tmp_path, monkeypatch, (
        "import json, os, sys\n"
        "json.dump({'text': f'{os.getpid()} {sys.flags.isolated} {sys.argv[1]}', 'complete': True}, sys.stdout)\n"))

    pid, isolated, kind = _read(FIXTURES / "booking.pdf").text.split()

    assert int(pid) != documents.os.getpid() and isolated == "1" and kind == "pdf"
    assert sys.flags.isolated == 0  # the test itself is not isolated, so the flag came from the command
