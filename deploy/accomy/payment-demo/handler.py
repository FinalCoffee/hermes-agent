"""Demo payment page for the Accomy bot (AWS Lambda behind an HTTP API). It charges nothing.

The bot gives a traveller a link that carries a signed token: the order with every item being paid for,
the total, and which Telegram chat to tell. ``GET /p/<token>`` shows that order, item by item, in the
visitor's language (from the browser, or chosen on the page), says plainly that it is a demonstration
and accepts only Airwallex's public test card. The card number is checked in the browser and never sent
here. Pressing Pay sends ``POST /p/<token>``, and this function then sends that chat a real Telegram
message saying the (demo) payment went through.

One small table remembers, per order code, whether it was PAID or VOID, so an order is paid once and a
cancelled order cannot be paid. The bot voids an order when it cancels it (``POST /v/<token>``) and asks
whether one was paid (``GET /s/<token>``), with tokens it signs for that purpose and never shows anyone.

Secrets come from SSM Parameter Store, read once per container: the key that signs links (the bot holds
the same key) and the bot token used to send the message.
"""

import base64
import hashlib
import hmac
import html
import json
import os
import time
import urllib.request
import zlib

import boto3

_SSM = boto3.client("ssm")
# health: allow HX005 -- an AWS Lambda, not Hermes: its environment is this function's own configuration
_TABLE = boto3.resource("dynamodb").Table(os.environ["STATE_TABLE"])
_SECRETS: dict[str, str] = {}
_STATE_DAYS = 90
_MAX_CLAIMS_BYTES = 64 * 1024
TEST_CARD = "4012000300000062"
_HEADERS = {
    "cache-control": "no-store",
    "referrer-policy": "no-referrer",  # the token is in the URL
    "x-content-type-options": "nosniff",
    "content-security-policy": (
        "default-src 'none'; style-src 'unsafe-inline'; script-src 'unsafe-inline'; connect-src 'self'; "
        "frame-ancestors 'none'; base-uri 'none'; form-action 'none'"),
}

