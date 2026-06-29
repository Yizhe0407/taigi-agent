"""Shared test environment guards."""

import os

# 測試不得對外送 telemetry：.env（由 api/__init__ 的 load_dotenv 載入）指向
# localhost:4318，沒有 collector 時 OTel exporter 的重試迴圈會拖慢 teardown
# 並噴錯誤訊息。先設成空字串佔位 — load_dotenv 預設不覆寫既有變數，
# configure_telemetry 看到 falsy 值就不會註冊 OTLP exporter。
for _var in (
    "OTEL_EXPORTER_OTLP_ENDPOINT",
    "OTEL_EXPORTER_OTLP_TRACES_ENDPOINT",
    "OTEL_EXPORTER_OTLP_METRICS_ENDPOINT",
):
    os.environ[_var] = ""
