"""
Comprehensive LLM behavior probe for the Taigi Bus Agent.

Tests two independent dimensions per case:
  [D] Dispatch — did the router fire the right intent / tool?
  [F] Format   — is the response human-like (no AI artifacts)?

Behavior text checks (Expect.*) apply on top for LLM-path cases where the
response wording matters.

Run: uv run python scripts/probe_llm.py
"""

import asyncio
import re
import sys
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from dotenv import load_dotenv
load_dotenv()

from agent.router import Intent
from agent.session import AgentSession
from config import Settings, make_agent_session


# ── Intent → tool name (for display) ─────────────────────────────────────────

_INTENT_TO_TOOL: dict[Intent, str] = {
    Intent.ARRIVAL_TIME:          "get_arrivals_here",
    Intent.FIND_ROUTES_TO_DEST:   "find_routes_to_destination",
    Intent.OTHER_ROUTES_FOLLOWUP: "find_routes_to_destination",
    Intent.ROUTES_AT_STOP:        "get_routes_at_stop_here",
    Intent.STOP_STATUS:           "get_stop_arrival_statuses_here",
    Intent.ROUTE_STOPS_CLARIFY:   "get_route_stops",
    Intent.CHECK_STOP_ON_ROUTE:   "check_stop_on_route",
}

_INTENT_LABEL: dict[Intent, str] = {
    Intent.ROUTE_ONLY:            "R1",
    Intent.REMOTE_DESTINATION:    "R2",
    Intent.TIMETABLE_UNSUPPORTED: "R3",
    Intent.FIND_ROUTES_TO_DEST:   "R4",
    Intent.OTHER_ROUTES_FOLLOWUP: "R4b",
    Intent.ROUTES_AT_STOP:        "R5",
    Intent.STOP_STATUS:           "R6",
    Intent.ARRIVAL_TIME:          "R7",
    Intent.ROUTE_STOPS_CLARIFY:   "R8",
    Intent.CHECK_STOP_ON_ROUTE:   "R9",
}


# ── Behavior expectations (response text checks) ──────────────────────────────

class Expect(Enum):
    ASK_CLARIFY_ROUTE_INFO = "詢問想查路線什麼資訊"
    DECLINE_PLANNING       = "引導用地圖規劃，僅一句"
    DECLINE_UNSUPPORTED    = "說明不支援"
    ANSWER_YES_NO_ONLY     = "只答有／沒有，不列清單"
    ASK_ROUTE_NUMBER       = "詢問路線號碼"
    FREE                   = "無特定行為約束"


@dataclass
class TestCase:
    user_input: str
    description: str
    reset: bool = True
    expect: Expect = Expect.FREE
    # expected Intent from conv_state.last_intent after this turn.
    # None = UNKNOWN / LLM-handled; no dispatch assertion made.
    expect_intent: Intent | None = None


# ── Test suite ────────────────────────────────────────────────────────────────