# Every language the page speaks.
LANGUAGES = {"zh-Hant": "繁體中文", "zh-Hans": "简体中文", "en": "English", "ja": "日本語", "ko": "한국어"}
_TEXT = {
    "zh-Hant": {
        "title": "Accomy 付款（示範）", "language": "語言",
        "banner": "示範整合付款：這是測試環境，不會實際扣款。請勿輸入真實卡號。",
        "trip": "行程", "order": "訂單編號", "items": "付款項目", "tap": "點選項目可查看明細",
        "total": "應付總額", "approx": "約略換算自", "check": "請確認以上內容與您在 Telegram 中確認的訂單相符。如有不符，請不要付款，並回到 Telegram 告知助理。",
        "card": "卡號", "expiry": "有效期限", "cvc": "安全碼", "hint": "請使用 Airwallex 測試卡號", "fill": "填入測試卡號",
        "pay": "付款", "wrong": "示範環境只接受上方的測試卡號。請勿輸入真實卡號。", "paying": "處理中…",
        "done": "付款完成（示範）", "done_note": "已傳送通知到您的 Telegram，可以關閉此頁面。",
        "failed": "暫時無法完成，請稍後再試。", "cancelled": "這筆訂單已取消，無需付款。", "paid": "這筆訂單已完成付款（示範）。",
        "expired": "這個付款連結已失效，請回到 Telegram 重新取得。",
        "kind": {"HOTEL": "住宿", "FLIGHT": "機票", "EVENT_TICKET": "活動門票"},
        "field": {"hotel": "飯店", "room": "房型", "check_in": "入住", "check_out": "退房", "nights": "晚數",
                  "guests": "入住人數", "rooms": "房間數", "passengers": "旅客人數", "flights": "航班",
                  "event": "活動", "venue": "場地", "category": "票種", "starts_local": "開始時間（當地）",
                  "tickets": "張數", "quantity": "數量", "amount": "金額", "cancellation": "取消規定"},
        "message": "✅ 付款完成（示範）\n訂單 {code}\n{name}\n金額 {currency} {amount}\n\n這是示範付款，未實際扣款。",
    },
    "zh-Hans": {
        "title": "Accomy 付款（演示）", "language": "语言",
        "banner": "演示集成付款：这是测试环境，不会实际扣款。请勿输入真实卡号。",
        "trip": "行程", "order": "订单编号", "items": "付款项目", "tap": "点击项目可查看明细",
        "total": "应付总额", "approx": "约略换算自", "check": "请确认以上内容与您在 Telegram 中确认的订单一致。如有不符，请不要付款，并回到 Telegram 告知助理。",
        "card": "卡号", "expiry": "有效期", "cvc": "安全码", "hint": "请使用 Airwallex 测试卡号", "fill": "填入测试卡号",
        "pay": "付款", "wrong": "演示环境只接受上方的测试卡号。请勿输入真实卡号。", "paying": "处理中…",
        "done": "付款完成（演示）", "done_note": "已发送通知到您的 Telegram，可以关闭此页面。",
        "failed": "暂时无法完成，请稍后再试。", "cancelled": "这笔订单已取消，无需付款。", "paid": "这笔订单已完成付款（演示）。",
        "expired": "这个付款链接已失效，请回到 Telegram 重新获取。",
        "kind": {"HOTEL": "住宿", "FLIGHT": "机票", "EVENT_TICKET": "活动门票"},
        "field": {"hotel": "酒店", "room": "房型", "check_in": "入住", "check_out": "退房", "nights": "晚数",
                  "guests": "入住人数", "rooms": "房间数", "passengers": "旅客人数", "flights": "航班",
                  "event": "活动", "venue": "场地", "category": "票种", "starts_local": "开始时间（当地）",
                  "tickets": "张数", "quantity": "数量", "amount": "金额", "cancellation": "取消规定"},
        "message": "✅ 付款完成（演示）\n订单 {code}\n{name}\n金额 {currency} {amount}\n\n这是演示付款，未实际扣款。",
    },
    "en": {
        "title": "Accomy payment (demo)", "language": "Language",
        "banner": "Demo payment integration: this is a test environment and nothing is charged. Do not enter a real card.",
        "trip": "Trip", "order": "Order", "items": "What you are paying for", "tap": "Tap an item to see its details",
        "total": "Total due", "approx": "approx., converted from", "check": "Check that this matches the order you confirmed in Telegram. If anything differs, do not pay: go back to Telegram and tell the assistant.",
        "card": "Card number", "expiry": "Expiry", "cvc": "CVC", "hint": "Use the Airwallex test card", "fill": "Fill in the test card",
        "pay": "Pay", "wrong": "The demo accepts only the test card above. Do not enter a real card.", "paying": "Processing…",
        "done": "Payment complete (demo)", "done_note": "A notice was sent to your Telegram. You can close this page.",
        "failed": "That did not go through. Please try again shortly.", "cancelled": "This order was cancelled. There is nothing to pay.", "paid": "This order has already been paid (demo).",
        "expired": "This payment link has expired. Ask for a new one in Telegram.",
        "kind": {"HOTEL": "Hotel", "FLIGHT": "Flight", "EVENT_TICKET": "Event tickets"},
        "field": {"hotel": "Hotel", "room": "Room", "check_in": "Check-in", "check_out": "Check-out", "nights": "Nights",
                  "guests": "Guests", "rooms": "Rooms", "passengers": "Passengers", "flights": "Flights",
                  "event": "Event", "venue": "Venue", "category": "Category", "starts_local": "Starts (local time)",
                  "tickets": "Tickets", "quantity": "Quantity", "amount": "Amount", "cancellation": "Cancellation"},
        "message": "✅ Payment complete (demo)\nOrder {code}\n{name}\nAmount {currency} {amount}\n\nThis was a demonstration payment: nothing was charged.",
    },
    "ja": {
        "title": "Accomy お支払い（デモ）", "language": "言語",
        "banner": "デモ決済：これはテスト環境で、実際の請求は発生しません。本物のカード番号は入力しないでください。",
        "trip": "旅程", "order": "注文番号", "items": "お支払い内容", "tap": "項目をタップすると詳細を表示します",
        "total": "お支払い合計", "approx": "概算、換算元", "check": "Telegram で確認したご注文と同じ内容かご確認ください。異なる場合は支払わず、Telegram でアシスタントにお知らせください。",
        "card": "カード番号", "expiry": "有効期限", "cvc": "セキュリティコード", "hint": "Airwallex のテストカード番号をご利用ください", "fill": "テストカード番号を入力",
        "pay": "支払う", "wrong": "デモでは上記のテストカード番号のみ利用できます。本物のカード番号は入力しないでください。", "paying": "処理中…",
        "done": "お支払い完了（デモ）", "done_note": "Telegram に通知を送信しました。このページは閉じて構いません。",
        "failed": "完了できませんでした。しばらくしてからもう一度お試しください。", "cancelled": "この注文はキャンセルされました。お支払いは不要です。", "paid": "この注文はお支払い済みです（デモ）。",
        "expired": "この支払いリンクは無効になりました。Telegram で新しいリンクを取得してください。",
        "kind": {"HOTEL": "宿泊", "FLIGHT": "航空券", "EVENT_TICKET": "イベントチケット"},
        "field": {"hotel": "ホテル", "room": "部屋タイプ", "check_in": "チェックイン", "check_out": "チェックアウト", "nights": "泊数",
                  "guests": "宿泊人数", "rooms": "部屋数", "passengers": "搭乗者数", "flights": "フライト",
                  "event": "イベント", "venue": "会場", "category": "席種", "starts_local": "開始時刻（現地）",
                  "tickets": "枚数", "quantity": "数量", "amount": "金額", "cancellation": "キャンセル規定"},
        "message": "✅ お支払い完了（デモ）\n注文 {code}\n{name}\n金額 {currency} {amount}\n\nこれはデモ決済で、実際の請求は発生していません。",
    },
    "ko": {
        "title": "Accomy 결제(데모)", "language": "언어",
        "banner": "데모 결제 연동: 테스트 환경이며 실제로 결제되지 않습니다. 실제 카드 번호를 입력하지 마세요.",
        "trip": "여행", "order": "주문 번호", "items": "결제 항목", "tap": "항목을 누르면 상세 내용을 볼 수 있습니다",
        "total": "결제 금액", "approx": "대략, 환산 기준", "check": "Telegram에서 확인한 주문과 같은지 확인해 주세요. 다르면 결제하지 말고 Telegram에서 어시스턴트에게 알려 주세요.",
        "card": "카드 번호", "expiry": "유효 기간", "cvc": "보안 코드", "hint": "Airwallex 테스트 카드 번호를 사용하세요", "fill": "테스트 카드 번호 입력",
        "pay": "결제", "wrong": "데모에서는 위의 테스트 카드 번호만 사용할 수 있습니다. 실제 카드 번호를 입력하지 마세요.", "paying": "처리 중…",
        "done": "결제 완료(데모)", "done_note": "Telegram으로 알림을 보냈습니다. 이 페이지를 닫아도 됩니다.",
        "failed": "완료하지 못했습니다. 잠시 후 다시 시도해 주세요.", "cancelled": "이 주문은 취소되었습니다. 결제할 내용이 없습니다.", "paid": "이 주문은 이미 결제되었습니다(데모).",
        "expired": "이 결제 링크는 만료되었습니다. Telegram에서 새 링크를 받아 주세요.",
        "kind": {"HOTEL": "숙박", "FLIGHT": "항공권", "EVENT_TICKET": "이벤트 티켓"},
        "field": {"hotel": "호텔", "room": "객실", "check_in": "체크인", "check_out": "체크아웃", "nights": "숙박 일수",
                  "guests": "투숙 인원", "rooms": "객실 수", "passengers": "탑승객 수", "flights": "항공편",
                  "event": "이벤트", "venue": "장소", "category": "좌석 종류", "starts_local": "시작 시간(현지)",
                  "tickets": "매수", "quantity": "수량", "amount": "금액", "cancellation": "취소 규정"},
        "message": "✅ 결제 완료(데모)\n주문 {code}\n{name}\n금액 {currency} {amount}\n\n데모 결제이며 실제로 결제되지 않았습니다.",
    },
}


