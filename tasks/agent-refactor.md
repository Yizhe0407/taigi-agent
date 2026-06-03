# Agent 治本重構計劃

## 背景

目前 agent 用 prompt 當 state machine：10 條決策規則塞進 system prompt，靠 4B model 自己分類、追上下文、選工具。實證問題：
- Multi-turn 經常崩（「那還有其他路線嗎」→ 列本站所有路線當作直達目的地）
- Rule 邊界模糊（「幾點有車」走 Rule 3 而非 Rule 7）
- Forbidden-tool 重試 loop 是症狀補丁，不是修法
- 每個 bug 都靠加 prompt 子句修，prompt 已 90 行還在增長
- Probe regex-based，噪訊大，分數浮動 ±2

根因：classification、state tracking、output phrasing 三件事全交給 LLM 同時做。4B model 撐不住。

## 設計原則

1. **Classification 移到 Python**：deterministic intent router，LLM 只負責 phrasing
2. **顯式 ConvState**：對話狀態用 typed dataclass，不靠 LLM 從 messages 推
3. **單一真理**：tool description 描述工具做什麼，何時呼叫由 router 決定（不在 prompt 也不在 schema）
4. **窄工具集**：每個 intent 只暴露相關 tool 給 LLM，不可能呼叫錯
5. **Workaround 砍掉**：能用結構性方法解的，不留 retry loop

## 分階段

---

## P0 - 正確性 + 架構轉軸

### Cut 1: Geo-aware 修 `find_routes_to_destination` 與 `render_stop_on_route`

**目標**：修正 silent correctness bug — 目的地必須在 kiosk 站之後的 sequence 才算「能到」。

**動的檔案**：
- `backend/services/departures.py:737-770`（`render_routes_to_destination._check`）
- `backend/services/departures.py:676-716`（`render_stop_on_route`）
- 新增 `backend/tests/services/test_departures_geo.py`

**設計**：

```python
async def _check(route_name, route_id):
    data = await provider.fetch_route_estimate(route_id)
    by_direction = {}
    for stop in data:
        gb = stop.get("GoBack", 1)
        seq = _as_int(stop.get("SeqNo")) or 0
        name = _strip_paren(stop.get("StopName", ""))
        by_direction.setdefault(gb, []).append((seq, name))

    hits = []
    for gb, stops in by_direction.items():
        stops.sort(key=lambda x: x[0])
        kiosk_seq = next(
            (s for s, n in stops if kiosk_stop in n or n in kiosk_stop),
            None,
        )
        if kiosk_seq is None:
            continue
        downstream = [n for s, n in stops if s > kiosk_seq]
        if any(destination in n or n in destination for n in downstream):
            label = _direction_label_from_info(route_info, route_name, gb)
            hits.append((route_name, label))
    return hits
```

**驗收**：
- 既有 probe R3 case 全 pass（「我想去虎尾科大」「去斗六」等）
- 新單元測試：模擬「kiosk 在中間、destination 在 kiosk 西邊」→ 該方向不該回傳
- 新單元測試：模擬「kiosk 是終點」→ 沒有 downstream，返回空

**風險**：低。改動局部，邏輯純函數，可單元測試。

**估**：departures.py ~40 行修改 + 測試 ~80 行。

---

### Cut 2: Python intent router + ConvState

**目標**：把 10 條 prompt rule 的 classification 邏輯搬到 Python；LLM 只負責 phrasing 或在 ambiguous case 兜底。

#### 2.1 新模組 `backend/agent/router.py`

