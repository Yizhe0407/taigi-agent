"""Runtime kiosk configuration — persists across restarts via
.agent_state/kiosk_config.json.

Default: 雲林科技大學, 回程.
Set by the admin UI; no env vars required.

Note: this module keeps an in-memory singleton so every request in the same
worker process sees the update immediately. Multiple uvicorn workers would
each have their own copy; writes land on disk so they survive restarts, but
other workers won't pick up a change until they're restarted. For a single-
worker demo deployment this is a non-issue.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path

_log = logging.getLogger(__name__)

_STATE_DIR = Path(__file__).resolve().parents[1] / ".agent_state"
_STATE_PATH = _STATE_DIR / "kiosk_config.json"
_lock = threading.Lock()

_DEFAULT_STOP_NAME = "雲林科技大學"
_DEFAULT_DIRECTION: str | None = "回程"


@dataclass(frozen=True)
class KioskConfig:
    stop_name: str = _DEFAULT_STOP_NAME
    direction: str | None = _DEFAULT_DIRECTION  # "去程" | "回程" | None (show both)
    lat: float | None = None
    lon: float | None = None

    @property
    def go_back(self) -> int | None:
        """Translate direction label to TDX Direction int (0=去程, 1=回程, None=both)."""
        if self.direction == "去程":
            return 0
        if self.direction == "回程":
            return 1
        return None


_current: KioskConfig | None = None
_current_mtime_ns: int | None = None
_last_stat_monotonic: float | None = None
# get_kiosk_config() is on the per-message hot path (build_system_prompt). A bare
# stat() every call is cheap in isolation but adds up under load, and cross-worker
# config changes don't need sub-second visibility — throttle re-stats to once per
# this many seconds; still stats on the very first call (see `_last_stat_monotonic
# is None` below) so a fresh process picks up disk state immediately.
_STAT_THROTTLE_SECONDS = 1.0


def _load() -> KioskConfig:
    """Load from state file; fall back to defaults on any error."""
    if not _STATE_PATH.exists():
        return KioskConfig()
    try:
        data = json.loads(_STATE_PATH.read_text(encoding="utf-8"))
        return KioskConfig(
            stop_name=data.get("stop_name", _DEFAULT_STOP_NAME),
            direction=data.get("direction", _DEFAULT_DIRECTION),
            lat=data.get("lat"),
            lon=data.get("lon"),
        )
    except Exception:
        _log.warning(
            "Failed to load kiosk config from %s; using defaults",
            _STATE_PATH,
            exc_info=True,
        )
        return KioskConfig()


def get_kiosk_config() -> KioskConfig:
    """Return current config and observe writes made by another worker.

    Throttled: this is called on every agent message (build_system_prompt), so a
    bare `Path.stat()` per call would land on the hot path. Skipping the stat for
    up to `_STAT_THROTTLE_SECONDS` after the last check keeps other-worker updates
    visible within about a second, cheaply, instead of once per request.
    """
    global _current, _current_mtime_ns, _last_stat_monotonic
    with _lock:
        now = time.monotonic()
        if _current is not None and _last_stat_monotonic is not None and now - _last_stat_monotonic < _STAT_THROTTLE_SECONDS:
            return _current
        try:
            mtime_ns = _STATE_PATH.stat().st_mtime_ns
        except FileNotFoundError:
            mtime_ns = None
        _last_stat_monotonic = now
        if _current is None or mtime_ns != _current_mtime_ns:
            _current = _load()
            _current_mtime_ns = mtime_ns
        return _current


def set_kiosk_config(cfg: KioskConfig) -> None:
    """Atomically persist config before publishing it to in-process readers."""
    global _current, _current_mtime_ns
    with _lock:
        _STATE_DIR.mkdir(parents=True, exist_ok=True)
        tmp_path = _STATE_PATH.with_name(f"{_STATE_PATH.name}.{uuid.uuid4().hex}.tmp")
        try:
            tmp_path.write_text(
                json.dumps(asdict(cfg), ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            os.replace(tmp_path, _STATE_PATH)
            _current = cfg
            _current_mtime_ns = _STATE_PATH.stat().st_mtime_ns
        finally:
            tmp_path.unlink(missing_ok=True)


def kiosk_stop_name() -> str:
    """Current kiosk stop name — the default scope for kiosk-here queries."""
    return get_kiosk_config().stop_name


def kiosk_go_back_filter() -> int | None:
    """Current kiosk direction filter as TDX Direction int (0=去程, 1=回程, None=both)."""
    return get_kiosk_config().go_back
