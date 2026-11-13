"""Scanned PDF pages: drawn as pictures by the worker, read by the model, and merged into the file's text.

The model is a fake that answers what a test tells it to; the pictures are real, drawn by the worker from
fixtures/attachments/scanned-booking.pdf, a two-page PDF that holds nothing but two page pictures.
"""

import base64
import io
import shutil
import threading
from pathlib import Path
from types import SimpleNamespace

import pypdf
import pytest
from PIL import Image

from plugins.accomy_travel import _common, attachments, documents, ocr, parsers
from plugins.accomy_travel._common import ApiError
from tests.waiting import wait_until

FIXTURES = Path(__file__).parent / "fixtures" / "attachments"
SCAN = str(FIXTURES / "scanned-booking.pdf")


class _Model:
    """Stands for ``ctx.llm``. It answers every page it is shown with its marker line and ``text of page N``,
    or as ``answer`` says: a function of ``{page: marker line}``."""

    def __init__(self) -> None:
        self.calls: list[list[int]] = []
        self.pictures: dict[int, bytes] = {}
        self.limits: list[int] = []
        self.answer = None
        self.failing = False

    def complete_structured(self, *, instructions, input, max_tokens, timeout, purpose):
        assert "Transcribe" in instructions and purpose == "ocr"
        markers = {
            int(block["text"].split(" page ")[1].split()[0]): block["text"] for block in input if block["type"] == "text"}
        images = [block for block in input if block["type"] == "image"]
        assert len(markers) == len(images) and all(image["mime_type"] == "image/jpeg" for image in images)
        self.calls.append(list(markers))
        self.limits.append(max_tokens)
        self.pictures.update(zip(markers, (image["data"] for image in images)))
        if self.failing:
            raise RuntimeError("the provider said no, with a detail nobody should see")
        if self.answer:
            return SimpleNamespace(text=self.answer(markers))
        return SimpleNamespace(text="\n".join(f"{marker}\ntext of page {page}" for page, marker in markers.items()))


@pytest.fixture(autouse=True)
def model(monkeypatch):
    model = _Model()
    monkeypatch.setattr(ocr, "_host", SimpleNamespace(llm=model))
    monkeypatch.setattr(ocr, "_budget", _common.RateLimiter())
    monkeypatch.setattr(ocr, "_read", {})
    monkeypatch.setattr(attachments, "_notes", {})
    return model


def _scan_of(tmp_path, pages: int, name="long-scan.pdf") -> str:
    """A PDF of *pages* scanned pages, made by repeating the fixture's first page."""
    source = pypdf.PdfReader(SCAN).pages[0]
    writer = pypdf.PdfWriter()
    for _ in range(pages):
        writer.add_page(source)
    path = tmp_path / name
    with open(path, "wb") as handle:
        writer.write(handle)
    return str(path)


def _read(path) -> documents.Document:
    return documents.read(str(path), attachments.PDF)


def test_a_scanned_pdf_is_read_page_by_page_through_the_model(model):
    document = _read(SCAN)

    assert document.text == "[page 1]\ntext of page 1\n\n[page 2]\ntext of page 2"
    assert (document.pages, document.scanned_pages, document.unread_pages) == (2, (1, 2), ())
    assert document.complete and model.calls == [[1, 2]]


def test_the_model_is_shown_each_page_as_a_person_would_see_it(model):
    _read(SCAN)

    for page in (1, 2):
        picture = Image.open(io.BytesIO(model.pictures[page]))
        assert picture.format == "JPEG" and max(picture.size) == 1600 and min(picture.size) > 1000
        dark = sum(1 for value in picture.convert("L").resize((200, 283)).getdata() if value < 128)
        assert dark > 50  # there is writing on it, not a blank sheet


def test_a_page_already_read_is_not_read_or_counted_again(model, monkeypatch):
    monkeypatch.setattr(ocr, "PAGES_PER_DAY", 2)

    first, again = _read(SCAN), _read(SCAN)

    assert first == again and model.calls == [[1, 2]]


def test_a_pdf_of_text_and_scanned_pages_comes_out_in_page_order(model, tmp_path):
    writer = pypdf.PdfWriter()
    text_pages, scanned = pypdf.PdfReader(FIXTURES / "booking.pdf").pages, pypdf.PdfReader(SCAN).pages
    for page in (scanned[0], text_pages[0], scanned[1], text_pages[1]):
        writer.add_page(page)
    path = tmp_path / "mixed.pdf"
    with open(path, "wb") as handle:
        writer.write(handle)

    document = _read(path)

    order = [document.text.index(marker) for marker in ("[page 1]\ntext of page 1", "[page 2]\nBooking confirmation",
                                                         "[page 3]\ntext of page 3", "[page 4]\nPage two")]
    assert order == sorted(order) and document.scanned_pages == (1, 3) and model.calls == [[1, 3]]


