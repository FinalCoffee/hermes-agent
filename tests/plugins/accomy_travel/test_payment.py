"""The demo payment link an order carries and the page behind it, exercised together without AWS."""

import base64
import importlib.util
import json
import shutil
import sys
import time
from datetime import UTC, datetime
from decimal import Decimal
from email import message_from_bytes, policy
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest

from hermes_cli import plugins as pmod
from plugins.accomy_travel import _common, payment
from plugins.accomy_travel.orderstore import store

REPO = Path(__file__).resolve().parents[3]
KEY = "k" * 64
SENDER = "statements@mail.example"
PDF = b"%PDF-1.4 the statement of the order"
ITEMS = [
    {"kind": "HOTEL", "title": "Park Hotel Tokyo / Deluxe King", "quantity": 1, "amount": "420.00",
     "details": {"hotel": "Park Hotel Tokyo", "check_in": "2027-03-01", "check_out": "2027-03-04", "nights": 3}},
    {"kind": "EVENT_TICKET", "title": "Final / Category 1", "quantity": 2, "amount": "553.81",
     "supplier_price": {"amount": "418.20", "currency": "GBP"}, "details": {"event": "Final", "tickets": 2}},
]


class _Taken(Exception):
    pass


class _Table:
    """The one DynamoDB behaviour the page relies on: a put that refuses a code that is already there."""

    meta = SimpleNamespace(client=SimpleNamespace(exceptions=SimpleNamespace(ConditionalCheckFailedException=_Taken)))

    def __init__(self):
        self.rows = {}

    def get_item(self, Key, ConsistentRead=False):
        return {"Item": self.rows[Key["code"]]} if Key["code"] in self.rows else {}

    def put_item(self, Item, ConditionExpression=None):
        if ConditionExpression and Item["code"] in self.rows:
            raise _Taken
        self.rows[Item["code"]] = Item

    def delete_item(self, Key):
        self.rows.pop(Key["code"], None)


