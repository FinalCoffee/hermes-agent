"""The PDF of an order's statement: how it is drawn.

One A4 document in Accomy's colours: the logo on a purple band, a notice that the order is a demonstration, who
ordered and who travels, the items with their dates and amounts, the total, and what the document is and is not.
Every word of an order comes ready in a ``Statement`` (statement.py): nothing here reads an order.

Text a user or a supplier wrote (a name, a trip's title, a hotel's name) is escaped before it becomes paragraph
markup, so it can never add a tag: reportlab's markup can pull in a picture from a path, or a link.

The standard PDF fonts draw Windows-1252 text and nothing else. Any other character is drawn with one wide font
of the host (``WIDE_FONT_FILES``, in the image through the Dockerfile), embedded in the document so that every
viewer shows it. For a character neither font holds, reportlab would draw a black box: a letter or a digit
becomes a question mark instead, so that a name in such a script is seen to be missing, and anything else, such
as an emoji, is left out.

This module loads reportlab, so tools_orders.py imports it when a statement is asked for, not with the plugin.
"""

from __future__ import annotations

import io
import logging
import re
import threading
import unicodedata
from pathlib import Path
from xml.sax.saxutils import escape

from reportlab.lib.colors import HexColor, white
from reportlab.lib.enums import TA_RIGHT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfgen.canvas import FILL_EVEN_ODD
from reportlab.platypus import BaseDocTemplate, Flowable, Frame, PageTemplate, Paragraph, Spacer, Table, TableStyle

from .statement import Column, Statement

logger = logging.getLogger(__name__)

PAGE_WIDTH, PAGE_HEIGHT = A4
MARGIN = 40.0
BAND_HEIGHT = 40.0
NOTICE_HEIGHT = 19.0
FOOTER_HEIGHT = 62.0
CONTENT_WIDTH = PAGE_WIDTH - 2 * MARGIN
CONTENT_TOP = BAND_HEIGHT + NOTICE_HEIGHT + 22

# Accomy's colours, from accomy.com: the purple of the logo and of its buttons, the night blue of its dark
# sections, and tints of them for boxes and rules. Amber marks what a reader must not miss.
BAND_FROM, BAND_TO = HexColor("#6961EA"), HexColor("#4C44CD")
BAND_LABEL = HexColor("#D9D6FA")
INK = HexColor("#252056")
SOFT = HexColor("#6F6C8F")
HAIRLINE = HexColor("#E2E0F0")
TINT, TINT_EDGE = HexColor("#F6F5FD"), HexColor("#DEDBF6")
AMBER_TINT, AMBER_INK = HexColor("#FDF3DF"), HexColor("#8A5300")
TONES = {
    "good": (HexColor("#E4F3EA"), HexColor("#1C7A45")),
    "open": (AMBER_TINT, AMBER_INK),
    "muted": (HexColor("#ECEBF3"), HexColor("#5B5878")),
}

TEXT, BOLD, MONO, MONO_BOLD = "Helvetica", "Helvetica-Bold", "Courier", "Courier-Bold"
WIDE = "AccomyWide"
# (file, face) of a TrueType font that holds Chinese, Japanese, Korean and accented Latin: Debian's
# fonts-wqy-microhei. reportlab embeds TrueType outlines only, which rules out the Noto CJK fonts.
WIDE_FONT_FILES: tuple[tuple[str, int], ...] = (("/usr/share/fonts/truetype/wqy/wqy-microhei.ttc", 0),)
LOGO_FILE = Path(__file__).parent / "assets" / "accomy-logo.svg"
LOGO_HEIGHT = 15.0

DOCUMENT_TITLE = "Order statement and itinerary"
DEMO_BANNER = ("Demonstration order. It is not a ticket or a booking: "
               "no airline, hotel or organiser holds it, and nothing was charged.")
WHAT_IT_IS = (
    "<b>What this document is.</b> A record of a demonstration order made with Accomy AI Travel: the itinerary "
    "and the prices as they were shown when the order was placed.")
WHAT_IT_IS_NOT = (
    "<b>What it is not.</b> This is not a ticket, a booking confirmation, a receipt or a tax invoice. The order "
    "was not placed with any airline, hotel or event organiser, and nothing was charged.")
FOOTER_LINES = (
    "Accomy AI Travel · accomy.com",
    "Generated automatically, so no signature is required. Questions about this order: ask the assistant in the "
    "chat it was placed in.",
)

