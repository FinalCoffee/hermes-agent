"""Deterministic scope screen shared by the policy hook and ``research_web``.

The system prompt is the primary control (the model refuses); these patterns are the backstop that
stops an obvious out-of-scope request before it spends a supplier call. They are kept tight on
purpose: a false positive refuses a real travel question, which costs more than a missed one that
the prompt still catches ("travel in Java", "airport security scan rules", "create an itinerary"
must all pass).
"""

from __future__ import annotations

import re

CODE = "code"
SECURITY = "security"
PRIVACY = "privacy"
MATH = "math"
ROLE_OVERRIDE = "role_override"

_CODE_RE = re.compile(
    r"```|\bdef\s|\bimport\s|#include|\bfunction\s|</[a-z]|console\.log"
    r"|\bclass\s+\w+\s*[:({]|=>|\bpublic\s+static\b|\bSELECT\s+.+\bFROM\b",
    re.IGNORECASE)
# A coding verb within ~40 characters of a code noun.
_CODE_NL_RE = re.compile(
    r"\b(write|generate|create|build|debug|fix|refactor|implement|code)\b"
    r".{0,40}\b(code|script|function|regex|scraper|crawler|snippet|algorithm|css|html|sql|api\s+call)\b"
    r"|(寫|写|產生|产生|生成|幫我做|帮我做|除錯|调试|debug|修(正|復|复))"
    r".{0,12}(程式碼|程式|代碼|代码|腳本|脚本|爬蟲|爬虫|函式|函數|函数|正則|正则|演算法|算法|sql|html|css|python|javascript)",
    re.IGNORECASE)
# Basic arithmetic stays allowed (splitting costs, luggage weight); only symbolic or advanced math is out.
_MATH_RE = re.compile(
    r"\d\s*(\^|\*\*)\s*\d"
    r"|\b(integral|derivative|differentiate|integrate|factorial|logarithm|sqrt|square\s+root|cube\s+root"
    r"|sine|cosine|tangent|matrix|polynomial|quadratic|standard\s+deviation)\b"
    r"|\bsolve\b[^.?!]{0,40}(\bequation\b|=|\bfor\s+[a-z]\b)"
    r"|微積分|微积分|積分|导数|導數|方程式|矩陣|矩阵|標準差|标准差",
    re.IGNORECASE)
# Each term carries its attack target inline, so "security scan" and the "port" in "airport" never match.
_CYBER_RE = re.compile(
    r"\b(sql\s*injection|sqli|xss|csrf|exploit|payload|reverse\s+shell|buffer\s+overflow"
    r"|privilege\s+escalation|bypass\s+(auth|authentication|login|2fa|mfa)"
    r"|crack(ing)?\s+(a\s+)?(password|wifi|wpa|hash)|brute[\s-]?force|keylogger"
    r"|ransomware|malware|spyware|ddos|botnet|phishing"
    r"|port[\s-]*scan(?:ning)?|vulnerability[\s-]*scan(?:ning)?|vuln[\s-]*scan(?:ning)?"
    r"|pentest|penetration[\s-]*test|nmap|masscan|nikto|metasploit|sqlmap"
    r"|how\s+to\s+hack|hack\s+(into|a\b))\b"
    r"|駭入|黑入|入侵(系統|系统|網站|网站|帳號|账号|伺服器|服务器)|破解(密碼|密码|帳號|账号|wifi)"
    r"|木馬|木马|勒索軟體|勒索软件|惡意程式|恶意软件|釣魚(網站|郵件)|钓鱼(网站|邮件)|漏洞(利用|掃描|扫描)|滲透測試|渗透测试",
    re.IGNORECASE)
_PRIVACY_RE = re.compile(
    r"\b(home\s+address|personal\s+(phone|email|address)|credit\s+card\s+number"
    r"|social\s+security\s+number|ssn|passport\s+number|bank\s+account\s+number"
    r"|leaked\s+(password|credential)s?|dox(x?ing)?)\b"
    r"|(他|她|某人|別人|别人|這個人|这个人).{0,8}(住址|住哪|地址|電話|电话|手機號|手机号|身分證|身份证|護照號|护照号)"
    r"|人肉搜索|肉搜|起底",
    re.IGNORECASE)
_ROLE_OVERRIDE_RE = re.compile(
    r"\b(ignore|disregard|forget)\s+(all\s+|any\s+)?(the\s+|your\s+)?(previous|prior|above|earlier|system)\s+"
    r"(instructions?|rules?|prompts?)"
    r"|\byou\s+are\s+now\s+(a|an|the)\b|\bact\s+as\s+(a|an)\s+(?!travel|tour|local)"
    r"|\b(reveal|show|print|repeat)\s+(me\s+)?(your\s+|the\s+)?(system\s+prompt|hidden\s+instructions?)"
    r"|\b(jailbreak|developer\s+mode|dan\s+mode)\b"
    r"|(忽略|無視|无视|忘記|忘记).{0,6}(之前|以上|上面|先前|系統|系统|所有).{0,6}(指示|指令|規則|规则|提示|設定|设定)"
    r"|你現在是|你现在是|(顯示|显示|告訴我|告诉我|輸出|输出|印出).{0,6}(系統提示|系统提示|提示詞|提示词|system\s*prompt)",
    re.IGNORECASE)

# Order matters only for the label: a request that is both code and attack reads as the attack.
_SCREENS = (
    (SECURITY, (_CYBER_RE,)),
    (PRIVACY, (_PRIVACY_RE,)),
    (CODE, (_CODE_RE, _CODE_NL_RE)),
    (MATH, (_MATH_RE,)),
)


def reject_reason(text: str) -> str | None:
    """The out-of-scope task category *text* asks for, or ``None`` when it is in scope."""
    for label, patterns in _SCREENS:
        if any(pattern.search(text) for pattern in patterns):
            return label
    return None


def is_role_override(text: str) -> bool:
    """Whether *text* tries to replace the assistant's role or extract its instructions."""
    return bool(_ROLE_OVERRIDE_RE.search(text))