CASES: list[TestCase] = [
    # ── Rule 1: pure route number → canned clarification ask ─────────────────
    TestCase("201",    "只說號碼，無問句",           True, Expect.ASK_CLARIFY_ROUTE_INFO, Intent.ROUTE_ONLY),
    TestCase("201路",  "號碼含路字，仍無問句",       True, Expect.ASK_CLARIFY_ROUTE_INFO, Intent.ROUTE_ONLY),
    TestCase("7000b",  "後綴字母路線，只說號碼",     True, Expect.ASK_CLARIFY_ROUTE_INFO, Intent.ROUTE_ONLY),
    TestCase("Y01",    "前綴字母路線，只說號碼",     True, Expect.ASK_CLARIFY_ROUTE_INFO, Intent.ROUTE_ONLY),
    TestCase("7126",   "四位數路線，只說號碼",       True, Expect.ASK_CLARIFY_ROUTE_INFO, Intent.ROUTE_ONLY),

    # ── Rule 2: cross-city / transfer → canned map redirect ──────────────────
    TestCase("我要去台中怎麼搭",   "遠距城市→redirect",        True, Expect.DECLINE_PLANNING, Intent.REMOTE_DESTINATION),
    TestCase("到台北要怎麼轉乘",   "跨縣市轉乘→redirect",      True, Expect.DECLINE_PLANNING, Intent.REMOTE_DESTINATION),
    TestCase("要不要轉乘才能到嘉義", "明確問轉乘→redirect",    True, Expect.DECLINE_PLANNING, Intent.REMOTE_DESTINATION),
    TestCase("台北的車呢",         "跨縣市口語→redirect",      True, Expect.DECLINE_PLANNING, Intent.REMOTE_DESTINATION),

    # ── Rule 3: timetable / inter-stop ETA → canned unsupported ──────────────
    TestCase("201路完整時刻表",        "完整時刻表",         True, Expect.DECLINE_UNSUPPORTED, Intent.TIMETABLE_UNSUPPORTED),
    TestCase("從這到斗六大概要幾分鐘", "站間行駛時間估算",   True, Expect.DECLINE_UNSUPPORTED, Intent.TIMETABLE_UNSUPPORTED),

    # ── Rule 4: local destination → router dispatches find_routes_to_destination
    TestCase("要搭哪台去斗六",        "本地目的地→查路線",       True, Expect.FREE, Intent.FIND_ROUTES_TO_DEST),
    TestCase("去虎尾的車怎麼搭",      "去...的車怎麼搭",         True, Expect.FREE, Intent.FIND_ROUTES_TO_DEST),
    TestCase("到高鐵站要搭什麼車",    "到某站要搭什麼車",        True, Expect.FREE, Intent.FIND_ROUTES_TO_DEST),
    TestCase("我想去斗六火車站",      "想去+本地站名",           True, Expect.FREE, Intent.FIND_ROUTES_TO_DEST),

    # ── Rule 5: routes at this stop → router dispatches get_routes_at_stop_here
    TestCase("這站有哪些路線",     "標準本站路線問法", True, Expect.FREE, Intent.ROUTES_AT_STOP),
    TestCase("這裡有幾路車",       "口語問法",         True, Expect.FREE, Intent.ROUTES_AT_STOP),
    TestCase("這個站牌有什麼公車", "另一口語問法",     True, Expect.FREE, Intent.ROUTES_AT_STOP),
    TestCase("我可以在這搭哪幾路", "主動問可搭路線",   True, Expect.FREE, Intent.ROUTES_AT_STOP),

    # ── Rule 6: all-bus status → router dispatches get_stop_arrival_statuses_here
    TestCase("現在還有哪些車", "標準全站狀態問法",     True, Expect.FREE, Intent.STOP_STATUS),
    TestCase("還有幾路在跑",   "口語問法",             True, Expect.FREE, Intent.STOP_STATUS),
    TestCase("末班車走了嗎",   "末班車查詢",           True, Expect.FREE, Intent.STOP_STATUS),
    TestCase("還有車嗎",       "最短問法",             True, Expect.FREE, Intent.STOP_STATUS),
    TestCase("現在幾點還有車", "包含時間語境的狀態問", True, Expect.FREE, Intent.STOP_STATUS),

    # ── Rule 7: specific route arrival → router dispatches get_arrivals_here ──
    TestCase("201路幾點到",           "路線+時間，標準",        True, Expect.FREE, Intent.ARRIVAL_TIME),
    TestCase("201還要等多久",         "等多久問法",             True, Expect.FREE, Intent.ARRIVAL_TIME),
    TestCase("下一班201什麼時候來",   "下一班...何時",          True, Expect.FREE, Intent.ARRIVAL_TIME),
    TestCase("201快到了嗎",           "快到了嗎",               True, Expect.FREE, Intent.ARRIVAL_TIME),
    TestCase("請問201多久會到",       "敬語問法",               True, Expect.FREE, Intent.ARRIVAL_TIME),
    TestCase("欸201到了沒",           "非正式口語",             True, Expect.FREE, Intent.ARRIVAL_TIME),
    TestCase("可以告訴我201幾點到嗎", "較長口語問句",           True, Expect.FREE, Intent.ARRIVAL_TIME),
    TestCase("7001路還要等多久",      "不存在路線→查無資料，不補推斷", True, Expect.FREE, Intent.ARRIVAL_TIME),

    # ── Rule 8: route stop list → router dispatches get_route_stops ──────────
    TestCase("201路停哪些站",       "站牌清單，標準",  True, Expect.FREE, Intent.ROUTE_STOPS_CLARIFY),
    # Below fall through to LLM (phrasing not covered by _ROUTE_STOPS_RE):
    TestCase("201路沿途有哪些站牌", "沿途問法→LLM",   True, Expect.FREE, None),
    TestCase("201去程怎麼走",       "去程問法→LLM",   True, Expect.FREE, None),
    TestCase("201回程的站牌",       "回程問法→LLM",   True, Expect.FREE, None),

    # ── Rule 9: has stop check → router dispatches check_stop_on_route ───────
    TestCase("201路有停斗六火車站嗎", "查有無停站，直問",   True, Expect.ANSWER_YES_NO_ONLY, Intent.CHECK_STOP_ON_ROUTE),
    TestCase("201有沒有停雲科大",     "口語問有無停站",     True, Expect.ANSWER_YES_NO_ONLY, Intent.CHECK_STOP_ON_ROUTE),
    # Below fall through to LLM (pattern not matched by _CHECK_STOP_RE):
    TestCase("搭201可以到高鐵站嗎",   "可以到→LLM",         True, Expect.ANSWER_YES_NO_ONLY, None),
    TestCase("201能到斗六嗎",         "能到→LLM",           True, Expect.ANSWER_YES_NO_ONLY, None),

    # ── LLM path: no route number, ask for it ────────────────────────────────
    TestCase("往斗六的車幾分鐘後到", "有目的地但無號碼→LLM問路線", True, Expect.ASK_ROUTE_NUMBER, None),

    # ── Multi-turn: context isolation ─────────────────────────────────────────
    TestCase("307路幾點到", "多輪第一輪：查307", True,  Expect.FREE, Intent.ARRIVAL_TIME),
    TestCase("201路呢",     "多輪第二輪：不可用307歷史", False, Expect.FREE, None),

    # ── Multi-turn: route context carry ───────────────────────────────────────
    TestCase("201路停哪些站",      "多輪：先查站牌",   True,  Expect.FREE, Intent.ROUTE_STOPS_CLARIFY),
    TestCase("那有停斗六火車站嗎", "多輪：追問→LLM",  False, Expect.ANSWER_YES_NO_ONLY, None),
]