def _secret(name: str) -> str:
    if name not in _SECRETS:
        _SECRETS[name] = _SSM.get_parameter(Name=os.environ[name], WithDecryption=True)["Parameter"]["Value"].strip()
    return _SECRETS[name]


def _b64(data: str) -> bytes:
    return base64.urlsafe_b64decode(data + "=" * (-len(data) % 4))


def read_token(token: str, key: str, now: float, act: str = "pay") -> dict | None:
    """The claims of a link the bot signed for *act* and that has not expired; None for anything else."""
    try:
        version, body, signature = token.split(".")
        expected = hmac.new(key.encode(), f"{version}.{body}".encode(), hashlib.sha256).digest()
        if version != "v2" or not hmac.compare_digest(expected, _b64(signature)):
            return None
        inflater = zlib.decompressobj()
        raw = inflater.decompress(_b64(body), _MAX_CLAIMS_BYTES)
        if inflater.unconsumed_tail:
            return None
        claims = json.loads(raw)
        # A payment link must never work as a void or a status request, nor the other way round.
        return claims if float(claims["exp"]) > now and claims.get("act", "pay") == act and claims["code"] else None
    except (ValueError, KeyError, TypeError, zlib.error):
        return None


def pick_language(asked: str | None, accept: str | None, default: str | None) -> str:
    """The language chosen on the page, else the first one the browser lists that the page speaks, else
    the link's, else English."""
    if asked in LANGUAGES:
        return asked
    for part in (accept or "").split(","):
        tag = part.split(";")[0].strip().lower()
        if tag.startswith("zh"):
            return "zh-Hans" if any(mark in tag for mark in ("hans", "cn", "sg")) else "zh-Hant"
        for code in ("en", "ja", "ko"):
            if tag.startswith(code):
                return code
    return default if default in LANGUAGES else "en"


