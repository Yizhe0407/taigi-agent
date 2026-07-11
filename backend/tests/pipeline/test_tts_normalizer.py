from pipeline.tts_normalizer import normalize_for_tts

# ── bracket stripping ────────────────────────────────────────────────────────


def test_strip_corner_brackets():
    assert normalize_for_tts("在雲林科技大學找不到路線") == "在雲林科技大學找不到路線"
    assert normalize_for_tts("「雲林科技大學」") == "雲林科技大學"


def test_strip_various_brackets():
    assert normalize_for_tts("【公告】") == "公告"
    assert normalize_for_tts("《路線》") == "路線"


def test_strip_paren_keep_content():
    assert normalize_for_tts("持法媽祖宮(頂溪)") == "持法媽祖宮頂溪"
    assert normalize_for_tts("稅務局（雲林縣政府）") == "稅務局雲林縣政府"


def test_no_brackets_unchanged():
    assert normalize_for_tts("雲林科技大學") == "雲林科技大學"


# ── time conversion ──────────────────────────────────────────────────────────


def test_time_morning():
    assert normalize_for_tts("9:05") == "九點零五分"


def test_time_zero_minutes():
    assert normalize_for_tts("10:00") == "十點"


def test_time_ten_minutes():
    assert normalize_for_tts("10:10") == "十點十分"


def test_time_afternoon():
    assert normalize_for_tts("14:30") == "下午兩點三十分"


def test_time_noon():
    assert normalize_for_tts("12:00") == "中午十二點"


def test_time_midnight():
    assert normalize_for_tts("0:00") == "凌晨十二點"


def test_time_midnight_with_minutes():
    assert normalize_for_tts("00:05") == "凌晨十二點零五分"


def test_time_embedded():
    assert normalize_for_tts("預定16:35到站") == "預定下午四點三十五分到站"


# ── route code conversion ────────────────────────────────────────────────────


def test_route_numeric():
    assert normalize_for_tts("201路") == "二零一路"


def test_route_zero():
    assert normalize_for_tts("307路") == "三零七路"


def test_route_four_digit():
    assert normalize_for_tts("7126路") == "七一二六路"


def test_route_suffix_letter():
    assert normalize_for_tts("7000b路") == "七零零零逼路"


def test_route_suffix_letter_upper():
    assert normalize_for_tts("7000B路") == "七零零零逼路"


def test_route_prefix_letter():
    assert normalize_for_tts("Y01路") == "歪零一路"


def test_route_in_sentence():
    assert normalize_for_tts("201路往高鐵") == "二零一路往高鐵"


# ── minute/duration conversion ───────────────────────────────────────────────


def test_minutes_single():
    assert normalize_for_tts("約5分鐘後") == "約五分鐘後"


def test_minutes_ten():
    assert normalize_for_tts("約12分鐘後") == "約十二分鐘後"


def test_minutes_thirty():
    assert normalize_for_tts("約30分後") == "約三十分後"


def test_minutes_no_hou():
    assert normalize_for_tts("5分鐘") == "五分鐘"


# ── ordinal/count classifiers ────────────────────────────────────────────────


def test_ordinal_ban():
    assert normalize_for_tts("第1班") == "第一班"


def test_ordinal_stop():
    assert normalize_for_tts("3站後") == "三站後"


def test_ordinal_two_digit():
    assert normalize_for_tts("第12班") == "第十二班"


# ── remaining digits fallback ────────────────────────────────────────────────


def test_remaining_digit_route_no_lu():
    # 沒有 路 後綴的路線代號 fallback 到 digit-by-digit
    assert normalize_for_tts("201") == "二零一"


def test_standalone_number():
    assert normalize_for_tts("查不到 7001 路線") == "查不到 七零零一 路線"


# ── letter conversion ────────────────────────────────────────────────────────


def test_letter_standalone():
    assert normalize_for_tts("Y") == "歪"


def test_letter_b():
    assert normalize_for_tts("B") == "逼"


def test_letter_in_route_no_lu():
    # Y01 沒有 路，先 digit fallback 01→零一，再 letter Y→歪
    result = normalize_for_tts("Y01")
    assert result == "歪零一"


# ── combined / edge cases ────────────────────────────────────────────────────


def test_pure_chinese_unchanged():
    assert normalize_for_tts("查不到該資料") == "查不到該資料"


def test_already_chinese_numbers():
    assert normalize_for_tts("二零一路預定十點到") == "二零一路預定十點到"


def test_empty():
    assert normalize_for_tts("") == ""


def test_complex_sentence():
    result = normalize_for_tts("201路往高鐵雲林站，預定16:35到，約5分鐘後。")
    assert "二零一路" in result
    assert "下午四點三十五分" in result
    assert "五分鐘後" in result
    assert "16" not in result
    assert "35" not in result
    assert "201" not in result
    assert "5" not in result