# ── Format checks ─────────────────────────────────────────────────────────────

_TOOL_NARRATE_RE = re.compile(r"查詢中|正在查詢|資料獲取|已幫您查|幫您查")
_APOLOGY_RE      = re.compile(r"很抱歉|對不起|不好意思")
_TRAILING_RE     = re.compile(r"您可以(?!搭|乘)|如需|歡迎再|請問還有")
_AI_FILLER_RE    = re.compile(r"當然[！!]|很高興|以下是|希望.{0,6}幫|好的[，,]")
_REASONING_RE    = re.compile(r"根據.{0,10}判斷|按照規則|我先查|我來查|我為您")
_SELF_REF_RE     = re.compile(r"我已(?!有)|我幫您")


def check_format(text: str) -> list[str]:
    issues = []
    if _TOOL_NARRATE_RE.search(text):
        issues.append("含工具動作敘述")
    if _APOLOGY_RE.search(text):
        issues.append("含道歉語")
    if _TRAILING_RE.search(text):
        issues.append("含結尾引導語")
    if _AI_FILLER_RE.search(text):
        issues.append("含AI填充語")
    if _REASONING_RE.search(text):
        issues.append("含推理洩漏")
    if _SELF_REF_RE.search(text):
        issues.append("含自我指涉")
    sentences = [s.strip() for s in re.split(r"[。！？\n]", text) if s.strip()]
    if len(sentences) > 3:
        issues.append(f"回答超過三句（{len(sentences)} 句）")
    return issues


# ── Behavior checks ───────────────────────────────────────────────────────────

def check_behavior(text: str, expect: Expect) -> list[str]:
    issues = []
    if expect == Expect.ASK_CLARIFY_ROUTE_INFO:
        if not re.search(r"什麼資訊|想查|哪條|想問", text):
            issues.append("應詢問想查什麼資訊")
    elif expect == Expect.DECLINE_PLANNING:
        if "地圖" not in text and "規劃" not in text:
            issues.append("應引導使用地圖規劃")
        sentences = [s.strip() for s in re.split(r"[。！？]", text) if s.strip()]
        if len(sentences) > 1:
            issues.append(f"規劃拒絕應僅一句，實際 {len(sentences)} 句")
    elif expect == Expect.DECLINE_UNSUPPORTED:
        if re.search(r"地圖|幾路|想查什麼", text):
            issues.append("誤引導地圖或詢問路線，應直接說明不支援")
    elif expect == Expect.ANSWER_YES_NO_ONLY:
        sentences = [s.strip() for s in re.split(r"[。！？\n]", text) if s.strip()]
        if len(sentences) > 2:
            issues.append(f"應只回有/沒有，輸出 {len(sentences)} 句（疑似列出清單）")
        if not re.search(r"^有|^能|^會|沒有|可以到|無法到|不停|能到|不能|會經過", text):
            issues.append("應含明確的有/沒有回答")
    elif expect == Expect.ASK_ROUTE_NUMBER:
        if not re.search(r"幾路|路線號碼|哪條路|哪路", text):
            issues.append("應詢問路線號碼")
    return issues