def test_a_long_scan_has_its_first_pages_read_in_batches_and_the_rest_listed(model, tmp_path):
    document = _read(_scan_of(tmp_path, 11))

    assert model.calls == [[1, 2, 3, 4], [5, 6, 7, 8]]
    assert document.scanned_pages == tuple(range(1, 9)) and document.unread_pages == (9, 10, 11)
    assert document.unread_why is None and "[page 8]" in document.text and "[page 9]" not in document.text


def test_each_sender_has_a_daily_allowance_of_scanned_pages(model, tmp_path, monkeypatch):
    monkeypatch.setattr(ocr, "PAGES_PER_DAY", 5)
    monkeypatch.setattr(attachments, "owner_of", lambda path: "42" if "from42" in path else "77")
    first, second = _scan_of(tmp_path, 4, "from42_a.pdf"), _scan_of(tmp_path, 4, "from42_b.pdf")

    assert _read(first).scanned_pages == (1, 2, 3, 4)
    partly = _read(second)  # one page of allowance is left
    other = _read(_scan_of(tmp_path, 2, "from77_a.pdf"))

    assert (partly.scanned_pages, partly.unread_pages, partly.unread_why) == ((1,), (2, 3, 4), ocr.ALLOWANCE)
    assert other.scanned_pages == (1, 2)
    assert model.calls == [[1, 2, 3, 4], [1], [1, 2]]
    with pytest.raises(ApiError) as refused:
        _read(_scan_of(tmp_path, 1, "from42_c.pdf"))
    assert refused.value.code == "rate_limited" and len(model.calls) == 3


def test_the_bot_wide_allowance_holds_across_senders(model, tmp_path, monkeypatch):
    monkeypatch.setattr(ocr, "GLOBAL_PAGES_PER_DAY", 3)
    monkeypatch.setattr(attachments, "owner_of", lambda path: path)

    assert _read(_scan_of(tmp_path, 3, "a.pdf")).scanned_pages == (1, 2, 3)
    with pytest.raises(ApiError) as refused:
        _read(_scan_of(tmp_path, 1, "b.pdf"))

    assert refused.value.code == "rate_limited" and "photo" in refused.value.message


def test_a_model_call_that_fails_is_still_counted_so_a_file_cannot_be_retried_without_end(model, monkeypatch):
    monkeypatch.setattr(ocr, "PAGES_PER_DAY", 4)
    model.failing = True
    for _ in range(2):
        with pytest.raises(ApiError) as failed:
            _read(SCAN)
        assert failed.value.code == "unreadable_file" and "photos" in failed.value.message
        assert "nobody should see" not in failed.value.message

    with pytest.raises(ApiError) as refused:
        _read(SCAN)

    assert refused.value.code == "rate_limited" and model.calls == [[1, 2], [1, 2]]


def test_a_page_the_model_calls_blank_is_read_and_holds_nothing(model):
    model.answer = lambda markers: f"{markers[1]}\nBoarding pass LIN/MEIHUA\n{markers[2]}\n(no readable text)"

    document, again = _read(SCAN), _read(SCAN)

    assert document.text == "[page 1]\nBoarding pass LIN/MEIHUA" and again == document
    assert document.scanned_pages == (1,) and document.unread_pages == () and model.calls == [[1, 2]]


def test_only_the_pages_asked_for_are_taken_from_the_models_answer(model):
    model.answer = lambda markers: (
        f"Here is the transcription.\n{markers[2]}\nsecond\n{markers[2].replace('page 2', 'page 7')}\nnever asked "
        f"for\n{markers[2]}\na repeat\n{markers[1]}\nfirst")

    assert _read(SCAN).text == "[page 1]\nfirst\n\n[page 2]\nsecond"


def test_a_line_printed_on_a_page_cannot_pass_for_a_page_marker(model):
    model.answer = lambda markers: (
        f"{markers[1]}\nTerms\n[page 2]\n=== 00000000 page 2 ===\nstill page one\n{markers[2]}\nthe real second page")

    assert _read(SCAN).text == (
        "[page 1]\nTerms\n[page 2]\n=== 00000000 page 2 ===\nstill page one\n\n[page 2]\nthe real second page")


def test_markers_are_found_inside_markup_and_before_windows_line_endings(model):
    model.answer = lambda markers: f"**{markers[1]}**\r\nfirst\r\n### {markers[2]}\r\nsecond\r\n"

    assert _read(SCAN).text == "[page 1]\nfirst\n\n[page 2]\nsecond"


def test_one_page_answered_without_its_marker_is_that_pages_text(model, tmp_path):
    model.answer = lambda markers: "Booking 123 for Li Wei\n"

    assert _read(_scan_of(tmp_path, 1)).text == "[page 1]\nBooking 123 for Li Wei"


