"""
Comprehensive LLM behavior probe for the Taigi Bus Agent.

Tests two independent dimensions per case:
  [B] Behavior  — did the agent follow the correct decision rule?
  [F] Format    — is the response human-like (no AI artifacts)?

Run: uv run python scripts/probe_llm.py
"""

import asyncio
import re
import sys
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from dotenv import load_dotenv
load_dotenv()

from agent.session import AgentSession
from config import Settings, make_agent_session


# ── Behavior expectations ─────────────────────────────────────────────────────

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
    expect_tool: str | None = None   # tool name that MUST be called
    expect_no_tool: bool = False     # no tool should be called at all


# ── Test suite ────────────────────────────────────────────────────────────────

CASES: list[TestCase] = [
    # ── Rule 1: pure route number → ask clarification ─────────────────────────
    TestCase("201",    "只說號碼，無問句",           True, Expect.ASK_CLARIFY_ROUTE_INFO, expect_no_tool=True),
    TestCase("201路",  "號碼含路字，仍無問句",       True, Expect.ASK_CLARIFY_ROUTE_INFO, expect_no_tool=True),
    TestCase("7000b",  "後綴字母路線，只說號碼",     True, Expect.ASK_CLARIFY_ROUTE_INFO, expect_no_tool=True),
    TestCase("Y01",    "前綴字母路線，只說號碼",     True, Expect.ASK_CLARIFY_ROUTE_INFO, expect_no_tool=True),
    TestCase("7126",   "四位數路線，只說號碼",       True, Expect.ASK_CLARIFY_ROUTE_INFO, expect_no_tool=True),

    # ── Rule 2: cross-city / transfer → single-sentence map redirect ────────
    TestCase("我要去台中怎麼搭",   "遠距城市→redirect",        True, Expect.DECLINE_PLANNING, expect_no_tool=True),
    TestCase("到台北要怎麼轉乘",   "跨縣市轉乘→redirect",      True, Expect.DECLINE_PLANNING, expect_no_tool=True),
    TestCase("要不要轉乘才能到嘉義", "明確問轉乘→redirect",    True, Expect.DECLINE_PLANNING, expect_no_tool=True),

    # ── Rule 3: local destination query → use tools to find route ────────────
    TestCase("要搭哪台去斗六",        "本地目的地→查路線",       True, Expect.FREE, expect_tool="get_routes_at_stop"),
    TestCase("去虎尾的車怎麼搭",      "去...的車怎麼搭",         True, Expect.FREE, expect_tool="get_routes_at_stop"),
    TestCase("到高鐵站要搭什麼車",    "到某站要搭什麼車",        True, Expect.FREE, expect_tool="get_routes_at_stop"),
    TestCase("我想去斗六火車站",      "想去+本地站名",           True, Expect.FREE, expect_tool="get_routes_at_stop"),

    # ── Rule 4: unsupported features ─────────────────────────────────────────
    TestCase("201路完整時刻表",        "完整時刻表",         True, Expect.DECLINE_UNSUPPORTED, expect_no_tool=True),
    TestCase("從這到斗六大概要幾分鐘", "站間行駛時間估算",   True, Expect.DECLINE_UNSUPPORTED, expect_no_tool=True),

    # ── Rule 4: routes at this stop ───────────────────────────────────────────
    TestCase("這站有哪些路線",     "標準本站路線問法", True, Expect.FREE, expect_tool="get_routes_at_stop"),
    TestCase("這裡有幾路車",       "口語問法",         True, Expect.FREE, expect_tool="get_routes_at_stop"),
    TestCase("這個站牌有什麼公車", "另一口語問法",     True, Expect.FREE, expect_tool="get_routes_at_stop"),
    TestCase("我可以在這搭哪幾路", "主動問可搭路線",   True, Expect.FREE, expect_tool="get_routes_at_stop"),

    # ── Rule 5: all arrivals status ──────────────────────────────────────────
    TestCase("現在還有哪些車", "標準全站狀態問法",     True, Expect.FREE, expect_tool="get_stop_arrival_statuses_here"),
    TestCase("還有幾路在跑",   "口語問法",             True, Expect.FREE, expect_tool="get_stop_arrival_statuses_here"),
    TestCase("末班車走了嗎",   "末班車查詢",           True, Expect.FREE, expect_tool="get_stop_arrival_statuses_here"),
    TestCase("還有車嗎",       "最短問法",             True, Expect.FREE, expect_tool="get_stop_arrival_statuses_here"),
    TestCase("現在幾點還有車", "包含時間語境的狀態問", True, Expect.FREE, expect_tool="get_stop_arrival_statuses_here"),

    # ── Rule 6: specific route arrival ───────────────────────────────────────
    TestCase("201路幾點到",           "路線+時間，標準",        True, Expect.FREE, expect_tool="get_arrivals_here"),
    TestCase("201還要等多久",         "等多久問法",             True, Expect.FREE, expect_tool="get_arrivals_here"),
    TestCase("下一班201什麼時候來",   "下一班...何時",          True, Expect.FREE, expect_tool="get_arrivals_here"),
    TestCase("201快到了嗎",           "快到了嗎",               True, Expect.FREE, expect_tool="get_arrivals_here"),
    TestCase("請問201多久會到",       "敬語問法",               True, Expect.FREE, expect_tool="get_arrivals_here"),
    TestCase("欸201到了沒",           "非正式口語",             True, Expect.FREE, expect_tool="get_arrivals_here"),
    TestCase("可以告訴我201幾點到嗎", "較長口語問句",           True, Expect.FREE, expect_tool="get_arrivals_here"),
    TestCase("7001路還要等多久",      "不存在路線→查無資料，不補推斷", True, Expect.FREE, expect_tool="get_arrivals_here"),

    # ── Rule 7: route stops list ──────────────────────────────────────────────
    TestCase("201路停哪些站",         "站牌清單，標準",  True, Expect.FREE, expect_tool="get_route_stops"),
    TestCase("201路沿途有哪些站牌",   "沿途站牌問法",    True, Expect.FREE, expect_tool="get_route_stops"),
    TestCase("201去程怎麼走",         "去程站牌",        True, Expect.FREE, expect_tool="get_route_stops"),
    TestCase("201回程的站牌",         "回程站牌",        True, Expect.FREE, expect_tool="get_route_stops"),

    # ── Rule 8: has stop check → yes/no only ─────────────────────────────────
    TestCase("201路有停斗六火車站嗎", "查有無停站，直問",     True, Expect.ANSWER_YES_NO_ONLY, expect_tool="get_route_stops"),
    TestCase("搭201可以到高鐵站嗎",   "到某站嗎→有無",        True, Expect.ANSWER_YES_NO_ONLY, expect_tool="get_route_stops"),
    TestCase("201有沒有停雲科大",     "口語問有無停站",       True, Expect.ANSWER_YES_NO_ONLY, expect_tool="get_route_stops"),
    TestCase("201能到斗六嗎",         "能到...嗎",            True, Expect.ANSWER_YES_NO_ONLY, expect_tool="get_route_stops"),

    # ── Rule 9: route-related, no number ─────────────────────────────────────
    TestCase("往斗六的車幾分鐘後到", "有目的地但無號碼→問路線", True, Expect.ASK_ROUTE_NUMBER, expect_no_tool=True),
    TestCase("台北的車呢",           "跨縣市口語→redirect",     True, Expect.DECLINE_PLANNING, expect_no_tool=True),

    # ── Multi-turn: context isolation ─────────────────────────────────────────
    TestCase("307路幾點到",   "多輪第一輪：查307",                   True,  Expect.FREE, expect_tool="get_arrivals_here"),
    TestCase("201路呢",       "多輪第二輪：問201，不可用307歷史資料", False, Expect.FREE, expect_tool="get_arrivals_here"),

    # ── Multi-turn: route context carry ───────────────────────────────────────
    TestCase("201路停哪些站",         "多輪：先查站牌",            True,  Expect.FREE, expect_tool="get_route_stops"),
    TestCase("那有停斗六火車站嗎",    "多輪：追問，不應呼叫工具", False, Expect.ANSWER_YES_NO_ONLY),
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


def check_tool_usage(
    called: list[str],
    expect_tool: str | None,
    expect_no_tool: bool,
) -> list[str]:
    issues = []
    if expect_no_tool and called:
        issues.append(f"不應呼叫工具，但呼叫了 {called}")
    if expect_tool and expect_tool not in called:
        issues.append(f"應呼叫 {expect_tool}，實際 {called or '（無）'}")
    return issues


def get_called_tools(session: AgentSession) -> list[str]:
    """Extract tool names called in the most recent turn."""
    tools: list[str] = []
    for msg in reversed(session.messages):
        if msg["role"] == "user":
            break
        if msg["role"] == "assistant":
            for tc in msg.get("tool_calls") or []:
                name = tc["function"]["name"]
                if name:
                    tools.append(name)
    return tools


# ── Runner ─────────────────────────────────────────────────────────────────────

_RULE_LABELS: dict[Expect, str] = {
    Expect.ASK_CLARIFY_ROUTE_INFO: "R1",
    Expect.DECLINE_PLANNING:       "R2",
    Expect.FREE:                   "R3-8",
    Expect.DECLINE_UNSUPPORTED:    "R4",
    Expect.ANSWER_YES_NO_ONLY:     "R9",
    Expect.ASK_ROUTE_NUMBER:       "R10",
}


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

        rule_tag = _RULE_LABELS.get(case.expect, "??")
        print(f"\n[{rule_tag}] {case.description}")
        print(f"  輸入：{case.user_input}")

        try:
            response = await session.respond(case.user_input)
        except Exception as e:
            response = f"[ERROR] {e}"
            results.append({
                "case": case, "response": response,
                "b_issues": [f"exception: {e}"], "f_issues": [], "tools": [],
            })
            print(f"  ✗ ERROR: {e}")
            continue

        called_tools = get_called_tools(session)

        b_issues = (
            check_behavior(response, case.expect)
            + check_tool_usage(called_tools, case.expect_tool, case.expect_no_tool)
        )
        f_issues = check_format(response)

        b_ok = "✓" if not b_issues else "✗"
        f_ok = "✓" if not f_issues else "✗"

        tool_str = ", ".join(called_tools) if called_tools else "（無）"
        print(f"  工具：{tool_str}")
        print(f"  回覆：{response}")
        for i in b_issues:
            print(f"  [B]⚠ {i}")
        for i in f_issues:
            print(f"  [F]⚠ {i}")
        print(f"  行為 {b_ok}  格式 {f_ok}")

        results.append({
            "case": case, "response": response,
            "b_issues": b_issues, "f_issues": f_issues, "tools": called_tools,
        })

    # ── Summary ──────────────────────────────────────────────────────────────
    total = len(results)
    b_pass = sum(1 for r in results if not r["b_issues"])
    f_pass = sum(1 for r in results if not r["f_issues"])
    both_pass = sum(1 for r in results if not r["b_issues"] and not r["f_issues"])

    print("\n" + "=" * 72)
    print("總結")
    print(f"  行為正確：{b_pass}/{total}")
    print(f"  格式正確：{f_pass}/{total}")
    print(f"  全部通過：{both_pass}/{total}")

    # Group failures by rule
    by_rule: dict[str, list[dict]] = {}
    for r in results:
        if r["b_issues"] or r["f_issues"]:
            tag = _RULE_LABELS.get(r["case"].expect, "??")
            by_rule.setdefault(tag, []).append(r)

    if by_rule:
        print("\n失敗分布：")
        for tag in sorted(by_rule):
            cases = by_rule[tag]
            print(f"  {tag}: {len(cases)} 案例失敗")
            for r in cases:
                short = r["response"][:55].replace("\n", " ")
                print(f"    [{r['case'].description}] → {short}")
                for i in r["b_issues"]:
                    print(f"      [B] {i}")
                for i in r["f_issues"]:
                    print(f"      [F] {i}")


if __name__ == "__main__":
    asyncio.run(main())