# ── Dispatch check ────────────────────────────────────────────────────────────

def check_dispatch(session: AgentSession, expect_intent: Intent | None) -> list[str]:
    """Assert conv_state.last_intent matches expected intent (router-dispatched cases)."""
    if expect_intent is None:
        return []
    actual = session.conv_state.last_intent
    if actual != expect_intent:
        actual_str = actual.value if actual else "None"
        return [f"期望 intent={expect_intent.value}，實際={actual_str}"]
    return []


def get_dispatch_display(session: AgentSession) -> str:
    """Human-readable dispatch info: router intent → tool, or LLM tool from messages."""
    intent = session.conv_state.last_intent
    if intent is not None:
        tool = _INTENT_TO_TOOL.get(intent, "（無，canned）")
        return f"{intent.value} → {tool}"
    # UNKNOWN / LLM path: scan messages for LLM-dispatched tool calls
    for msg in reversed(session.messages):
        if msg["role"] == "user":
            break
        if msg["role"] == "assistant":
            for tc in msg.get("tool_calls") or []:
                name = tc["function"]["name"]
                if name:
                    return f"llm → {name}"
    return "llm → （無）"


# ── Runner ─────────────────────────────────────────────────────────────────────

async def main() -> None:
    settings = Settings.from_env()
    session = make_agent_session(settings)

    print(f"模型：{settings.llm_model}")
    print(f"API ：{settings.llm_base_url}")
    print("=" * 72)

    results: list[dict] = []

    for case in CASES:
        if case.reset:
            session = make_agent_session(settings)

        intent_label = _INTENT_LABEL.get(case.expect_intent, "LLM") if case.expect_intent else "LLM"
        print(f"\n[{intent_label}] {case.description}")
        print(f"  輸入：{case.user_input}")

        try:
            response = await session.respond(case.user_input)
        except Exception as e:
            response = f"[ERROR] {e}"
            results.append({
                "case": case, "response": response,
                "d_issues": [f"exception: {e}"], "f_issues": [],
            })
            print(f"  ✗ ERROR: {e}")
            continue

        dispatch_str = get_dispatch_display(session)
        d_issues = check_dispatch(session, case.expect_intent) + check_behavior(response, case.expect)
        f_issues = check_format(response)

        d_ok = "✓" if not d_issues else "✗"
        f_ok = "✓" if not f_issues else "✗"

        print(f"  派發：{dispatch_str}")
        print(f"  回覆：{response}")
        for i in d_issues:
            print(f"  [D]⚠ {i}")
        for i in f_issues:
            print(f"  [F]⚠ {i}")
        print(f"  派發 {d_ok}  格式 {f_ok}")

        results.append({
            "case": case, "response": response,
            "d_issues": d_issues, "f_issues": f_issues,
        })

    # ── Summary ──────────────────────────────────────────────────────────────
    total = len(results)
    d_pass = sum(1 for r in results if not r["d_issues"])
    f_pass = sum(1 for r in results if not r["f_issues"])
    both_pass = sum(1 for r in results if not r["d_issues"] and not r["f_issues"])

    print("\n" + "=" * 72)
    print("總結")
    print(f"  派發正確：{d_pass}/{total}")
    print(f"  格式正確：{f_pass}/{total}")
    print(f"  全部通過：{both_pass}/{total}")

    by_label: dict[str, list[dict]] = {}
    for r in results:
        if r["d_issues"] or r["f_issues"]:
            intent = r["case"].expect_intent
            tag = _INTENT_LABEL.get(intent, "LLM") if intent else "LLM"
            by_label.setdefault(tag, []).append(r)

    if by_label:
        print("\n失敗分布：")
        for tag in sorted(by_label):
            cases = by_label[tag]
            print(f"  {tag}: {len(cases)} 案例失敗")
            for r in cases:
                short = r["response"][:55].replace("\n", " ")
                print(f"    [{r['case'].description}] → {short}")
                for i in r["d_issues"]:
                    print(f"      [D] {i}")
                for i in r["f_issues"]:
                    print(f"      [F] {i}")


if __name__ == "__main__":
    asyncio.run(main())