def _reply(status: int, body: str, content_type: str = "text/html; charset=utf-8") -> dict:
    return {"statusCode": status, "headers": {**_HEADERS, "content-type": content_type}, "body": body}


def _notice(title: str, message: str, status: int) -> dict:
    return _reply(status, f"<!doctype html><meta charset=utf-8><meta name=viewport content='width=device-width,"
                          f"initial-scale=1'><title>{html.escape(title)}</title>"
                          f"<body style='font:16px system-ui;margin:3rem 1.5rem;text-align:center'>"
                          f"<p>{html.escape(message)}</p>")


def _value(value) -> str:
    """One detail value as HTML: a list (the flights of an itinerary) is one line each."""
    if isinstance(value, list):
        return "<br>".join(html.escape(str(entry)) for entry in value)
    return html.escape(str(value))


def _item(text: dict, item: dict, currency: str, opened: bool) -> str:
    """One thing being paid for: a line that opens to show everything the order recorded about it."""
    amount = f"{html.escape(currency)} {html.escape(str(item['amount']))}"
    rows = [(text["field"].get(key, key), _value(value)) for key, value in item.get("details", {}).items()]
    if item.get("quantity", 1) != 1 and "tickets" not in item.get("details", {}):
        rows.append((text["field"]["quantity"], html.escape(str(item["quantity"]))))
    source = item.get("supplier_price")
    shown = amount + (f" <small>({html.escape(text['approx'])} {html.escape(source['currency'])} "
                      f"{html.escape(str(source['amount']))})</small>" if source else "")
    rows.append((text["field"]["amount"], shown))
    table = "".join(f"<tr><th>{html.escape(label)}</th><td>{value}</td></tr>" for label, value in rows)
    kind = html.escape(text["kind"].get(item.get("kind"), ""))
    return (f"<details{' open' if opened else ''}><summary><span class=kind>{kind}</span>"
            f"<span class=name>{html.escape(str(item['title']))}</span><span class=amt>{amount}</span></summary>"
            f"<table>{table}</table></details>")