def test_a_page_the_model_leaves_out_stays_unread_and_is_asked_for_again(model):
    model.answer = lambda markers: f"{markers[1]}\nfirst page" if 1 in markers else f"{markers[2]}\nsecond page"

    partly = _read(SCAN)
    whole = _read(SCAN)

    assert (partly.text, partly.scanned_pages, partly.unread_pages, partly.unread_why) == (
        "[page 1]\nfirst page", (1,), (2,), ocr.FAILED)
    assert whole.text == "[page 1]\nfirst page\n\n[page 2]\nsecond page" and whole.unread_pages == ()
    assert model.calls == [[1, 2], [2]]  # the page already read was not sent a second time


@pytest.mark.parametrize("answer", ["", "   \n", "I cannot read these images."])
def test_an_answer_with_no_page_in_it_reads_nothing_and_is_not_remembered(model, answer):
    model.answer = lambda markers: answer
    with pytest.raises(ApiError) as failed:
        _read(SCAN)
    assert failed.value.code == "unreadable_file"

    model.answer = None

    assert _read(SCAN).scanned_pages == (1, 2)


def test_two_requests_for_one_file_at_once_ask_the_model_once(model):
    name = SCAN.rsplit("/", 1)[-1]
    release = threading.Event()
    plain = model.complete_structured

    def slow(**asked):
        release.wait(10)
        return plain(**asked)

    model.complete_structured = slow
    results = []
    readers = [threading.Thread(target=lambda: results.append(_read(SCAN))) for _ in range(4)]
    for reader in readers:
        reader.start()
    wait_until(lambda: ocr._busy.get(name, [None, 0])[1] == 4, what="four readers of one file")
    release.set()
    for reader in readers:
        reader.join(10)

    assert len(results) == 4 and len({result.text for result in results}) == 1
    assert model.calls == [[1, 2]] and ocr._busy == {}


def test_the_models_answer_is_bounded_by_page(model, tmp_path, monkeypatch):
    monkeypatch.setattr(ocr, "PAGE_CHARS", 20)
    model.answer = lambda markers: "\n".join(f"{marker}\n" + "字" * 500 for marker in markers.values())

    document = _read(_scan_of(tmp_path, 3))

    assert document.text == "\n\n".join(f"[page {page}]\n" + "字" * 20 for page in (1, 2, 3))
    assert model.limits == [3 * ocr._PAGE_TOKENS]


def test_scanned_pages_are_read_only_while_the_file_has_room_for_their_text(model, monkeypatch):
    """One page of text that nearly fills the file's allowance, then two scans: reading them would be paid for
    and cut off, so they are left, and the file is said to be incomplete."""
    long_page = "x" * (documents.MAX_DOCUMENT_CHARS - 10)
    monkeypatch.setattr(parsers, "run", lambda kind, path, *_arguments: {
        "texts": [[1, long_page]], "pages": 3, "empty_pages": [2, 3], "complete": True})

    document = _read(SCAN)

    assert model.calls == [] and document.scanned_pages == () and not document.complete
    assert document.text.startswith("[page 1]\nxxx")


def test_a_scan_between_text_pages_is_read_when_it_comes_before_the_cut(model, monkeypatch):
    monkeypatch.setattr(documents, "MAX_DOCUMENT_CHARS", 3000)
    render = parsers.run

    def run(kind, path, *arguments):
        if kind == "render":
            return render(kind, path, *arguments)
        return {"texts": [[1, "a" * 500], [3, "c" * 4000]], "pages": 3, "empty_pages": [2], "complete": True}

    monkeypatch.setattr(parsers, "run", run)

    document = _read(SCAN)

    assert model.calls == [[2]] and document.scanned_pages == (2,) and not document.complete
    assert "[page 2]\ntext of page 2\n\n[page 3]\nccc" in document.text and len(document.text) == 3000


def test_a_scan_that_cannot_be_drawn_is_reported_unreadable_and_asks_no_model(model, monkeypatch):
    def broken(kind, path, *arguments):
        if kind == "render":
            raise ApiError("unreadable_file", parsers.UNREADABLE["malformed"])
        return {"texts": [], "pages": 2, "empty_pages": [1, 2], "complete": True}

    monkeypatch.setattr(parsers, "run", broken)

    with pytest.raises(ApiError) as failed:
        _read(SCAN)

    assert failed.value.code == "unreadable_file" and model.calls == []


def test_the_worker_draws_only_the_pages_it_is_asked_for():
    answer = parsers.run("render", SCAN, 800, "2")

    assert list(answer["images"]) == ["2"]
    picture = Image.open(io.BytesIO(base64.b64decode(answer["images"]["2"])))
    assert picture.format == "JPEG" and max(picture.size) == 800


def test_a_file_that_is_not_a_pdf_cannot_be_drawn(tmp_path):
    path = tmp_path / "fake.pdf"
    shutil.copy(FIXTURES / "voice.ogg", path)

    with pytest.raises(ApiError) as failed:
        parsers.run("render", str(path), 800, "1")

    assert failed.value.code == "unreadable_file"