```python
class Intent(Enum):
    ROUTE_ONLY            # Rule 1: 純路線號碼
    REMOTE_DESTINATION    # Rule 2: 跨縣市
    TIMETABLE_UNSUPPORTED # Rule 3: 完整時刻表
    FIND_ROUTES_TO_DEST   # Rule 4: 怎麼去某地
    OTHER_ROUTES_FOLLOWUP # Rule 4 follow-up: 還有其他路線嗎
    ROUTES_AT_STOP        # Rule 5: 本站有哪些路線
    STOP_STATUS           # Rule 6: 還有車嗎
    ARRIVAL_TIME          # Rule 7: X 路幾點到
    ROUTE_STOPS_CLARIFY   # Rule 8: 站牌（需澄清）
    CHECK_STOP_ON_ROUTE   # Rule 9: X 有沒有停 Y
    ASK_ROUTE_NUMBER      # Rule 10: 涉及路線但無號碼
    OFF_TOPIC             # 非公車
    UNKNOWN               # fallback → LLM

@dataclass
class ConvState:
    last_route: str | None = None
    last_destination: str | None = None
    last_intent: Intent | None = None
    pending_stops_clarify_route: str | None = None  # Rule 8 等澄清

@dataclass
class Decision:
    intent: Intent
    canned_response: str | None = None     # 直接回，無 LLM
    tool_call: tuple[str, dict] | None = None  # 直呼工具 + LLM 包裝
    fallback_to_llm: bool = False           # 交回 legacy LLM loop
    update_state: Callable[[ConvState], None] | None = None

class IntentRouter:
    def classify(self, user_input: str, state: ConvState) -> Decision: ...
```

#### 2.2 改 `AgentSession.respond()` 接 router

```python
async def respond(self, user_input: str) -> str:
    decision = self.router.classify(user_input, self.conv_state)

    if decision.canned_response:
        self._append_user(user_input)
        self._append_assistant(decision.canned_response)
        self._apply_state_update(decision)
        return decision.canned_response

    if decision.tool_call:
        tool_name, args = decision.tool_call
        result = await self.tool_handlers[tool_name](**args)
        reply = await self._phrase_tool_result(user_input, tool_name, result)
        self._apply_state_update(decision)
        return reply

    # Fallback: legacy LLM loop（保留，逐步搬完後刪）
    return await self._legacy_llm_loop(user_input)
```

#### 2.3 分批搬 intents（router 內部，外圍 API 不變）

順序由「最危險／最高頻」開始：

1. **ROUTE_ONLY**（Rule 1）— canned，無 LLM 呼叫
2. **REMOTE_DESTINATION**（Rule 2）— canned
3. **TIMETABLE_UNSUPPORTED**（Rule 3）— canned
4. **FIND_ROUTES_TO_DEST**（Rule 4）— tool + minimal phrasing
5. **OTHER_ROUTES_FOLLOWUP**（Rule 4 follow-up）— state-driven，重呼 same destination
6. **ARRIVAL_TIME**（Rule 7）— tool + minimal phrasing
7. **STOP_STATUS**（Rule 6）— tool + minimal phrasing
8. **ROUTES_AT_STOP**（Rule 5）— tool + minimal phrasing
9. **CHECK_STOP_ON_ROUTE**（Rule 9）— tool + canned phrasing
10. **ROUTE_STOPS_CLARIFY**（Rule 8）— canned 問句 + 設 pending state
11. **ASK_ROUTE_NUMBER**（Rule 10）— canned 或 state-driven

每搬一個跑 probe 看分數變化，紅了立即 revert 該 intent 並調 router 邏輯。

**驗收**：
- 每搬一個 intent，probe 該 intent 相關 case 100% pass
- Multi-turn case：「我想去虎尾科大 → 搭7120 → 幾點有車 → 那還有其他路線嗎」三輪都正確
- ConvState 單元測試：從 mock messages 算出正確 state

**風險**：中。架構改動大，但有 fallback_to_llm 兜底，分批搬可隨時 revert。

**估**：router.py ~250 行 + session.py 改 ~60 行 + 測試 ~150 行。

---

## P1 - 砍 workaround

依賴 P0 完成。

### 1.1 刪 forbidden-tool retry loop
- `backend/agent/session.py:51-63, 238-247` 整段刪
- 改成：當 router 決定某 intent 不該呼叫工具時，呼 LLM 時傳 `tools=None`
- ~25 行刪除

