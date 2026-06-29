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
import threading
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
    """Return the current runtime kiosk config (lazy-loaded once per process)."""
    global _current
    with _lock:
        if _current is None:
            _current = _load()
        return _current


def set_kiosk_config(cfg: KioskConfig) -> None:
    """Update the runtime config and persist to disk."""
    global _current
    with _lock:
        _current = cfg
    # Disk write outside the lock — readers can't see a partial state because
    # _current is an immutable dataclass reference swapped atomically above.
    _STATE_DIR.mkdir(parents=True, exist_ok=True)
    _STATE_PATH.write_text(
        json.dumps(asdict(cfg), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def kiosk_stop_name() -> str:
    """Current kiosk stop name — the default scope for kiosk-here queries."""
    return get_kiosk_config().stop_name


def kiosk_go_back_filter() -> int | None:
    """Current kiosk direction filter as TDX Direction int (0=去程, 1=回程, None=both)."""
    return get_kiosk_config().go_back
