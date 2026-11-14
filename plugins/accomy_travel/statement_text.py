"""The words of an order's statement, in every language it is written in.

A statement is written in the language of the conversation that asks for it, when that is one of these, and in
English for any other. They are the languages the payment page speaks (``payment.PAGE_LANGUAGES``).

Each language says the same things under the same keys; statement.py puts an order's facts into them, and
statement_pdf.py draws the result. What an order holds as a name or a code (a hotel, a room, a flight number, an
airport) stays as the order holds it. A language other than English writes a route with an arrow, so that the
codes around it read the same in every script.

Dates are templates too: ``{y}`` ``{m}`` ``{d}`` are numbers, ``{mon}`` and ``{wd}`` the month and the weekday
from the language's own lists. Nothing here uses strftime, which follows the locale of the process.
"""

from __future__ import annotations

FALLBACK = "en"


def _same(text: str) -> tuple[str, str]:
    """A count that reads the same for one and for many."""
    return text, text


# A count is (for one, for many). A payment state is (the status box, the two lines under "Payment").
WORDS: dict[str, dict] = {
    "en": {
        "months": ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"),
        "weekdays": ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"),
        "day": "{d} {mon} {y}", "short_day": "{d} {mon}", "weekday_day": "{wd} {d} {mon}",
        "span_in_month": "{d1}–{day2}", "span_in_year": "{short1}–{day2}", "utc": "{day} UTC",
        "document": "Document", "title": "Order statement and itinerary", "reference": "Reference",
        "issued": "Issued {day}",
        "paid": ("Demonstration · paid", "Demonstration payment made", "Nothing was charged"),
        "unpaid": ("Demonstration · not paid", "Not paid", "A demonstration: nothing is charged"),
        "unknown": ("Demonstration order", "Payment not known", "A demonstration: nothing is charged"),
        "ordered_by": "Ordered by", "travellers": "Travellers", "booked_through": "Booked through",
        "service": "Demonstration service",
        "description": "Description", "dates": "Dates", "quantity": "Quantity", "amount": "Amount",
        "one_way": "One way", "round_trip": "Round trip", "multi_city": "Multi-city",
        "route": "{origin} to {destination}",
        "flight_times": "{departs} {origin} to {arrives} {destination}{later}", "later": " on {day}",
        "cabins": {"economy": "Economy", "premiumeconomy": "Premium economy", "business": "Business",
                   "first": "First"},
        "passengers": ("{n} passenger", "{n} passengers"), "rooms": ("{n} room", "{n} rooms"),
        "guests": ("{n} guest", "{n} guests"), "nights": ("{n} night", "{n} nights"),
        "tickets": ("{n} ticket", "{n} tickets"),
        "starts": "Starts {time}, local time",
        "converted": "Converted from {currency} {amount}: an approximate price",
        "subtotal": "Subtotal", "total": "Total", "not_charged": "Demonstration: nothing was charged",
        "payment": "Payment", "order": "Order", "trip": "Trip",
        "order_lines": ("Demonstration order", "Not placed with a supplier"), "placed": "Placed {day}",
        "trip_code": "Trip {code}",
        "banner": "Demonstration order. It is not a ticket or a booking: no airline, hotel or organiser holds it, "
                  "and nothing was charged.",
        "what_it_is": ("What this document is.",
                       "A record of a demonstration order made with Accomy AI Travel: the itinerary and the prices "
                       "as they were shown when the order was placed."),
        "what_it_is_not": ("What it is not.",
                           "This is not a ticket, a booking confirmation, a receipt or a tax invoice. The order was "
                           "not placed with any airline, hotel or event organiser, and nothing was charged."),
        "lead_gap": " ",
        "footer": "Generated automatically, so no signature is required. Questions about this order: ask the "
                  "assistant in the chat it was placed in.",
        "page": "page {n}", "pdf_title": "Accomy order statement {reference}", "pdf_subject": "Demonstration order",
    },
    "zh-Hant": {
        "months": None,
        "weekdays": ("週一", "週二", "週三", "週四", "週五", "週六", "週日"),
        "day": "{y}年{m}月{d}日", "short_day": "{m}月{d}日", "weekday_day": "{m}月{d}日（{wd}）",
        "span_in_month": "{day1}–{d2}日", "span_in_year": "{day1}–{short2}", "utc": "{day}（UTC）",
        "document": "文件", "title": "訂單明細與行程表", "reference": "訂單編號",
        "issued": "開立日期 {day}",
        "paid": ("示範 · 已付款", "已完成示範付款", "未實際扣款"),
        "unpaid": ("示範 · 未付款", "尚未付款", "示範訂單，不會實際扣款"),
        "unknown": ("示範訂單", "付款狀態不明", "示範訂單，不會實際扣款"),
        "ordered_by": "訂購人", "travellers": "旅客", "booked_through": "訂購管道",
        "service": "示範服務",
        "description": "項目", "dates": "日期", "quantity": "數量", "amount": "金額",
        "one_way": "單程", "round_trip": "來回", "multi_city": "多個城市",
        "route": "{origin} → {destination}",
        "flight_times": "{departs} {origin} → {arrives} {destination}{later}", "later": "（{day}抵達）",
        "cabins": {"economy": "經濟艙", "premiumeconomy": "豪華經濟艙", "business": "商務艙", "first": "頭等艙"},
        "passengers": _same("{n} 位旅客"), "rooms": _same("{n} 間客房"), "guests": _same("{n} 位房客"),
        "nights": _same("{n} 晚"), "tickets": _same("{n} 張票"),
        "starts": "當地時間 {time} 開始",
        "converted": "由 {currency} {amount} 換算，為約略價格",
        "subtotal": "小計", "total": "總計", "not_charged": "示範訂單：未實際扣款",
        "payment": "付款", "order": "訂單", "trip": "行程",
        "order_lines": ("示範訂單", "未向供應商下單"), "placed": "下單日期 {day}",
        "trip_code": "行程編號 {code}",
        "banner": "示範訂單，不是機票也不是訂位憑證：航空公司、飯店或主辦單位都沒有這筆訂單，也未實際扣款。",
        "what_it_is": ("文件說明：", "這是透過 Accomy AI Travel 建立的示範訂單紀錄，內容為下單當時顯示的行程與價格。"),
        "what_it_is_not": ("請注意：",
                           "本文件不是機票、訂位確認、收據或統一發票。這筆訂單並未向任何航空公司、飯店或活動主辦單位下單，"
                           "也未實際扣款。"),
        "lead_gap": "",
        "footer": "本文件由系統自動產生，無需簽名。對這筆訂單有疑問，請在下單的對話中詢問助理。",
        "page": "第 {n} 頁", "pdf_title": "Accomy 訂單明細 {reference}", "pdf_subject": "示範訂單",
    },
    "zh-Hans": {
        "months": None,
        "weekdays": ("周一", "周二", "周三", "周四", "周五", "周六", "周日"),
        "day": "{y}年{m}月{d}日", "short_day": "{m}月{d}日", "weekday_day": "{m}月{d}日（{wd}）",
        "span_in_month": "{day1}–{d2}日", "span_in_year": "{day1}–{short2}", "utc": "{day}（UTC）",
        "document": "文件", "title": "订单明细与行程单", "reference": "订单编号",
        "issued": "开具日期 {day}",
        "paid": ("演示 · 已付款", "已完成演示付款", "未实际扣款"),
        "unpaid": ("演示 · 未付款", "尚未付款", "演示订单，不会实际扣款"),
        "unknown": ("演示订单", "付款状态未知", "演示订单，不会实际扣款"),
        "ordered_by": "订购人", "travellers": "旅客", "booked_through": "预订渠道",
        "service": "演示服务",
        "description": "项目", "dates": "日期", "quantity": "数量", "amount": "金额",
        "one_way": "单程", "round_trip": "往返", "multi_city": "多程",
        "route": "{origin} → {destination}",
        "flight_times": "{departs} {origin} → {arrives} {destination}{later}", "later": "（{day}到达）",
        "cabins": {"economy": "经济舱", "premiumeconomy": "超级经济舱", "business": "商务舱", "first": "头等舱"},
        "passengers": _same("{n} 位旅客"), "rooms": _same("{n} 间客房"), "guests": _same("{n} 位住客"),
        "nights": _same("{n} 晚"), "tickets": _same("{n} 张票"),
        "starts": "当地时间 {time} 开始",
        "converted": "由 {currency} {amount} 换算，为约略价格",
        "subtotal": "小计", "total": "总计", "not_charged": "演示订单：未实际扣款",
        "payment": "付款", "order": "订单", "trip": "行程",
        "order_lines": ("演示订单", "未向供应商下单"), "placed": "下单日期 {day}",
        "trip_code": "行程编号 {code}",
        "banner": "演示订单，不是机票也不是预订凭证：航空公司、酒店或主办方都没有这笔订单，也未实际扣款。",
        "what_it_is": ("文件说明：", "这是通过 Accomy AI Travel 创建的演示订单记录，内容为下单时显示的行程与价格。"),
        "what_it_is_not": ("请注意：",
                           "本文件不是机票、预订确认、收据或发票。这笔订单并未向任何航空公司、酒店或活动主办方下单，"
                           "也未实际扣款。"),
        "lead_gap": "",
        "footer": "本文件由系统自动生成，无需签名。对这笔订单有疑问，请在下单的对话中询问助手。",
        "page": "第 {n} 页", "pdf_title": "Accomy 订单明细 {reference}", "pdf_subject": "演示订单",
    },
    "ja": {
        "months": None,
        "weekdays": ("月", "火", "水", "木", "金", "土", "日"),
        "day": "{y}年{m}月{d}日", "short_day": "{m}月{d}日", "weekday_day": "{m}月{d}日（{wd}）",
        "span_in_month": "{day1}–{d2}日", "span_in_year": "{day1}–{short2}", "utc": "{day}（UTC）",
        "document": "書類", "title": "ご注文明細・旅程表", "reference": "注文番号",
        "issued": "発行日 {day}",
        "paid": ("デモ · 支払い済み", "デモ決済済み", "実際の請求はありません"),
        "unpaid": ("デモ · 未払い", "未払い", "デモのため請求は発生しません"),
        "unknown": ("デモ注文", "支払い状況は不明", "デモのため請求は発生しません"),
        "ordered_by": "ご注文者", "travellers": "ご旅行者", "booked_through": "ご利用サービス",
        "service": "デモサービス",
        "description": "内容", "dates": "日程", "quantity": "数量", "amount": "金額",
        "one_way": "片道", "round_trip": "往復", "multi_city": "複数都市",
        "route": "{origin} → {destination}",
        "flight_times": "{departs} {origin} → {arrives} {destination}{later}", "later": "（{day}着）",
        "cabins": {"economy": "エコノミークラス", "premiumeconomy": "プレミアムエコノミー", "business": "ビジネスクラス",
                   "first": "ファーストクラス"},
        "passengers": _same("{n}名"), "rooms": _same("{n}室"), "guests": _same("{n}名"),
        "nights": _same("{n}泊"), "tickets": _same("{n}枚"),
        "starts": "現地時間 {time} 開始",
        "converted": "{currency} {amount} から換算した概算価格",
        "subtotal": "小計", "total": "合計", "not_charged": "デモ：実際の請求はありません",
        "payment": "お支払い", "order": "ご注文", "trip": "旅程",
        "order_lines": ("デモ注文", "サプライヤーへの発注なし"), "placed": "注文日 {day}",
        "trip_code": "旅程番号 {code}",
        "banner": "デモ注文です。航空券や予約ではありません。航空会社・ホテル・主催者にこの注文はなく、請求も発生していません。",
        "what_it_is": ("この書類について：",
                       "Accomy AI Travel で作成したデモ注文の記録です。注文時に表示された旅程と価格を記載しています。"),
        "what_it_is_not": ("ご注意：",
                           "この書類は航空券、予約確認書、領収書、適格請求書（インボイス）ではありません。この注文は航空会社・ホテル・"
                           "イベント主催者には発注されておらず、請求も発生していません。"),
        "lead_gap": "",
        "footer": "この書類は自動生成されたため、署名は不要です。この注文についてのご質問は、注文したチャットでアシスタントにお尋ねください。",
        "page": "{n} ページ", "pdf_title": "Accomy ご注文明細 {reference}", "pdf_subject": "デモ注文",
    },
    "ko": {
        "months": None,
        "weekdays": ("월", "화", "수", "목", "금", "토", "일"),
        "day": "{y}년 {m}월 {d}일", "short_day": "{m}월 {d}일", "weekday_day": "{m}월 {d}일 ({wd})",
        "span_in_month": "{day1}–{d2}일", "span_in_year": "{day1}–{short2}", "utc": "{day} (UTC)",
        "document": "문서", "title": "주문 명세서 및 여정표", "reference": "주문 번호",
        "issued": "발행일 {day}",
        "paid": ("데모 · 결제 완료", "데모 결제 완료", "실제로 청구된 금액 없음"),
        "unpaid": ("데모 · 미결제", "미결제", "데모이므로 청구되지 않습니다"),
        "unknown": ("데모 주문", "결제 상태 확인 불가", "데모이므로 청구되지 않습니다"),
        "ordered_by": "주문자", "travellers": "여행자", "booked_through": "예약 경로",
        "service": "데모 서비스",
        "description": "항목", "dates": "날짜", "quantity": "수량", "amount": "금액",
        "one_way": "편도", "round_trip": "왕복", "multi_city": "다구간",
        "route": "{origin} → {destination}",
        "flight_times": "{departs} {origin} → {arrives} {destination}{later}", "later": " ({day} 도착)",
        "cabins": {"economy": "이코노미석", "premiumeconomy": "프리미엄 이코노미석", "business": "비즈니스석",
                   "first": "일등석"},
        "passengers": _same("탑승객 {n}명"), "rooms": _same("객실 {n}개"), "guests": _same("투숙객 {n}명"),
        "nights": _same("{n}박"), "tickets": _same("{n}매"),
        "starts": "현지 시간 {time} 시작",
        "converted": "{currency} {amount}에서 환산한 대략적인 가격",
        "subtotal": "소계", "total": "합계", "not_charged": "데모: 실제로 청구된 금액이 없습니다",
        "payment": "결제", "order": "주문", "trip": "여행",
        "order_lines": ("데모 주문", "공급업체에 접수되지 않음"), "placed": "주문일 {day}",
        "trip_code": "여행 번호 {code}",
        "banner": "데모 주문입니다. 항공권이나 예약이 아니며, 항공사·호텔·주최 측에 이 주문은 없고 청구된 금액도 없습니다.",
        "what_it_is": ("문서 안내:",
                       "Accomy AI Travel에서 만든 데모 주문의 기록입니다. 주문 당시 표시된 여정과 가격을 담고 있습니다."),
        "what_it_is_not": ("유의 사항:",
                           "이 문서는 항공권, 예약 확인서, 영수증 또는 세금계산서가 아닙니다. 이 주문은 어떤 항공사, 호텔, "
                           "행사 주최 측에도 접수되지 않았으며 청구된 금액도 없습니다."),
        "lead_gap": " ",
        "footer": "이 문서는 자동으로 생성되어 서명이 필요 없습니다. 이 주문에 대한 문의는 주문한 채팅에서 어시스턴트에게 해 주세요.",
        "page": "{n}쪽", "pdf_title": "Accomy 주문 명세서 {reference}", "pdf_subject": "데모 주문",
    },
}


def words(language: str | None) -> dict:
    """The words of *language*, or English's for a language the statement is not written in."""
    return WORDS.get(language) or WORDS[FALLBACK]
