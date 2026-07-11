"""Kiosk-specific intent classification rules.

Patterns and canned responses that belong to the kiosk bus domain, kept
here so agent/router.py stays domain-agnostic.
"""

import random
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

TIMETABLE_CANNED_RESPONSE = "時刻表查不了，只能查下一班到站時間喔。"


# 社交（非公車）回應：語意由 LLM 分類，用詞由這裡的池子隨機挑。
# 讓弱模型只做它做得穩的「分類」，別靠它自己變化用詞（4B 傾向照抄範例）。
# 每句都遵守 prompt 鐵律：句號結尾、無問句、無 emoji、台語友善阿姨口吻。
DIRECT_RESPONSE_POOL: dict[str, tuple[str, ...]] = {
    "off_topic": (
        "我只知道公車啦，其他我不清楚呢。",
        "這我就不懂囉，你問公車我才答得出來。",
        "公車以外的我不會啦，歹勢。",
    ),
    "unclear": (
        "我沒聽清楚，麻煩再說一次你要去哪裡。",
        "歹勢沒聽清楚，你要去的站再講一次。",
        "剛剛沒聽清楚，再說一遍你要去的地方。",
    ),
    "complaint": (
        "哎呀歹勢，可能路上塞車系統不準，我也沒辦法呢。",
        "真歹勢，路況系統有時候不準，我也沒法度。",
        "歹勢啦，可能塞車系統抓不準，我也沒辦法。",
    ),
    "thanks": (
        "不會啦，路上小心喔。",
        "免客氣，路上小心。",
        "好啦，慢走喔。",
    ),
}


def pick_direct_response(intent: str | None, fallback: str) -> str:
    """社交 intent → 隨機挑一句阿姨口吻的固定語意回應（用詞由 code 保證變化）。
    其他 intent（例如公車查詢完成後的最終答案）→ 原樣回傳模型自己的訊息。
    """
    pool = DIRECT_RESPONSE_POOL.get(intent or "")
    return random.choice(pool) if pool else fallback


if __name__ == "__main__":
    # 社交 intent 一定落在池子裡；未知/None 一定回傳 fallback（最終答案不被覆寫）。
    for key, pool in DIRECT_RESPONSE_POOL.items():
        assert pick_direct_response(key, "FALLBACK") in pool
    assert pick_direct_response(None, "FALLBACK") == "FALLBACK"
    assert pick_direct_response("final", "五分鐘後到") == "五分鐘後到"
    print("ok")
