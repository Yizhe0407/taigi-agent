"""Persistence and cross-worker visibility tests for kiosk configuration."""

import json

import pytest

import services.kiosk_config as kiosk_config
from services.kiosk_config import KioskConfig


@pytest.fixture()
def isolated_state(tmp_path, monkeypatch):
    state_path = tmp_path / "kiosk_config.json"
    monkeypatch.setattr(kiosk_config, "_STATE_DIR", tmp_path)
    monkeypatch.setattr(kiosk_config, "_STATE_PATH", state_path)
    monkeypatch.setattr(kiosk_config, "_current", None)
    monkeypatch.setattr(kiosk_config, "_current_mtime_ns", None)
    monkeypatch.setattr(kiosk_config, "_last_stat_monotonic", None)
    return state_path


def test_set_persists_before_publishing(isolated_state):
    cfg = KioskConfig("斗六火車站", "去程", 23.711, 120.542)

    kiosk_config.set_kiosk_config(cfg)

    assert kiosk_config.get_kiosk_config() == cfg
    assert json.loads(isolated_state.read_text(encoding="utf-8")) == {
        "stop_name": "斗六火車站",
        "direction": "去程",
        "lat": 23.711,
        "lon": 120.542,
    }


def test_failed_replace_does_not_publish_partial_config(isolated_state, monkeypatch):
    original = KioskConfig("雲林科技大學", "回程", 23.695, 120.534)
    kiosk_config.set_kiosk_config(original)

    def fail_replace(src, dst):
        raise OSError("disk failure")

    monkeypatch.setattr(kiosk_config.os, "replace", fail_replace)

    with pytest.raises(OSError, match="disk failure"):
        kiosk_config.set_kiosk_config(KioskConfig("斗六火車站", "去程", 23.711, 120.542))

    assert kiosk_config._current == original
    assert json.loads(isolated_state.read_text(encoding="utf-8"))["stop_name"] == "雲林科技大學"
    assert list(isolated_state.parent.glob("*.tmp")) == []


def test_reader_reloads_config_written_by_another_worker(isolated_state):
    local = KioskConfig("雲林科技大學", "回程", 23.695, 120.534)
    kiosk_config.set_kiosk_config(local)

    external = {
        "stop_name": "斗六火車站",
        "direction": "去程",
        "lat": 23.711,
        "lon": 120.542,
    }
    isolated_state.write_text(json.dumps(external), encoding="utf-8")
    kiosk_config._current_mtime_ns = -1

    assert kiosk_config.get_kiosk_config() == KioskConfig(**external)


def test_stat_is_throttled_between_calls(isolated_state, monkeypatch):
    """Rapid repeated calls (the per-message hot path) must not re-stat every time."""
    local = KioskConfig("雲林科技大學", "回程", 23.695, 120.534)
    kiosk_config.set_kiosk_config(local)

    stat_calls = 0
    real_stat = kiosk_config.Path.stat

    def counting_stat(self, *args, **kwargs):
        nonlocal stat_calls
        stat_calls += 1
        return real_stat(self, *args, **kwargs)

    monkeypatch.setattr(kiosk_config.Path, "stat", counting_stat)

    for _ in range(5):
        assert kiosk_config.get_kiosk_config() == local

    assert stat_calls == 1


def test_stat_runs_again_after_throttle_window(isolated_state, monkeypatch):
    local = KioskConfig("雲林科技大學", "回程", 23.695, 120.534)
    kiosk_config.set_kiosk_config(local)
    kiosk_config.get_kiosk_config()  # establishes _last_stat_monotonic

    external = {"stop_name": "斗六火車站", "direction": "去程", "lat": 23.711, "lon": 120.542}
    isolated_state.write_text(json.dumps(external), encoding="utf-8")
    # Simulate the throttle window having elapsed without a real sleep.
    kiosk_config._last_stat_monotonic -= kiosk_config._STAT_THROTTLE_SECONDS + 1

    assert kiosk_config.get_kiosk_config() == KioskConfig(**external)