def _page(text: dict, language: str, claims: dict, paid: bool = False) -> str:
    t = {key: html.escape(value) for key, value in text.items() if isinstance(value, str)}
    currency = str(claims["currency"])
    total = f"{html.escape(currency)} {html.escape(str(claims['amount']))}"
    items = claims.get("items") or []
    listing = "".join(_item(text, item, currency, opened=len(items) == 1) for item in items)
    options = "".join(
        f"<option value=\"{code}\"{' selected' if code == language else ''}>{html.escape(name)}</option>"
        for code, name in LANGUAGES.items())
    pretty_card = " ".join(TEST_CARD[i:i + 4] for i in range(0, 16, 4))
    return f"""<!doctype html>
<html lang="{language}"><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><meta name="robots" content="noindex">
<title>{t['title']}</title>
<style>
:root{{color-scheme:light dark;--ink:#16202e;--soft:#5b6b80;--line:#d7dee8;--card:#fff;--page:#f3f6fa;--go:#0f6fde;--warn:#8a5a00;--warnbg:#fff4d6}}
@media(prefers-color-scheme:dark){{:root{{--ink:#e8eef6;--soft:#9fb0c5;--line:#2c3a4d;--card:#18212d;--page:#0e151e;--go:#3b8cf0;--warn:#ffd98a;--warnbg:#3a2f12}}}}
*{{box-sizing:border-box}}body{{margin:0;font:16px/1.5 system-ui,-apple-system,"PingFang TC","Noto Sans TC","Hiragino Sans","Apple SD Gothic Neo",sans-serif;color:var(--ink);background:var(--page)}}
main{{max-width:28rem;margin:0 auto;padding:1rem}}
header{{display:flex;justify-content:space-between;align-items:center;margin-bottom:.8rem}}header b{{font-size:1.1rem}}
select{{font:inherit;font-size:.9rem;padding:.35rem .5rem;border:1px solid var(--line);border-radius:.5rem;background:var(--card);color:inherit}}
.banner{{background:var(--warnbg);color:var(--warn);border-radius:.6rem;padding:.7rem .9rem;font-size:.9rem;font-weight:600}}
.box{{background:var(--card);border:1px solid var(--line);border-radius:.8rem;padding:1rem;margin-top:.9rem}}
.row{{display:flex;justify-content:space-between;gap:1rem;color:var(--soft);font-size:.9rem}}.row b{{color:var(--ink);text-align:right;overflow-wrap:anywhere}}
h2{{font-size:.95rem;margin:1rem 0 .1rem}}.tap{{font-size:.8rem;color:var(--soft);margin-bottom:.4rem}}
details{{border-top:1px solid var(--line)}}summary{{display:flex;align-items:baseline;gap:.5rem;padding:.7rem 0;cursor:pointer;list-style:none}}
summary::-webkit-details-marker{{display:none}}summary::after{{content:"▾";color:var(--soft);margin-left:.2rem}}details[open] summary::after{{content:"▴"}}
.kind{{flex:none;font-size:.75rem;font-weight:600;color:var(--go);border:1px solid var(--go);border-radius:.4rem;padding:0 .35rem}}
.name{{flex:1;min-width:0;overflow-wrap:anywhere;font-weight:600}}.amt{{flex:none;font-variant-numeric:tabular-nums;white-space:nowrap}}
table{{width:100%;border-collapse:collapse;font-size:.88rem;margin:0 0 .8rem}}th{{text-align:left;font-weight:400;color:var(--soft);vertical-align:top;padding:.15rem .8rem .15rem 0;white-space:nowrap}}
td{{padding:.15rem 0;overflow-wrap:anywhere}}td small{{color:var(--soft)}}
.totalrow{{display:flex;justify-content:space-between;align-items:baseline;border-top:2px solid var(--line);padding-top:.7rem}}
.total{{font-size:1.5rem;font-weight:700;font-variant-numeric:tabular-nums}}.check{{font-size:.85rem;color:var(--soft);margin:.8rem 0 0}}
label{{display:block;font-size:.85rem;color:var(--soft);margin:.8rem 0 .25rem}}
input{{width:100%;font:inherit;padding:.65rem .7rem;border:1px solid var(--line);border-radius:.5rem;background:transparent;color:inherit}}
.two{{display:grid;grid-template-columns:1fr 1fr;gap:.7rem}}
.hint{{font-size:.85rem;color:var(--soft);margin-top:.6rem}}.hint code{{color:var(--ink);font-weight:600}}
button{{font:inherit;border:0;border-radius:.6rem;cursor:pointer}}
#fill{{background:none;color:var(--go);padding:.2rem 0;text-decoration:underline}}
#pay{{width:100%;margin-top:1rem;padding:.85rem;background:var(--go);color:#fff;font-weight:700;font-size:1.05rem}}#pay:disabled{{opacity:.6}}
#msg{{min-height:1.4rem;margin-top:.7rem;font-size:.9rem;color:#c0392b}}
#done{{display:none;text-align:center;padding:2rem 1rem}}#done h1{{font-size:1.3rem;margin:.6rem 0 .3rem}}.tick{{font-size:3rem}}
</style>
<main>
<header><b>Accomy</b><label style="margin:0"><span style="position:absolute;left:-999px">{t['language']}</span>
<select id="lang" aria-label="{t['language']}">{options}</select></label></header>
<div class="banner">{t['banner']}</div>
<div class="box">
<div class="row"><span>{t['trip']}</span><b>{html.escape(str(claims['name']))}</b></div>
<div class="row"><span>{t['order']}</span><b>{html.escape(str(claims['code']))}</b></div>
<h2>{t['items']}</h2><div class="tap">{t['tap']}</div>
{listing}
<div class="totalrow"><span>{t['total']}</span><span class="total">{total}</span></div>
<p class="check">{t['check']}</p></div>
<form class="box" id="form" autocomplete="off"{' hidden' if paid else ''}>
<label for="card">{t['card']}</label><input id="card" inputmode="numeric" placeholder="{pretty_card}" maxlength="23">
<div class="two"><div><label for="exp">{t['expiry']}</label><input id="exp" inputmode="numeric" placeholder="12 / 30" maxlength="7"></div>
<div><label for="cvc">{t['cvc']}</label><input id="cvc" inputmode="numeric" placeholder="123" maxlength="4"></div></div>
<div class="hint">{t['hint']}: <code>{pretty_card}</code><br><button type="button" id="fill">{t['fill']}</button></div>
<button id="pay">{t['pay']} {total}</button><div id="msg" role="alert"></div></form>
<div class="box" id="done"{' style="display:block"' if paid else ''}><div class="tick">✅</div><h1>{t['done']}</h1><p>{t['paid'] if paid else t['done_note']}</p></div>
</main>
<script>
const $ = (id) => document.getElementById(id), card = $("card");
$("lang").addEventListener("change", () => {{ location.search = "?lang=" + encodeURIComponent($("lang").value); }});
card.addEventListener("input", () => {{ card.value = card.value.replace(/\\D/g, "").slice(0, 19).replace(/(.{{4}})(?=.)/g, "$1 "); }});
$("fill").addEventListener("click", () => {{ card.value = "{pretty_card}"; $("exp").value = "12 / 30"; $("cvc").value = "123"; }});
$("form").addEventListener("submit", async (event) => {{
  event.preventDefault();
  // The card is checked here and never leaves this page: the demo must not receive card numbers.
  if (card.value.replace(/\\D/g, "") !== "{TEST_CARD}") {{ $("msg").textContent = {json.dumps(text['wrong'])}; return; }}
  $("msg").textContent = ""; $("pay").disabled = true; $("pay").textContent = {json.dumps(text['paying'])};
  try {{
    const answer = await fetch(location.pathname + "?lang={language}", {{method: "POST"}});
    if (answer.status === 409) {{ location.reload(); return; }}
    if (!answer.ok) throw new Error(answer.status);
    $("form").style.display = "none"; $("done").style.display = "block"; $("done").scrollIntoView();
  }} catch (error) {{
    $("msg").textContent = {json.dumps(text['failed'])}; $("pay").disabled = false; $("pay").textContent = {json.dumps(text['pay'] + ' ' + currency + ' ' + str(claims['amount']))};
  }}
}});
</script></html>"""


