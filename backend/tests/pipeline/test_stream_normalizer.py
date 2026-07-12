"""StreamNormalizer：增量版 normalize_llm_output 的行為契約。

核心不變式：任意切分方式下，feed/flush 輸出串接 == 批次 normalize 結果
（think 塊剝除、簡轉繁、首尾空白剝除）。
"""

from pipeline.normalize import StreamNormalizer, normalize_llm_output


def _drain(text: str, chunk_size: int) -> list[str]:
    normalizer = StreamNormalizer()
    pieces: list[str] = []
    for i in range(0, len(text), chunk_size):
        pieces.extend(normalizer.feed(text[i : i + chunk_size]))
    pieces.extend(normalizer.flush())
    return pieces


def test_matches_batch_normalization_at_any_chunk_size():
    text = "<think>我在想事情。</think>你好！公车还有五分钟到。请稍等一下"
    expected = normalize_llm_output(text)
    for size in (1, 2, 3, 7, len(text)):
        assert "".join(_drain(text, size)) == expected, f"chunk_size={size}"


def test_emits_at_sentence_boundaries_before_stream_ends():
    normalizer = StreamNormalizer()
    assert normalizer.feed("第一句到站了。第二句还") == ["第一句到站了。"]
    assert normalizer.feed("没完") == []
    assert normalizer.flush() == ["第二句還沒完"]


def test_think_tag_split_across_deltas_never_leaks():
    normalizer = StreamNormalizer()
    pieces = normalizer.feed("<thi")
    pieces += normalizer.feed("nk>祕密推理</th")
    pieces += normalizer.feed("ink>答案。")
    pieces += normalizer.flush()
    assert pieces == ["答案。"]


def test_unclosed_think_block_is_dropped_not_spoken():
    normalizer = StreamNormalizer()
    assert normalizer.feed("好的。<think>推理到一半就断线") == ["好的。"]
    assert normalizer.flush() == []


def test_long_enumeration_emits_at_pause_boundaries():
    """站名清單（整句只有「、」）不能憋到句號才輸出，否則首音延遲等於整句生成時間。"""
    normalizer = StreamNormalizer()
    pieces = []
    text = "201路去程經過的站點有：高鐵雲林站、臺大虎尾分院、持法媽祖宮、虎尾惠來、大美瓦斯廠。"
    for i in range(0, len(text), 4):  # simulate token deltas
        pieces.extend(normalizer.feed(text[i : i + 4]))
        if pieces:
            break  # first piece must arrive before the sentence completes
    assert pieces, "清單句應在軟邊界（、：）就先輸出第一段"
    assert len(pieces[0]) < len(text) // 2


def test_long_clause_without_punctuation_still_emits():
    normalizer = StreamNormalizer()
    pieces = normalizer.feed("字" * 250)
    assert pieces, "buffer 超過上限仍未輸出，TTS 會被卡住"