# reportlab keeps its fonts in one registry for the process, and a font keeps state for each document being
# built. Tool handlers run on a thread pool, so one statement is drawn at a time.
_lock = threading.Lock()
_wide_glyphs: frozenset[int] | None = None
_logo: tuple[float, float, tuple] | None = None


# ---- text ------------------------------------------------------------------------------------------

def _wide() -> frozenset[int]:
    """The characters the wide font draws. It is registered on first use; a host without the file has none."""
    global _wide_glyphs
    if _wide_glyphs is None:
        found: frozenset[int] = frozenset()
        for path, face in WIDE_FONT_FILES:
            if Path(path).is_file():
                font = TTFont(WIDE, path, subfontIndex=face)
                pdfmetrics.registerFont(font)
                found = frozenset(font.face.charToGlyph)
                break
        else:
            logger.warning("accomy statement: no wide font on this host, so only Latin text is drawn")
        _wide_glyphs = found
    return _wide_glyphs


def _standard(char: str) -> bool:
    """Whether the standard fonts draw *char*."""
    try:
        char.encode("cp1252")
    except UnicodeEncodeError:
        return False
    return True


def markup(text: str) -> str:
    """*text* as paragraph markup that shows it literally: escaped, and each run in a font that holds it."""
    wide = _wide()
    pieces: list[str] = []
    run: list[str] = []
    run_is_wide = False

    def close_run() -> None:
        if run:
            piece = escape("".join(run))
            pieces.append(f'<font name="{WIDE}">{piece}</font>' if run_is_wide else piece)
            run.clear()

    # Composed forms first: a font holds "ễ" as one character, and reportlab does not join a letter to its marks.
    for char in " ".join(unicodedata.normalize("NFC", text).split()):
        if not char.isprintable():
            continue  # a soft hyphen or a zero-width joiner has a glyph in some fonts, and is never meant to show
        if _standard(char):
            needs_wide = False
        elif ord(char) in wide:
            needs_wide = True
        elif unicodedata.category(char)[0] in "LN":
            char, needs_wide = "?", False
        else:
            continue
        if needs_wide != run_is_wide:
            close_run()
            run_is_wide = needs_wide
        run.append(char)
    close_run()
    return "".join(pieces)


def _style(name: str, font: str, size: float, colour, leading: float, **more) -> ParagraphStyle:
    # CJK wrapping may break a line between any two characters: a title in Chinese has no spaces to break at.
    return ParagraphStyle(name, fontName=font, fontSize=size, textColor=colour, leading=leading, wordWrap="CJK", **more)


TITLE = _style("title", BOLD, 11.5, INK, 14)
REFERENCE = _style("reference", MONO_BOLD, 9.5, INK, 13, alignment=TA_RIGHT)
STRONG = _style("strong", BOLD, 9.5, INK, 13)
BODY = _style("body", TEXT, 9.5, INK, 13)
NOTE = _style("note", TEXT, 8.2, SOFT, 11.5)
CELL = _style("cell", MONO, 8.8, INK, 12)
CELL_RIGHT = _style("cell-right", MONO, 8.8, INK, 12, alignment=TA_RIGHT)
SUM_LABEL = _style("sum-label", TEXT, 9.5, INK, 13)
SUM_VALUE = _style("sum-value", MONO, 8.8, INK, 13, alignment=TA_RIGHT)
TOTAL_LABEL = _style("total-label", BOLD, 13, INK, 17)
TOTAL_VALUE = _style("total-value", BOLD, 13, INK, 17, alignment=TA_RIGHT)
SMALL = _style("small", TEXT, 7.8, INK, 11.5)


class Label(Flowable):
    """A small heading in spaced capitals, as over each block of the page."""

    SIZE, SPACING = 5.6, 1.1

    def __init__(self, text: str, *, right: bool = False) -> None:
        super().__init__()
        self.text, self.right = text.upper(), right
        self.width = 0.0

    def wrap(self, avail_width, avail_height):
        self.width = avail_width
        return avail_width, self.SIZE + 3

    def draw(self) -> None:
        length = pdfmetrics.stringWidth(self.text, MONO, self.SIZE) + self.SPACING * (len(self.text) - 1)
        line = self.canv.beginText(self.width - length if self.right else 0, 2)
        line.setFont(MONO, self.SIZE)
        line.setCharSpace(self.SPACING)
        line.setFillColor(SOFT)
        line.textOut(self.text)
        self.canv.drawText(line)


