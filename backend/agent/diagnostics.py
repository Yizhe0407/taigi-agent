"""Small CLI diagnostics helper for the agent runtime.

The project still uses stdout for the local kiosk/CLI workflow. Keeping the
prefix formatting here prevents ad hoc diagnostic strings from drifting while
avoiding a full logging setup for the current single-process runtime.

When a trace is active, the same message is attached to the current span as a
`diagnostic` event so retries / context trims show up inline in SigNoz traces.
Messages here are operational only (no user content) — see
docs/observability.md for the content policy.
"""

from opentelemetry import trace


def log_diagnostic(scope: str, message: str) -> None:
    print(f"[{scope}] {message}")
    span = trace.get_current_span()
    if span.is_recording():
        span.add_event(
            "diagnostic",
            {"diagnostic.scope": scope, "diagnostic.message": message},
        )
