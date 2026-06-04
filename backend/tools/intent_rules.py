"""Kiosk-specific intent classification rules.

Patterns and canned responses that belong to the kiosk bus domain, kept
here so agent/router.py stays domain-agnostic.
"""

import re

# Timetable / inter-stop travel-time queries the kiosk doesn't support.
# Carefully avoids matching real-time arrival phrasings like
# 「幾點有車」「幾點來」「下一班幾點」which belong to ARRIVAL_TIME.
TIMETABLE_RE = re.compile(
    r"(完整時刻表|全天時刻表|時刻表|班次表"
    r"|幾點幾分發車|發車時刻"
    r"|站間.{0,10}幾分鐘"
    r"|從.{1,20}到.{1,20}(要|大概|大約)?.{0,5}幾分鐘?)"
)

TIMETABLE_CANNED_RESPONSE = "時刻表查不了，要查到站時間嗎？"