class Pill(Flowable):
    """The order's status in a rounded box against the right edge."""

    SIZE, PAD, HEIGHT = 7.2, 9.0, 16.0

    def __init__(self, text: str, tone: str) -> None:
        super().__init__()
        self.text = text
        self.fill, self.ink = TONES[tone]
        self.width = 0.0

    def wrap(self, avail_width, avail_height):
        self.width = avail_width
        return avail_width, self.HEIGHT

    def draw(self) -> None:
        box = pdfmetrics.stringWidth(self.text, BOLD, self.SIZE) + 2 * self.PAD
        left = self.width - box
        self.canv.setFillColor(self.fill)
        self.canv.roundRect(left, 0, box, self.HEIGHT, self.HEIGHT / 2, stroke=0, fill=1)
        self.canv.setFillColor(self.ink)
        self.canv.setFont(BOLD, self.SIZE)
        self.canv.drawString(left + self.PAD, (self.HEIGHT - self.SIZE) / 2 + 1.2, self.text)


# ---- the logo --------------------------------------------------------------------------------------

_VIEWBOX_RE = re.compile(r'viewBox="0 0 ([0-9.]+) ([0-9.]+)"')
_PATH_RE = re.compile(r'<path[^>]*\sd="([^"]+)"')
_UNDRAWN_RE = re.compile(r"[^MLHVCZ0-9eE.,\s+-]")
_STEP_RE = re.compile(r"([MLHVCZ])([^MLHVCZ]*)")
_NUMBER_RE = re.compile(r"[-+]?[0-9]*\.?[0-9]+(?:[eE][-+]?[0-9]+)?")
_NUMBERS_PER_STEP = {"M": 2, "L": 2, "H": 1, "V": 1, "C": 6}


def _shape(data: str) -> tuple[tuple[str, tuple[float, ...]], ...]:
    """One path of the logo as moves (M), lines (L), curves (C) and closes (Z), every point in full."""
    if _UNDRAWN_RE.search(data):
        raise ValueError("the logo's paths may use the commands M, L, H, V, C and Z only")
    steps: list[tuple[str, tuple[float, ...]]] = []
    x = y = 0.0
    for command, rest in _STEP_RE.findall(data):
        if command == "Z":
            steps.append(("Z", ()))
            continue
        numbers = [float(number) for number in _NUMBER_RE.findall(rest)]
        size = _NUMBERS_PER_STEP[command]
        if not numbers or len(numbers) % size:
            raise ValueError(f"the logo has a path command {command} with the wrong number of coordinates")
        for start in range(0, len(numbers), size):
            values = numbers[start:start + size]
            if command == "H":
                x = values[0]
                steps.append(("L", (x, y)))
            elif command == "V":
                y = values[0]
                steps.append(("L", (x, y)))
            else:
                x, y = values[-2], values[-1]
                # After a move, further pairs of the same command are lines.
                steps.append(("L" if command == "M" and start else command, tuple(values)))
    return tuple(steps)


def _logo_shapes() -> tuple[float, float, tuple]:
    """The logo file as (width, height, shapes).

    The file is an SVG as a design tool exports the brand's logo: a viewBox from 0 0 and paths in absolute
    commands. A file drawn any other way is refused, not drawn wrong."""
    global _logo
    if _logo is None:
        source = LOGO_FILE.read_text(encoding="utf-8-sig")
        size = _VIEWBOX_RE.search(source)
        shapes = tuple(_shape(data) for data in _PATH_RE.findall(source))
        if size is None or not shapes:
            raise ValueError("the logo needs a viewBox that starts at 0 0 and at least one path")
        _logo = (float(size.group(1)), float(size.group(2)), shapes)
    return _logo


def _draw_logo(canvas, left: float, bottom: float, height: float) -> float:
    """Draw the logo in white, *height* tall, its lower left corner at (left, bottom). Returns its width."""
    width, tall, shapes = _logo_shapes()
    scale = height / tall
    canvas.saveState()
    canvas.translate(left, bottom + height)
    canvas.scale(scale, -scale)  # an SVG's y axis points down, a PDF's up
    canvas.setFillColor(white)
    for steps in shapes:
        path = canvas.beginPath()
        draw = {"M": path.moveTo, "L": path.lineTo, "C": path.curveTo, "Z": path.close}
        for command, values in steps:
            draw[command](*values)
        # Even-odd leaves the inside of a letter such as "o" open, whichever way its outlines run.
        canvas.drawPath(path, stroke=0, fill=1, fillMode=FILL_EVEN_ODD)
    canvas.restoreState()
    return width * scale


