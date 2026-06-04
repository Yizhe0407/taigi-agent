# 後端深度體檢報告

> 生成日期：2026-06-04。完成項目請勾選。

---

## 架構問題

- [ ] **P1 — `agent/session.py` 違反 CLAUDE.md 邊界** (`session.py:16-21, 234-235, 249`)
  - 引入 `opencc`、定義 `_THINK_RE`，在 session 出口跑 `_S2TWP.convert()` + strip `<think>`
  - CLAUDE.md 明文：session 只做 messages/LLM/dispatch/context recovery；LLM output post-processing 屬 pipeline
  - 修：搬 normalize 到 `pipeline/llm_output.py`，session 改吐 raw str

- [x] **P1 — 文字 normalize 散三處**
  - `router._to_halfwidth` (`router.py:83-88`) fullwidth → halfwidth
  - `departures._normalize_route_key` + `_FULLWIDTH_RE` (`departures.py:33, 36-39`) 同 transform 再實作一次
  - `session._S2TWP` (`session.py:21`) s2twp
  - 修：集中 `pipeline/normalize.py`，entry 跑一次

- [x] **P2 — `_TIMETABLE_RE` 污染 router** (`router.py:71-76`)
  - bus-specific 措辭 hard-code 進 generic router，違反「router 應 generic」精神
  - 修：交回 LLM tools/prompt 處理，或抽 `tools/intent_rules.py`

- [ ] **P2 — `_kiosk_place` 跨層 private import** (`api/route_plans.py:107`)
  - `from services.route_plans import _kiosk_place` — 從外部模組 import private `_*` symbol
  - 修：export 成 public 或 inline

- [ ] **P2 — `_prepare_context` / `_recover_context` 重複** (`session.py:103-118`)
  - 兩 method 只差 budget，代碼幾乎相同
  - 修：抽 `_compact_and_trim(budget)`

---

## 過長代碼

- [x] **`services/departures.py` 1039 行——主犯，需拆分**
  - [x] 拆 `services/departures/classification.py` (112 行)：`_classify_stop`、`DepartureSection`、`DepartureDecision`、`_scheduled_minutes_from_now`、`StopClassification` dataclass
  - [x] 拆 `services/departures/snapshot.py` (270 行)：`build_departure_snapshot`、`build_route_detail`、dataclasses、exceptions
  - [x] 拆 `services/departures/renderers.py` (339 行)：全部 `render_*` functions
  - [x] 拆 `services/departures/normalize.py` (164 行)：`_strip_paren`、`_name_matches`、`_normalize_route_key`、`_lookup_route`、`_stop_similarity`、`_fuzzy_candidates`、`_downstream_names`、`_stops_by_direction_with_seq`、`_direction_label_from_info`、`_is_terminal_direction`、`_mins_zh`、`_fmt_time_12h`、`_as_int`
  - [x] 拆 `services/departures/provider.py` (40 行)：`get_provider`、`set_provider`、`provider_override`

- [x] **`_classify_stop` 回 8-tuple anti-pattern** (`departures.py:230-348`)
  - 19 處 `_, _, status_text, _, _, _, _, _ = _classify_stop(stop, now)` 散布全檔，改動即斷
  - 修：return `@dataclass(frozen=True) StopClassification(section, decision, status_text, decision_text, minutes, scheduled_time, sort_priority, sort_minutes)`

- [ ] **`render_arrivals_to_destination` 100 行** (`departures.py:918-1017`)
  - nested `_check` async + fuzzy fallback + recursive call 三種關注點混在一起
  - 修：拆 `_classify_arrival_at_stop` + `_remap_destination_via_fuzzy`

- [ ] **`build_departure_snapshot` 88 行** (`departures.py:428-515`)
  - 修：拆三步 — iterate eta rows → terminal filter → dedupe + classify

---

## 冗餘 / 死碼

- [x] **DEAD：`find_routes_to_destination`** (`tools/kiosk_bus.py:30-32`)
  - 沒進 `TOOL_HANDLERS`（`agent/tools.py:156-165`）
  - prod 路徑零依賴，完全被 `get_arrivals_to_destination` 取代（後者多帶到站時間）
  - 修：刪除此函式

- [x] **DEAD：`render_routes_to_destination`** (`departures.py:854-915`，62 行)
  - 唯一 caller 是上面的 dead function
  - 修：刪除此函式 (-65 行合計)

- [x] **冗 noop dual singleton** (`telemetry.py:39-41, 100-112`)
  - `_telemetry` + `_noop` 兩個 singleton；OTel SDK 預設 NoOp providers 已 noop 化
  - `_has_otlp_endpoint` gate (`telemetry.py:50-51, 65`) 多此一舉
  - 修：刪 noop 分支 + endpoint gate，省 ~30 行

- [x] **冗 `trace_tool_routing` empty span** (`telemetry.py:206-215`)
  - `with start_span(): pass` 只放 attribute，沒有 traced operation
  - 修：改 `add_event` 在 parent span，省 span overhead

- [ ] **重複 try/except 樣板** (`departures.py:599-610, 711-722, 829-839, 868-872, 939-943`)
  - 至少 6 處 `try: provider.X() except Exception: return "查詢失敗，請稍後再試。"` 完全相同
  - 修：抽 `async def _safe_provider_call(coro, default_msg) -> str` helper

