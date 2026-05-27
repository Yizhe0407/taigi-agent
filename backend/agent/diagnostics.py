"""Small CLI diagnostics helper for the agent runtime.

The project still uses stdout for the local kiosk/CLI workflow. Keeping the
prefix formatting here prevents ad hoc diagnostic strings from drifting while
avoiding a full logging setup for the current single-process runtime.
"""


def log_diagnostic(scope: str, message: str) -> None:
    print(f"[{scope}] {message}")