def _state(code: str) -> str:
    return (_TABLE.get_item(Key={"code": code}, ConsistentRead=True).get("Item") or {}).get("state", "UNPAID")


def _claim(code: str, state: str) -> tuple[bool, str]:
    """Record *state* for an order that has none yet. Returns whether this call recorded it, and the state the
    order is in afterwards: of two requests at once, exactly one wins."""
    now = int(time.time())
    try:
        _TABLE.put_item(Item={"code": code, "state": state, "at": now, "ttl": now + _STATE_DAYS * 86400},
                        ConditionExpression="attribute_not_exists(code)")
    except _TABLE.meta.client.exceptions.ConditionalCheckFailedException:
        return False, _state(code)
    return True, state


def _json(status: int, **body) -> dict:
    return _reply(status, json.dumps(body), "application/json")


def _send_telegram(chat_id: str, message: str) -> None:
    request = urllib.request.Request(
        f"https://api.telegram.org/bot{_secret('BOT_TOKEN_PARAM')}/sendMessage",
        data=json.dumps({"chat_id": chat_id, "text": message}).encode(),
        headers={"content-type": "application/json"})
    with urllib.request.urlopen(request, timeout=8):
        pass


def _pay(text: dict, claims: dict) -> dict:
    code = str(claims["code"])
    won, state = _claim(code, "PAID")
    if not won:  # paid before (say so, and send nothing again) or cancelled
        return _json(200, ok=True, already=True) if state == "PAID" else _json(409, ok=False, state=state)
    try:
        _send_telegram(str(claims["chat"]), text["message"].format(
            code=code, name=claims["name"], currency=claims["currency"], amount=claims["amount"]))
    # health: allow BLE001 -- any failure must leave the order payable, and a traceback would log the bot token
    except Exception as error:
        # Never the exception itself: its URL holds the bot token. The order is payable again.
        _TABLE.delete_item(Key={"code": code})
        print(f"telegram notice failed: {type(error).__name__} {getattr(error, 'code', '')}")
        return _json(502, ok=False)
    return _json(200, ok=True)


