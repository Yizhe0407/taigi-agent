"""Shared test environment guards."""

import os

import pytest

# Rate-limit behavior has dedicated tests; disabling it keeps unrelated API
# tests isolated from process-global token buckets.
os.environ["RATE_LIMIT_ENABLED"] = "false"
# Same for the outbound TDX limiter (real default: 5/min); its own tests build
# limiters explicitly. Reset per test so one test's 429 pause never leaks.
os.environ["TDX_RATE_LIMIT"] = "1000000/s"


@pytest.fixture(autouse=True)
def _fresh_tdx_rate_limiters():
    from providers.tdx_rate_limit import reset_tdx_rate_limiters

    reset_tdx_rate_limiters()
    yield
    reset_tdx_rate_limiters()


# 測試不得對外送 telemetry：.env（由 api/__init__ 的 load_dotenv 載入）指向
# localhost:4318，沒有 collector 時 OTel exporter 的重試迴圈會拖慢 teardown
# 並噴錯誤訊息。先設成空字串佔位 — load_dotenv 預設不覆寫既有變數，
# configure_telemetry 看到 falsy 值就不會註冊 OTLP exporter。
for _var in (
    "OTEL_EXPORTER_OTLP_ENDPOINT",
    "OTEL_EXPORTER_OTLP_TRACES_ENDPOINT",
    "OTEL_EXPORTER_OTLP_METRICS_ENDPOINT",
    "OTEL_EXPORTER_OTLP_LOGS_ENDPOINT",
):
    os.environ[_var] = ""
