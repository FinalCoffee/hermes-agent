"""The demo payment page an order links to (deploy/accomy/payment-demo), and what the bot tells and asks it.

The page charges nothing. It shows the order item by item, accepts only a public test card, and on Pay
sends the traveller a Telegram message, once. A link is a token signed with a key the page shares: the
order with its items, the amount and the chat to tell. It names no traveller. The page keeps one fact
per order, PAID or VOID: the bot voids an order it cancels, and asks whether an order was paid. Without
the key or the page's address an order simply carries no link and nothing is asked.

The bot has no way to hear that an order was paid, so the statement that is mailed after payment is left
with the page beforehand: the PDF and the contact's address, which the page keeps until the order is paid
or cancelled, or its link has expired, and mails on Pay. That is the one place a traveller's details go to the
page.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
import zlib

from . import _common
from .orderstore import store

SECRET_ENV = "ACCOMY_PAYMENT_LINK_SECRET"
URL_SETTING = "payment_url"
LINK_LIFE_SECONDS = 24 * 3600  # from the moment the order was placed, however often it is looked at
# What the page keeps for one order: its table holds 400 KB a row. The page holds the same number
# (_STATEMENT_BYTES in deploy/accomy/payment-demo/handler.py) and refuses anything larger.
STATEMENT_MAX_BYTES = 300 * 1024
STATEMENT_TIMEOUT = 8.0  # the order is placed already: its answer does not wait long for the page
_REQUEST_LIFE_SECONDS = 60
_settings = None  # the plugin context, kept to read this profile's settings when a link is made


def configure(ctx) -> None:
    global _settings
    _settings = ctx


def _page() -> tuple[str, str] | None:
    """The page's address and the shared key, when this profile has both."""
    key = _common.secret(SECRET_ENV)
    base = str(_settings.get_config(URL_SETTING, "") or "").rstrip("/") if _settings is not None else ""
    return (base, key) if key and base else None


def _token(key: str, claims: dict) -> str:
    packed = zlib.compress(json.dumps(claims, ensure_ascii=False, separators=(",", ":")).encode(), 9)
    body = base64.urlsafe_b64encode(packed).decode().rstrip("=")
    signature = hmac.new(key.encode(), f"v2.{body}".encode(), hashlib.sha256).digest()
    return f"v2.{body}.{base64.urlsafe_b64encode(signature).decode().rstrip('=')}"


def link(booking: store.Booking, items: list[dict], *, now: float | None = None) -> str | None:
    """The payment page for a confirmed order of this chat, showing *items* (the order's lines as the tools
    show them, with no traveller in them). None when there is nothing to pay, no page, or the link's day is over."""
    page = _page()
    expires = booking.created_at.timestamp() + LINK_LIFE_SECONDS
    if (page is None or booking.status != "CONFIRMED" or not booking.buyer.chat_ref
            or expires <= (now if now is not None else time.time())):
        return None
    base, key = page
    return f"{base}/p/" + _token(key, {
        "code": booking.code,
        "name": (booking.trip.title if booking.trip else booking.items[0].title)[:80],
        "amount": f"{booking.total_amount:,.2f}",
        "currency": booking.currency,
        "chat": booking.buyer.chat_ref,
        "exp": int(expires),
        "items": items,
    })


def _ask(method: str, route: str, act: str, booking: store.Booking, *, claims: dict | None = None,
         **request) -> str | None:
    """One signed request to the page about an order; the state it answers, or None when it cannot be reached.
    *request* goes to ``request_json`` as it is: the body, the wait, the budget."""
    page = _page()
    if page is None:
        return None
    base, key = page
    token = _token(key, {"code": booking.code, "act": act, "exp": int(time.time()) + _REQUEST_LIFE_SECONDS,
                         **(claims or {})})
    try:
        status, body = _common.request_json(method, f"{base}/{route}/{token}", **request)
    except _common.ApiError:
        return None
    return str(body.get("state")) if status == 200 and isinstance(body, dict) else None


def void(booking: store.Booking) -> str | None:
    """Tell the page the order is cancelled, so its link takes no payment. Answers PAID when it was paid first.

    Counted apart from the questions the bot asks the page: a void that the page never hears leaves a cancelled
    order payable, and its statement to be mailed, so a run of lookups must not use up its turn."""
    return _ask("POST", "v", "void", booking, budget_key="accomy-payment-page:void")


def state(booking: store.Booking) -> str | None:
    """PAID, VOID or UNPAID as the page knows it."""
    return _ask("GET", "s", "status", booking)


def keep_statement(booking: store.Booking, email: str, pdf: bytes) -> str | None:
    """Leave the order's statement with the page, to be mailed to *email* when the order is paid. Answers KEPT when
    the page will do so, OFF when it sends no mail, the order's state when that is no longer unpaid, and None
    when the page could not be told or the file is too large.

    The token signs the hash of what is sent, so the address and the file cannot be changed on the way, and
    carries the moment the payment link ends, after which the page keeps neither."""
    if len(pdf) > STATEMENT_MAX_BYTES:
        return None
    content = json.dumps({"email": email, "pdf": base64.b64encode(pdf).decode()}).encode()
    return _ask("POST", "e", "statement", booking, content=content, timeout=STATEMENT_TIMEOUT, claims={
        "sha": hashlib.sha256(content).hexdigest(),
        "until": int(booking.created_at.timestamp()) + LINK_LIFE_SECONDS})
