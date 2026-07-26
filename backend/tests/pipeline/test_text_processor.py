"""Tests for pipeline.text_processor's lazy-singleton converter init."""

import threading
import time
from unittest.mock import patch

import pipeline.text_processor as text_processor


def test_concurrent_cold_start_constructs_hanlo_converter_exactly_once():
    """Regression test for finding 3: process() runs on a 4-worker thread pool,
    so several cold-start calls can race into _get_hanlo() at once. Without a
    lock, two threads could both observe `_hanlo_converter is None` and each
    construct (and one discard) a duplicate — wasted disk/CPU work loading the
    lexicon. The double-checked lock must collapse concurrent cold starts into
    a single construction."""
    build_count = 0
    build_lock = threading.Lock()

    class _SlowConverter:
        def __init__(self):
            nonlocal build_count
            with build_lock:
                build_count += 1
            # Widen the race window so concurrent callers actually overlap
            # inside the unlocked region instead of serializing by luck.
            time.sleep(0.05)

    with (
        patch.object(text_processor, "_hanlo_converter", None),
        patch("pipeline.text_processor.TaigiConverter", _SlowConverter),
    ):
        threads = [threading.Thread(target=text_processor._get_hanlo) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

    assert build_count == 1


def test_concurrent_cold_start_constructs_taibun_converter_exactly_once():
    build_count = 0
    build_lock = threading.Lock()

    class _SlowConverter:
        def __init__(self, **kwargs):
            nonlocal build_count
            with build_lock:
                build_count += 1
            time.sleep(0.05)

    with (
        patch.object(text_processor, "_taibun_converter", None),
        patch("pipeline.text_processor.TaibunConverter", _SlowConverter),
    ):
        threads = [threading.Thread(target=text_processor._get_taibun) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

    assert build_count == 1