# ---- the page --------------------------------------------------------------------------------------

def _furniture(statement: Statement):
    """What every page carries around the statement: the band with the logo, the notice, and the footer."""

    def draw(canvas, doc) -> None:
        top = PAGE_HEIGHT - BAND_HEIGHT
        canvas.saveState()
        band = canvas.beginPath()
        band.rect(0, top, PAGE_WIDTH, BAND_HEIGHT)
        canvas.clipPath(band, stroke=0, fill=0)
        canvas.linearGradient(0, top, PAGE_WIDTH, top, (BAND_FROM, BAND_TO))
        canvas.restoreState()

        logo_bottom = top + (BAND_HEIGHT - LOGO_HEIGHT) / 2
        logo_width = _draw_logo(canvas, MARGIN, logo_bottom, LOGO_HEIGHT)
        canvas.saveState()  # the letter spacing of the tag must not reach the lines drawn after it
        tag = canvas.beginText(MARGIN + logo_width + 12, logo_bottom + 3.4)
        tag.setFont(MONO, 5.6)
        tag.setCharSpace(1.3)
        tag.setFillColor(BAND_LABEL)
        tag.textOut("AI TRAVEL")
        canvas.drawText(tag)
        canvas.restoreState()

        canvas.saveState()
        canvas.setFillColor(AMBER_TINT)
        canvas.rect(0, top - NOTICE_HEIGHT, PAGE_WIDTH, NOTICE_HEIGHT, stroke=0, fill=1)
        canvas.setFillColor(AMBER_INK)
        canvas.setFont(BOLD, 7.2)
        canvas.drawString(MARGIN, top - NOTICE_HEIGHT + 6.4, DEMO_BANNER)

        canvas.setStrokeColor(HAIRLINE)
        canvas.setLineWidth(0.8)
        canvas.line(MARGIN, FOOTER_HEIGHT - 10, PAGE_WIDTH - MARGIN, FOOTER_HEIGHT - 10)
        canvas.setFillColor(SOFT)
        canvas.setFont(TEXT, 6.6)
        canvas.drawString(MARGIN, FOOTER_HEIGHT - 22, FOOTER_LINES[0])
        canvas.drawString(MARGIN, FOOTER_HEIGHT - 31, FOOTER_LINES[1])
        canvas.drawRightString(PAGE_WIDTH - MARGIN, FOOTER_HEIGHT - 22, f"{statement.reference} · page {doc.page}")
        canvas.restoreState()

    return draw


_FLUSH = [("VALIGN", (0, 0), (-1, -1), "TOP"), ("LEFTPADDING", (0, 0), (-1, -1), 0),
          ("RIGHTPADDING", (0, 0), (-1, -1), 0), ("TOPPADDING", (0, 0), (-1, -1), 0),
          ("BOTTOMPADDING", (0, 0), (-1, -1), 0)]


def _head(statement: Statement) -> Table:
    left = [Label("Document"), Spacer(0, 4), Paragraph(DOCUMENT_TITLE, TITLE)]
    right = [Label("Reference", right=True), Spacer(0, 4), Paragraph(markup(statement.reference), REFERENCE),
             Spacer(0, 2), Label(f"Issued {statement.issued}", right=True), Spacer(0, 6),
             Pill(statement.status, statement.status_tone)]
    table = Table([[left, right]], colWidths=[CONTENT_WIDTH * 0.55, CONTENT_WIDTH * 0.45])
    table.setStyle(TableStyle([
        *_FLUSH, ("BOTTOMPADDING", (0, 0), (-1, -1), 12), ("LINEBELOW", (0, 0), (-1, -1), 1.2, INK)]))
    return table


def _columns(columns: tuple[Column, Column, Column]) -> Table:
    cells = []
    for column in columns:
        cell: list = [Label(column.label), Spacer(0, 3)]
        cell += [Paragraph(markup(line), STRONG if index == 0 else BODY) for index, line in enumerate(column.lines)]
        cells.append(cell)
    # Nine travellers with the longest names the order takes are taller than a page: the row may split.
    table = Table([cells], colWidths=[CONTENT_WIDTH * 0.35, CONTENT_WIDTH * 0.35, CONTENT_WIDTH * 0.30], splitInRow=1)
    table.setStyle(TableStyle([*_FLUSH, ("RIGHTPADDING", (0, 0), (-1, -1), 14)]))
    return table