### 1.2 簡化 `prefetch_route_arrival_context`
- `backend/tools/kiosk_bus.py:108-126` 改成單純的「extract route from text」utility（給 router 用）
- ROUTE_ONLY 的注 marker 機制刪掉（router 直接 deterministic 處理）
- ~20 行縮減

### 1.3 精簡 system prompt
- `backend/agent/prompt.py` 從 90 行 → ~20 行
- 只留：角色、輸出格式、禁止項
- Rule 1-10 全刪（已搬到 router）
- 留下：當 router 把工具結果交給 LLM phrase 時的格式約束

### 1.4 精簡 tool schemas
- `backend/agent/tools.py:14-138` 每個 schema 的 `MUST 呼叫 / NEVER` 子句刪掉
- description 只描述「工具做什麼、回什麼」
- ~50 行縮減

**驗收**：probe 分數不退，總 LOC 減少 ~150 行。

**風險**：低（前提是 P0 router 已 cover 所有 case）。

---

## P2 - Probe 改 trajectory + LLM judge

**目標**：probe 從 regex 變客觀指標，迭代有 signal。

### 2.1 Trajectory assertion

不檢查回答文字，檢查：
- 呼叫了哪個 tool（name + args）
- ConvState 結束狀態
- response 是否包含關鍵事實（如：包含路線號碼「7120」或「沒有」）

```python
@dataclass
class TrajectoryExpect:
    tool_calls: list[tuple[str, dict]] | None  # 期望順序
    final_state: ConvState | None
    must_contain: list[str] | None              # 簡單事實 check
    must_not_contain: list[str] | None
```

### 2.2 LLM judge（選擇性）

只對 phrasing/tone 用 LLM judge：
- 「這個回答自然嗎」
- 「有沒有道歉語」
- 用 GPT-4o-mini 或同等便宜模型，跑 batch

**驗收**：probe 跑 3 次分數差 ≤ 1。

**估**：probe_llm.py 重寫 ~200 行。

---

## P3 - 精簡

### 3.1 刪 summary compact
- `backend/agent/session.py:124-148` `_summary_compact` 整段刪
- Kiosk session 改硬上限「保留最後 N 輪」（N=5）
- `backend/agent/context.py` 縮減

### 3.2 `render_arrivals` 寬鬆 route lookup
- `backend/services/departures.py:543-563`
- Case-insensitive、忽略「路」字、trim 全形

### 3.3 雜項
- 砍未用的 dataclass / Enum
- 統一錯誤訊息格式

**估**：~100 行縮減。

---

## 全程觀測

每個 PR 必跑：
- `uv run pytest`
- `uv run python scripts/probe_llm.py`（P2 完成後改 trajectory probe）
- `uv run ruff check .`

實際操作的 multi-turn fixtures（手工跑，加進 probe）：
1. `我想去虎尾科大 → 搭7120就可以 → 幾點有車 → 那還有其他路線嗎`
2. `201 → 想查到站時間 → 幾分鐘後到`
3. `這站有哪些路線 → 201呢 → 有停斗六嗎`

## 完成定義（DoD）

| 階段 | 狀態 | DoD |
|------|------|-----|
| P0 Cut 1 | ✅ Done | geo-aware 單元測試全綠；probe 不退 |
| P0 Cut 2 | ✅ Done | 11 個 intent 全搬完；fallback_to_llm 從不觸發；probe ≥ 44/45 |
| P1 | ✅ Done | prompt ≤ 25 行；retry loop / prefetch marker 全刪；probe 不退 |
| P2 | ✅ Done | probe 改 trajectory assertions（`conv_state.last_intent`）；router regex bug 修正 |
| P3 | ✅ Done | summary compact 刪除；MAX_EXCHANGES=5 硬上限；`Intent` 清理；render_arrivals 寬鬆路線查找 |

## 不做

- 換 model：目前是部署層決定，不在這次重構範圍
- 新增功能（時刻表、跨站規劃等）：先把現有功能修對
- 改 frontend：純後端 agent 重構
- 改 provider / GTFS 同步：不在 agent 層