def handler(event, _context):
    method = event["requestContext"]["http"]["method"]
    path = event.get("rawPath", "")
    headers = event.get("headers") or {}
    asked = (event.get("queryStringParameters") or {}).get("lang")
    route, _, token = path[1:].partition("/")
    key = _secret("LINK_SECRET_PARAM")
    if (route, method) == ("v", "POST"):  # the bot cancelled the order
        claims = read_token(token, key, time.time(), "void")
        return _json(200, state=_claim(str(claims["code"]), "VOID")[1]) if claims else _json(403, ok=False)
    if (route, method) == ("s", "GET"):  # the bot asks whether the order was paid
        claims = read_token(token, key, time.time(), "status")
        return _json(200, state=_state(str(claims["code"]))) if claims else _json(403, ok=False)
    if route != "p" or method not in {"GET", "POST"}:
        return _notice("Accomy", "Accomy demo payment page.", 404)
    claims = read_token(token, key, time.time())
    if claims is None:
        text = _TEXT[pick_language(asked, headers.get("accept-language"), None)]
        return _notice(text["title"], text["expired"], 410)
    language = pick_language(asked, headers.get("accept-language"), claims.get("lang"))
    text = _TEXT[language]
    if method == "POST":
        return _pay(text, claims)
    state = _state(str(claims["code"]))
    if state == "VOID":
        return _notice(text["title"], text["cancelled"], 200)
    return _reply(200, _page(text, language, claims, paid=state == "PAID"))