def _items(statement: Statement) -> Table:
    rows: list[list] = [[Label("Description"), Label("Dates"), Label("Quantity"), Label("Amount", right=True)]]
    for line in statement.lines:
        description = [Paragraph(markup(line.title), STRONG), *(Paragraph(markup(note), NOTE) for note in line.notes)]
        rows.append([description, Paragraph(markup(line.dates), CELL), Paragraph(markup(line.quantity), CELL),
                     Paragraph(markup(line.amount), CELL_RIGHT)])
    widths = [CONTENT_WIDTH * 0.48, CONTENT_WIDTH * 0.20, CONTENT_WIDTH * 0.17, CONTENT_WIDTH * 0.15]
    # A long order runs on to the next page under the same headings, and so does one item taller than a page.
    table = Table(rows, colWidths=widths, repeatRows=1, splitInRow=1)
    table.setStyle(TableStyle([
        *_FLUSH, ("RIGHTPADDING", (0, 0), (-2, -1), 10),
        ("BOTTOMPADDING", (0, 0), (-1, 0), 6), ("LINEBELOW", (0, 0), (-1, 0), 1.2, INK),
        ("TOPPADDING", (0, 1), (-1, -1), 8), ("BOTTOMPADDING", (0, 1), (-1, -1), 9),
        ("LINEBELOW", (0, 1), (-1, -1), 0.8, HAIRLINE),
    ]))
    return table


def _total(statement: Statement) -> Table:
    width = CONTENT_WIDTH * 0.43
    amount = markup(f"{statement.currency} {statement.total}")
    rows = [[Paragraph("Total", TOTAL_LABEL), Paragraph(amount, TOTAL_VALUE)],
            [Paragraph("Demonstration: nothing was charged", NOTE), ""]]
    if len(statement.lines) > 1:
        rows.insert(0, [Paragraph("Subtotal", SUM_LABEL), Paragraph(amount, SUM_VALUE)])
    total_row = len(rows) - 2
    sums = Table(rows, colWidths=[width * 0.45, width * 0.55])
    sums.setStyle(TableStyle([
        *_FLUSH, ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("TOPPADDING", (0, 0), (-1, total_row), 6), ("BOTTOMPADDING", (0, 0), (-1, total_row), 6),
        ("LINEABOVE", (0, total_row), (-1, total_row), 1.2, INK), ("SPAN", (0, -1), (-1, -1)),
    ]))
    table = Table([["", sums]], colWidths=[CONTENT_WIDTH - width, width])
    table.setStyle(TableStyle([*_FLUSH, ("TOPPADDING", (0, 0), (-1, -1), 4)]))
    return table


def _explanation() -> Table:
    box = Table([[[Paragraph(WHAT_IT_IS, SMALL), Spacer(0, 4), Paragraph(WHAT_IT_IS_NOT, SMALL)]]],
                colWidths=[CONTENT_WIDTH])
    box.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), TINT), ("BOX", (0, 0), (-1, -1), 0.8, TINT_EDGE),
        ("ROUNDEDCORNERS", [6, 6, 6, 6]),
        ("LEFTPADDING", (0, 0), (-1, -1), 11), ("RIGHTPADDING", (0, 0), (-1, -1), 11),
        ("TOPPADDING", (0, 0), (-1, -1), 9), ("BOTTOMPADDING", (0, 0), (-1, -1), 10),
    ]))
    return box


def render(statement: Statement) -> bytes:
    """The statement as the bytes of a PDF file."""
    with _lock:
        out = io.BytesIO()
        frame = Frame(MARGIN, FOOTER_HEIGHT, CONTENT_WIDTH, PAGE_HEIGHT - CONTENT_TOP - FOOTER_HEIGHT,
                      leftPadding=0, rightPadding=0, topPadding=0, bottomPadding=0)
        doc = BaseDocTemplate(
            out, pagesize=A4, title=f"Accomy order statement {statement.reference}", author="Accomy AI Travel",
            subject="Demonstration order", creator="Accomy AI Travel")
        doc.addPageTemplates(PageTemplate(id="statement", frames=[frame], onPage=_furniture(statement)))
        doc.build([
            _head(statement), Spacer(0, 12), _columns(statement.parties), Spacer(0, 22),
            _items(statement), _total(statement), Spacer(0, 26), _columns(statement.summary), Spacer(0, 22),
            _explanation(),
        ])
        return out.getvalue()