@pytest.fixture
def page(monkeypatch):
    """The Lambda handler of deploy/accomy/payment-demo with its table, its secrets, Telegram and the mail service
    replaced. It is set up to send mail from SENDER; ``page.mailed`` holds what it handed to the mail service."""
    boto3, botocore, config = ModuleType("boto3"), ModuleType("botocore"), ModuleType("botocore.config")
    table = _Table()
    boto3.client = lambda name, config=None: SimpleNamespace(get_parameter=lambda **k: {"Parameter": {"Value": KEY}})
    boto3.resource = lambda name, config=None: SimpleNamespace(Table=lambda table_name: table)
    config.Config = lambda **waits: waits
    for name, module in (("boto3", boto3), ("botocore", botocore), ("botocore.config", config)):
        monkeypatch.setitem(sys.modules, name, module)
    for name in ("LINK_SECRET_PARAM", "BOT_TOKEN_PARAM", "STATE_TABLE"):
        monkeypatch.setenv(name, name)
    monkeypatch.setenv("STATEMENT_SENDER", SENDER)
    spec = importlib.util.spec_from_file_location("accomy_payment_page", REPO / "deploy/accomy/payment-demo/handler.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.sent, module.mailed, module.rows = [], [], table.rows
    monkeypatch.setattr(module, "_send_telegram", lambda chat, message: module.sent.append((chat, message)))
    monkeypatch.setattr(module, "_ses", lambda: SimpleNamespace(send_email=lambda **mail: module.mailed.append(mail)))
    return module


def _call(page, method, url, lang=None, accept=None, body=None):
    return page.handler({
        "requestContext": {"http": {"method": method}}, "rawPath": "/" + url.split("/", 3)[3],
        "queryStringParameters": {"lang": lang} if lang else None, "body": body,
        "headers": {"accept-language": accept} if accept else {}}, None)


def _booking(status="CONFIRMED", chat="5550123", created=None):
    return store.Booking(
        id=1, code="AC0123456789AB", buyer=store.Buyer("telegram", "5550123", chat), contact=store.Contact("Mei Lin"),
        locale="zh-TW", currency="USD", status=status, total_amount=Decimal("1330.50"), version=1,
        created_at=created or datetime.now(UTC), order_mode="SIMULATED",
        trip=store.Trip("TR0123456789AB", "東京賞櫻 <b>", None, None, "ACTIVE"))


@pytest.fixture
def configured(monkeypatch):
    monkeypatch.setenv(payment.SECRET_ENV, KEY)
    monkeypatch.setattr(payment, "_settings", SimpleNamespace(get_config=lambda key, default=None: "https://pay.example/"))


def test_the_page_shows_the_order_item_by_item_and_never_who_travels(configured, page):
    shown = _call(page, "GET", payment.link(_booking(), ITEMS), lang="en")

    assert shown["statusCode"] == 200
    body = shown["body"]
    assert body.count("<details") == 2 and "Park Hotel Tokyo / Deluxe King" in body and "2027-03-04" in body
    assert "USD 1,330.50" in body and "converted from GBP 418.20" in body and "Demo payment integration" in body
    assert "東京賞櫻 &lt;b&gt;" in body and "東京賞櫻 <b>" not in body
    assert "Mei Lin" not in body and "4012 0003 0000 0062" in body


@pytest.mark.parametrize(("lang", "accept", "expected"), [
    (None, "zh-TW,zh;q=0.9,en;q=0.8", "示範整合付款"), (None, "zh-CN", "演示集成付款"), (None, "ja-JP", "デモ決済"),
    (None, "ko-KR", "데모 결제 연동"), (None, "fr-FR", "Demo payment integration"), ("en", "zh-TW", "Demo payment integration"),
    ("zh-Hant", "en-US", "示範整合付款"), ("xx", "ja", "デモ決済"),
])
def test_the_page_speaks_the_visitors_language_and_the_one_they_choose(configured, page, lang, accept, expected):
    body = _call(page, "GET", payment.link(_booking(), ITEMS), lang=lang, accept=accept)["body"]

    assert expected in body and all(f'value="{code}"' in body for code in page.LANGUAGES)


def test_an_order_is_paid_once_and_the_traveller_is_told_once(configured, page):
    url = payment.link(_booking(), ITEMS)

    first, again = _call(page, "POST", url, lang="en"), _call(page, "POST", url, lang="en")
    assert (first["statusCode"], json.loads(again["body"])) == (200, {"ok": True, "already": True})
    assert len(page.sent) == 1 and page.sent[0][0] == "5550123" and "AC0123456789AB" in page.sent[0][1]
    assert "USD 1,330.50" in page.sent[0][1] and "nothing was charged" in page.sent[0][1]
    assert "already been paid" in _call(page, "GET", url, lang="en")["body"]


def test_a_get_never_pays(configured, page):
    _call(page, "GET", payment.link(_booking(), ITEMS))
    assert page.sent == []


def _through(page, seen=None):
    """The bot's HTTP call, answered by the page itself. *seen* collects what the bot sent."""
    def request_json(method, url, content=None, **_kwargs):
        if seen is not None:
            seen.append((method, url, content))
        answer = _call(page, method, url, body=content.decode() if content else None)
        return answer["statusCode"], json.loads(answer["body"])

    return request_json


def test_a_cancelled_order_cannot_be_paid(configured, page, monkeypatch):
    monkeypatch.setattr(_common, "request_json", _through(page))
    booking = _booking()
    url = payment.link(booking, ITEMS)

    assert (payment.state(booking), payment.void(booking), payment.state(booking)) == ("UNPAID", "VOID", "VOID")
    assert _call(page, "POST", url)["statusCode"] == 409 and page.sent == []
    assert "cancelled" in _call(page, "GET", url, lang="en")["body"] and "<form" not in _call(page, "GET", url)["body"]


def test_cancelling_after_payment_reports_that_it_was_paid(configured, page, monkeypatch):
    monkeypatch.setattr(_common, "request_json", _through(page))
    booking = _booking()
    _call(page, "POST", payment.link(booking, ITEMS))

    assert (payment.state(booking), payment.void(booking)) == ("PAID", "PAID")


def test_a_payment_link_is_not_a_void_or_status_request_and_the_reverse(configured, page):
    url = payment.link(_booking(), ITEMS)
    token = url.rsplit("/", 1)[1]

    assert _call(page, "POST", f"https://pay.example/v/{token}")["statusCode"] == 403
    assert _call(page, "GET", f"https://pay.example/s/{token}")["statusCode"] == 403
    void_token = payment._token(KEY, {"code": "AC0123456789AB", "act": "void", "exp": int(time.time()) + 60})
    assert _call(page, "GET", f"https://pay.example/p/{void_token}")["statusCode"] == 410 and page.sent == []


def test_a_changed_expired_or_foreign_link_is_refused(configured, page):
    token = payment.link(_booking(), ITEMS).rsplit("/", 1)[1]
    version, body, signature = token.split(".")
    old = payment.link(_booking(created=datetime.fromtimestamp(time.time() - 23 * 3600, UTC)), ITEMS, now=time.time() - 3600)

    assert page.read_token(token, KEY, time.time())["code"] == "AC0123456789AB"
    assert page.read_token(f"{version}.{body[:-2]}AA.{signature}", KEY, time.time()) is None
    assert page.read_token(token, "another key", time.time()) is None
    assert page.read_token(old.rsplit("/", 1)[1], KEY, time.time() + 2 * 3600) is None
    assert _call(page, "POST", f"https://pay.example/p/{version}.{body}.AAAA")["statusCode"] == 410 and page.sent == []


def test_looking_at_an_order_again_does_not_extend_its_link(configured):
    booking = _booking(created=datetime.fromtimestamp(time.time() - 3600, UTC))
    first = payment.link(booking, ITEMS, now=time.time())
    later = payment.link(booking, ITEMS, now=time.time() + 600)

    assert first == later
    assert payment.link(_booking(created=datetime.fromtimestamp(time.time() - 25 * 3600, UTC)), ITEMS) is None


@pytest.mark.parametrize("booking", [_booking(status="CANCELLED"), _booking(status="DRAFT"), _booking(chat=None)])
def test_an_order_with_nothing_to_pay_or_nobody_to_tell_has_no_link(configured, booking):
    assert payment.link(booking, ITEMS) is None


def test_without_the_key_or_the_address_there_is_no_link_and_nothing_is_asked(monkeypatch, configured):
    monkeypatch.setattr(_common, "request_json", lambda *a, **k: pytest.fail("the page must not be called"))
    monkeypatch.delenv(payment.SECRET_ENV)
    assert (payment.link(_booking(), ITEMS), payment.state(_booking()), payment.void(_booking())) == (None, None, None)
    monkeypatch.setenv(payment.SECRET_ENV, KEY)
    monkeypatch.setattr(payment, "_settings", SimpleNamespace(get_config=lambda key, default=None: default))
    assert payment.link(_booking(), ITEMS) is None


def test_a_failed_notice_leaves_the_order_payable_and_logs_no_token(configured, page, monkeypatch, capsys):
    def refuse(chat, message):
        raise ValueError("https://api.telegram.org/bot123:SECRET/sendMessage is not a URL")

    monkeypatch.setattr(page, "_send_telegram", refuse)
    url = payment.link(_booking(), ITEMS)

    assert _call(page, "POST", url)["statusCode"] == 502
    assert "SECRET" not in capsys.readouterr().out
    monkeypatch.setattr(page, "_send_telegram", lambda chat, message: page.sent.append((chat, message)))
    assert _call(page, "POST", url)["statusCode"] == 200 and len(page.sent) == 1


def test_a_cancel_reaches_the_page_when_lookups_have_used_up_the_pages_turns(configured, monkeypatch):
    # A void the page never hears leaves a cancelled order payable, and its statement to be mailed.
    answered = []

    class _Answer:
        status_code = 200

        @staticmethod
        def json():
            return {"state": "VOID"}

    def wire(method, url, **_kwargs):
        answered.append((method, url.split("/")[3]))
        return _Answer()

    monkeypatch.setattr(_common, "_supplier_budget", _common.RateLimiter())
    monkeypatch.setattr(_common.httpx, "request", wire)
    booking = _booking()
    while payment.state(booking) is not None:  # until the page's budget of lookups is spent
        pass
    lookups = len(answered)

    assert payment.void(booking) == "VOID"
    assert answered[lookups:] == [("POST", "v")] and payment.state(booking) is None


# ── The statement that is mailed once the order is paid ───────────────────────


def _statement_rows(page) -> list:
    return [code for code in page.rows if code.endswith("#statement")]


def _mail(page, index: int = 0):
    return message_from_bytes(page.mailed[index]["Content"]["Raw"]["Data"], policy=policy.default)


def test_a_statement_left_with_the_page_is_mailed_once_when_the_order_is_paid(configured, page, monkeypatch):
    monkeypatch.setattr(_common, "request_json", _through(page))
    booking = _booking()
    url = payment.link(booking, ITEMS)

    kept = payment.keep_statement(booking, "mei@example.com", PDF)
    before_payment = list(page.mailed)
    _call(page, "POST", url, lang="en"), _call(page, "POST", url, lang="en")

    (mail,) = page.mailed  # one mail, however often Pay is pressed
    message = _mail(page)
    (attachment,) = message.iter_attachments()
    assert (kept, before_payment) == ("KEPT", [])
    assert (mail["FromEmailAddress"], mail["Destination"]) == (SENDER, {"ToAddresses": ["mei@example.com"]})
    assert message["To"] == "mei@example.com" and SENDER in message["From"]
    assert "AC0123456789AB" in message["Subject"]
    assert (attachment.get_filename(), attachment.get_content_type(), attachment.get_content()) == (
        "Accomy-statement-AC0123456789AB.pdf", "application/pdf", PDF)
    body = message.get_body().get_content()
    assert "USD 1,330.50" in body and "nothing was charged" in body and "not a ticket" in body
    assert "you can ignore it" in body  # for whoever owns an address that someone else typed
    # The chat is told of the payment, and then, in a message of its own, where the statement went.
    assert [chat for chat, _text in page.sent] == ["5550123", "5550123"]
    assert "Payment complete" in page.sent[0][1] and "emailed" not in page.sent[0][1]
    assert page.sent[1][1] == "📎 The statement of order AC0123456789AB was emailed to mei@example.com"


def test_the_mail_holds_none_of_the_buyers_own_words(configured, page, monkeypatch):
    # The trip's name is the buyer's text, and the mail goes to an address nobody confirmed.
    monkeypatch.setattr(_common, "request_json", _through(page))
    booking = _booking()
    payment.keep_statement(booking, "mei@example.com", PDF)

    _call(page, "POST", payment.link(booking, ITEMS), lang="zh-Hant")

    message = _mail(page)
    assert "東京賞櫻" in page.sent[0][1]  # the buyer's own chat still names the trip
    assert "東京賞櫻" not in message.get_body().get_content() and "東京賞櫻" not in str(message["Subject"])


def test_once_mailed_the_page_holds_neither_the_address_nor_the_file(configured, page, monkeypatch):
    monkeypatch.setattr(_common, "request_json", _through(page))
    booking = _booking()
    payment.keep_statement(booking, "mei@example.com", PDF)
    held_before = _statement_rows(page)

    _call(page, "POST", payment.link(booking, ITEMS))

    assert len(held_before) == 1 and _statement_rows(page) == []
    assert "mei@example.com" not in json.dumps(page.rows, default=str)


def test_an_order_without_a_statement_is_paid_as_before_and_no_mail_is_claimed(configured, page):
    _call(page, "POST", payment.link(_booking(), ITEMS), lang="en")

    assert page.mailed == [] and len(page.sent) == 1 and "emailed" not in page.sent[0][1]


@pytest.mark.parametrize(("lang", "subject", "body", "notice"), [
    ("zh-Hant", "Accomy 示範訂單 AC0123456789AB 的行程單", "示範付款已完成", "的行程單已寄到 mei@example.com"),
    ("zh-Hans", "Accomy 演示订单 AC0123456789AB 的行程单", "演示付款已完成", "的行程单已发送到 mei@example.com"),
    ("ja", "Accomy デモ注文 AC0123456789AB の明細書", "デモ決済が完了しました", "の明細書を mei@example.com に送信しました"),
    ("ko", "Accomy 데모 주문 AC0123456789AB 명세서", "데모 결제가 완료되었습니다", "명세서를 mei@example.com(으)로 보냈습니다"),
])
def test_the_mail_and_its_notice_are_in_the_language_of_the_page(configured, page, monkeypatch, lang, subject, body, notice):
    monkeypatch.setattr(_common, "request_json", _through(page))
    booking = _booking()
    payment.keep_statement(booking, "mei@example.com", PDF)

    _call(page, "POST", payment.link(booking, ITEMS), lang=lang)

    message = _mail(page)
    assert message["Subject"] == subject and body in message.get_body().get_content()
    assert "USD 1,330.50" in message.get_body().get_content()
    assert page.sent[1][1].endswith(notice)


def test_the_statement_of_an_unpaid_order_expires_with_its_payment_link(configured, page, monkeypatch):
    monkeypatch.setattr(_common, "request_json", _through(page))
    booking = _booking(created=datetime.fromtimestamp(time.time() - 3600, UTC))

    payment.keep_statement(booking, "mei@example.com", PDF)

    (row,) = (page.rows[code] for code in _statement_rows(page))
    # The table drops the row by itself from then on, and no payment link works after it either.
    assert row["ttl"] == int(booking.created_at.timestamp()) + payment.LINK_LIFE_SECONDS


def test_a_cancelled_order_is_never_mailed(configured, page, monkeypatch):
    monkeypatch.setattr(_common, "request_json", _through(page))
    booking = _booking()
    payment.keep_statement(booking, "mei@example.com", PDF)

    payment.void(booking)
    refused = _call(page, "POST", payment.link(booking, ITEMS))

    assert _statement_rows(page) == [] and (refused["statusCode"], page.mailed) == (409, [])


@pytest.mark.parametrize("closed", ["void", "pay"])
def test_a_statement_that_arrives_after_the_order_was_cancelled_or_paid_is_not_kept(configured, page, monkeypatch, closed):
    monkeypatch.setattr(_common, "request_json", _through(page))
    booking = _booking()
    payment.void(booking) if closed == "void" else _call(page, "POST", payment.link(booking, ITEMS))

    answer = payment.keep_statement(booking, "mei@example.com", PDF)

    assert answer == {"void": "VOID", "pay": "PAID"}[closed] and _statement_rows(page) == []


def test_a_page_set_up_without_a_sender_keeps_nothing_and_says_so(configured, page, monkeypatch):
    monkeypatch.delenv("STATEMENT_SENDER")
    monkeypatch.setattr(_common, "request_json", _through(page))
    booking = _booking()

    answer = payment.keep_statement(booking, "mei@example.com", PDF)
    _call(page, "POST", payment.link(booking, ITEMS))

    assert (answer, _statement_rows(page), page.mailed) == ("OFF", [], [])


def test_what_the_bot_hands_over_cannot_be_changed_on_the_way(configured, page, monkeypatch):
    seen = []
    monkeypatch.setattr(_common, "request_json", _through(page, seen))
    booking = _booking()
    payment.keep_statement(booking, "mei@example.com", PDF)
    page.rows.clear()
    (_method, url, content) = seen[0]

    other_address = _call(page, "POST", url, body=content.decode().replace("mei@example.com", "eve@example.com"))
    other_file = _call(page, "POST", url, body=json.dumps({"email": "mei@example.com", "pdf": base64.b64encode(b"%PDF-other").decode()}))
    as_sent = _call(page, "POST", url, body=content.decode())

    assert (other_address["statusCode"], other_file["statusCode"]) == (403, 403)
    assert as_sent["statusCode"] == 200 and len(_statement_rows(page)) == 1


def test_a_body_that_the_gateway_hands_on_encoded_is_the_body_the_bot_signed(configured, page, monkeypatch):
    # API Gateway base64-encodes a body whose content type it does not take for text.
    seen = []
    monkeypatch.setattr(_common, "request_json", _through(page, seen))
    payment.keep_statement(_booking(), "mei@example.com", PDF)
    page.rows.clear()
    (_method, url, content) = seen[0]

    answer = page.handler({
        "requestContext": {"http": {"method": "POST"}}, "rawPath": "/" + url.split("/", 3)[3], "headers": {},
        "body": base64.b64encode(content).decode(), "isBase64Encoded": True}, None)

    assert json.loads(answer["body"]) == {"state": "KEPT"} and len(_statement_rows(page)) == 1


def test_only_the_bots_own_request_leaves_a_statement(configured, page, monkeypatch):
    seen = []
    monkeypatch.setattr(_common, "request_json", _through(page, seen))
    booking = _booking()
    payment.keep_statement(booking, "mei@example.com", PDF)
    payment.state(booking)
    page.rows.clear()
    (_post, statement_url, content), (_get, status_url, _none) = seen
    statement_token, status_token = statement_url.rsplit("/", 1)[1], status_url.rsplit("/", 1)[1]
    pay_token = payment.link(booking, ITEMS).rsplit("/", 1)[1]
    signed, signature = statement_token.rsplit(".", 1)
    # Another first character, never the last: that one's low bits are padding, and changing them changes nothing.
    forged = f"{signed}.{'A' if signature[0] != 'A' else 'B'}{signature[1:]}"

    # A payment link is in the traveller's hands, and a status request is a bot request of another kind.
    for token in (pay_token, status_token, forged):
        assert _call(page, "POST", f"https://pay.example/e/{token}", body=content.decode())["statusCode"] == 403
    # And the statement request is good for nothing else.
    assert _call(page, "POST", f"https://pay.example/v/{statement_token}")["statusCode"] == 403
    assert _call(page, "GET", f"https://pay.example/s/{statement_token}")["statusCode"] == 403
    assert _call(page, "POST", f"https://pay.example/p/{statement_token}")["statusCode"] == 410
    assert page.rows == {}


@pytest.mark.parametrize(("email", "pdf"), [
    ("mei@example", PDF),  # no domain to deliver to
    ("Mei <mei@example.com>", PDF),  # more than an address
    ("mei@example.com\r\nBcc: eve@example.com", PDF),  # a second header
    ("mei@example.com\n", PDF),
    ("mei@example.com,eve@example.com", PDF),  # two recipients
    ("mei@例え.jp", PDF),  # outside what the mail header is built for
    (".mei@example.com", PDF), ("mei.@example.com", PDF), ("mei..lin@example.com", PDF),  # the mail library refuses these
    ("mei@-example.com", PDF), ("mei@example-.com", PDF), ("mei@example.c", PDF), ("mei@example.123", PDF),
    ("m" * 65 + "@example.com", PDF),
    ("mei@example.com", b"<html>not a PDF</html>"),
])
def test_the_page_refuses_an_address_or_a_file_it_would_not_mail(configured, page, monkeypatch, email, pdf):
    monkeypatch.setattr(_common, "request_json", _through(page))

    assert payment.keep_statement(_booking(), email, pdf) is None
    assert page.rows == {}


@pytest.mark.parametrize("email", [
    "mei@example.com", "Mei.Lin+trip_2027%x-y@mail.example.co.uk", "m@x1.io", "a-b_c@sub-domain.example.travel"])
def test_every_address_the_page_keeps_is_one_it_can_put_on_a_mail(configured, page, monkeypatch, email):
    monkeypatch.setattr(_common, "request_json", _through(page))
    booking = _booking()

    assert payment.keep_statement(booking, email, PDF) == "KEPT"
    _call(page, "POST", payment.link(booking, ITEMS))

    assert page.mailed[0]["Destination"] == {"ToAddresses": [email]} and _mail(page)["To"] == email


def test_the_bot_and_the_page_hold_the_same_rule_for_an_address_and_the_same_size_limit(page):
    from plugins.accomy_travel import tools_orders

    # Two deployments, so two copies: an address the bot hands over must be one the page keeps.
    assert tools_orders._MAILBOX_RE.pattern == page._ADDRESS_RE.pattern
    assert payment.STATEMENT_MAX_BYTES == page._STATEMENT_BYTES


def test_a_statement_too_large_for_the_page_is_never_sent_to_it_and_the_page_refuses_one(configured, page, monkeypatch):
    seen = []
    monkeypatch.setattr(_common, "request_json", _through(page, seen))
    large = b"%PDF-" + b"0" * payment.STATEMENT_MAX_BYTES

    not_sent = payment.keep_statement(_booking(), "mei@example.com", large)
    monkeypatch.setattr(payment, "STATEMENT_MAX_BYTES", len(large))  # a bot that would send it anyway
    refused = payment.keep_statement(_booking(), "mei@example.com", large)

    assert (not_sent, refused) == (None, None) and len(seen) == 1 and page.rows == {}


def test_a_mail_that_cannot_be_sent_does_not_undo_the_payment_and_logs_no_address(configured, page, monkeypatch, capsys):
    def refuse(**mail):
        raise RuntimeError("MessageRejected: Address mei@example.com is on the suppression list")

    monkeypatch.setattr(_common, "request_json", _through(page))
    monkeypatch.setattr(page, "_ses", lambda: SimpleNamespace(send_email=refuse))
    booking = _booking()
    payment.keep_statement(booking, "mei@example.com", PDF)

    paid = _call(page, "POST", payment.link(booking, ITEMS), lang="en")

    assert paid["statusCode"] == 200 and payment.state(booking) == "PAID"
    assert len(page.sent) == 1 and "emailed" not in page.sent[0][1]  # the chat is told of no mail
    assert "mei@example.com" not in capsys.readouterr().out and _statement_rows(page) == []


def test_a_payment_the_page_takes_back_has_mailed_nothing_and_mails_once_when_it_goes_through(configured, page, monkeypatch):
    def refuse(chat, message):
        raise ValueError("no route to Telegram")

    monkeypatch.setattr(_common, "request_json", _through(page))
    booking = _booking()
    url = payment.link(booking, ITEMS)
    payment.keep_statement(booking, "mei@example.com", PDF)
    monkeypatch.setattr(page, "_send_telegram", refuse)
    first = _call(page, "POST", url)
    mailed_by_the_failed_payment, payable_again = list(page.mailed), payment.state(booking)
    monkeypatch.setattr(page, "_send_telegram", lambda chat, message: page.sent.append((chat, message)))

    second, third = _call(page, "POST", url), _call(page, "POST", url)

    # A mail that says "paid" must not go out for a payment the buyer is then told has failed.
    assert (first["statusCode"], mailed_by_the_failed_payment, payable_again) == (502, [], "UNPAID")
    assert (second["statusCode"], json.loads(third["body"]).get("already")) == (200, True) and len(page.mailed) == 1


def test_a_notice_of_the_mail_that_cannot_be_sent_leaves_the_payment_and_the_mail_as_they_are(configured, page, monkeypatch):
    def only_the_first(chat, message):
        if page.sent:
            raise ValueError("no route to Telegram")
        page.sent.append((chat, message))

    monkeypatch.setattr(_common, "request_json", _through(page))
    monkeypatch.setattr(page, "_send_telegram", only_the_first)
    booking = _booking()
    payment.keep_statement(booking, "mei@example.com", PDF)

    paid = _call(page, "POST", payment.link(booking, ITEMS))

    assert paid["statusCode"] == 200 and payment.state(booking) == "PAID" and len(page.mailed) == 1


def test_the_bot_token_is_read_before_the_order_is_recorded_as_paid(configured, page, monkeypatch):
    # A failure to read it must leave the order unpaid: after that point nothing would take the record back.
    def no_secret(name):
        if name == "BOT_TOKEN_PARAM":
            raise TimeoutError("parameter store")
        return KEY

    monkeypatch.setattr(page, "_secret", no_secret)
    booking = _booking()

    with pytest.raises(TimeoutError):
        _call(page, "POST", payment.link(booking, ITEMS))

    assert page.rows == {} and page.sent == []


def test_the_shipped_profile_names_the_payment_page(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    shutil.copy(REPO / "distributions/accomy/config.yaml", home / "config.yaml")
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv(payment.SECRET_ENV, KEY)
    manager = pmod.PluginManager()
    manager.discover_and_load()
    assert manager._plugins["accomy_travel"].enabled

    loaded = sys.modules["hermes_plugins.accomy_travel.payment"]  # the instance the bot's tools use
    assert loaded.link(_booking(), ITEMS).startswith("https://")