- [ ] **冗 `tool_error` 命名誤導** (`tool_dispatch.py:46-47, 93`)
  - 同函式既傳成功又傳錯誤，命名 `tool_error` misleading
  - 修：改名 `tool_result_msg`

- [ ] **`_TOOL_CALL_FAILED_MARKERS` 字串匹配脆弱** (`llm_client.py:31-34, 50-52`)
  - Groq 特定 marker 字串；vLLM 改報不同格式即靜默 fall-through
  - 修：vendor switch 時記得更新此處

---

## 效能問題

- [x] **N+1：`render_arrivals_to_destination` per-route HTTP fan-out** (`departures.py:947-991`)
  - kiosk 30 條路線各打一次 `fetch_route_estimate`，每 user query 觸發 30 req
  - `load_route_info` 有 10min TTL cache（`yunlin_ebus.py:19`），但 `fetch_route_estimate` 零 cache
  - 修：加 10s TTL per-route-id estimate cache；同 query 連發剩 1 round-trip

- [x] **`AsyncOpenAI` per-request rebuild** (`api/chat.py:75-78` → `config.py:118-130`)
  - 每 chat message 新建 `AsyncOpenAI` → 新 `httpx.AsyncClient` → 新 TCP pool
  - 修：`lru_cache make_client(settings_hash)` 或 process singleton

- [x] **`Settings.from_env()` per request** (`api/tts.py:38`, `api/chat.py:76, 91`)
  - 每 request 重 parse 所有 env vars + cors split
  - 修：`@lru_cache` 或 module-level singleton

- [x] **`compact_long_tool_results` 每 turn deepcopy 全 history** (`context.py:89`)
  - 每 LLM round 走 `_prepare_context` → `deepcopy(messages)`；長 tool result 很貴
  - 修：shallow copy list，只 deep copy 需 rewrite 的 dict

- [x] **`estimate_tokens` 每 turn 全 encode** (`context.py:48-64`)
  - 隨對話長度 O(N²) tiktoken encode；kiosk 短對話 OK，admin/debug 長 session 會慢
  - 修：cache per-message token count（key by `id(msg)` 或 content hash）

- [ ] **Sequential `load_route_info` → `fetch_route_estimate`** (`departures.py:597-608, 706-720, 825-837`)
  - `render_arrivals`、`render_route_stops`、`render_stop_on_route` 序列呼叫
  - `build_departure_snapshot` 已用 `asyncio.gather`（`departures.py:439-442`），同 pattern 未統一
  - 修：統一改 `asyncio.gather`

- [ ] **Session store 全 lock 串行化** (`api/session_store.py:40`)
  - 單 `threading.Lock` 包所有讀寫；WAL 已支援 concurrent read，lock 強行序列化
  - 修：分讀寫鎖，或每 thread own connection（kiosk 單機暫可接受）

- [ ] **`_stations_cache` 非 thread-safe** (`services/moovo.py:69, 222`)
  - module-global 無鎖；cold start 並發 first req 會打 TDX 兩次
  - 修：`asyncio.Lock` 保護 fetch path

- [ ] **`yunlin_boundary._point_in_ring` 全掃** (`yunlin_boundary.py:83-100`)
  - 千 vertex multipolygon 每點 O(N) 掃
  - 修：每 polygon 加 bbox pre-filter（低頻 admin 路徑，優先度低）

---

## CLI 風險

- [x] **`YunlinEbusProvider._http` 持久 client + 多 event loop** (`yunlin_ebus.py:46`, `agent/loop.py:30`)
  - ~~CLI mode 每 turn `asyncio.run(...)` 新 loop；`httpx.AsyncClient` 綁第一個 loop，第二 turn 會 hang~~
  - 已刪除 `agent/loop.py` + `main.py`（前端已取代 CLI）；`e2e_test.py` / `probe_llm.py` 保留為 dev script

---

## 優先 Quick Wins（由高到低）

- [x] **#1 刪 dead code** (`tools/kiosk_bus.py:30-32`, `departures.py:854-915`)：`find_routes_to_destination` + `render_routes_to_destination` + inner `_check`，-65 行，無 prod caller
- [x] **#2 `_classify_stop` 改 dataclass return**：一次 refactor 解 19 處 8-tuple unpack，全在 `departures.py`
- [x] **#3 Cache AsyncOpenAI + Settings**：per-request rebuild → process singleton（`api/chat.py`、`api/tts.py`、`config.py`）
- [x] **#4 `fetch_route_estimate` 10s TTL cache**：解 destination query 30× HTTP fan-out（`yunlin_ebus.py`）
- [x] **#5 拆 `services/departures.py`**：1039 → 5 個 40-340 行檔案 + `__init__.py` re-export
- [x] **#6 `telemetry.py` 砍 noop + endpoint gate**：-30 行（`telemetry.py:39-41, 50-51, 100-112`）
- [x] **#7 `_TIMETABLE_RE` 搬出 router**：router 回 generic（`router.py:71-76`）
- [x] **#8 集中 normalize**：`_to_halfwidth`、`_FULLWIDTH_RE`、`_S2TWP`、`_THINK_RE` 合入 `pipeline/normalize.py`
- [x] **#9 `compact_long_tool_results` 改 shallow copy**：每 turn 省 deepcopy 全 history（`context.py:89`）
- [x] **#10 tiktoken token count cache**：解 long-session O(N²)（`context.py:48`）
