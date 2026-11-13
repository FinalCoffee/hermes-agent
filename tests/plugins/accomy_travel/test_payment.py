"""The demo payment link an order carries and the page behind it, exercised together without AWS."""

import importlib.util
import json
import shutil
import sys
import time
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest

from hermes_cli import plugins as pmod
from plugins.accomy_travel import _common, payment
from plugins.accomy_travel.orderstore import store

REPO = Path(__file__).resolve().parents[3]
KEY = "k" * 64
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

    def put_item(self, Item, ConditionExpression):
        if Item["code"] in self.rows:
            raise _Taken
        self.rows[Item["code"]] = Item

    def delete_item(self, Key):
        self.rows.pop(Key["code"], None)


@pytest.fixture
def page(monkeypatch):
    """The Lambda handler of deploy/accomy/payment-demo with its table, its secrets and Telegram replaced."""
    boto3 = ModuleType("boto3")
    table = _Table()
    boto3.client = lambda name: SimpleNamespace(get_parameter=lambda **k: {"Parameter": {"Value": KEY}})
    boto3.resource = lambda name: SimpleNamespace(Table=lambda table_name: table)
    monkeypatch.setitem(sys.modules, "boto3", boto3)
    for name in ("LINK_SECRET_PARAM", "BOT_TOKEN_PARAM", "STATE_TABLE"):
        monkeypatch.setenv(name, name)
    spec = importlib.util.spec_from_file_location("accomy_payment_page", REPO / "deploy/accomy/payment-demo/handler.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.sent = []
    monkeypatch.setattr(module, "_send_telegram", lambda chat, message: module.sent.append((chat, message)))
    return module


def _call(page, method, url, lang=None, accept=None):
    return page.handler({
        "requestContext": {"http": {"method": method}}, "rawPath": "/" + url.split("/", 3)[3],
        "queryStringParameters": {"lang": lang} if lang else None,
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


def _through(page):
    """The bot's HTTP call, answered by the page itself."""
    def request_json(method, url, **_kwargs):
        answer = _call(page, method, url)
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
