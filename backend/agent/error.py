"""Human-readable error summary for CLI / API surfaces.

LLM endpoints and other upstreams often return verbose HTML error pages (e.g.
Cloudflare Tunnel 1033) that swamp logs and chat replies. `summarize_error`
keeps the essential signal and drops the noise.
"""

from __future__ import annotations

import re

_HTML_TITLE_RE = re.compile(r"<title>\s*(.*?)\s*</title>", re.IGNORECASE | re.DOTALL)
_CLOUDFLARE_ERROR_RE = re.compile(r"Cloudflare Tunnel error", re.IGNORECASE)


def summarize_error(error: Exception) -> str:
    """Keep errors readable when upstreams return verbose HTML bodies."""
    text = str(error).strip()
    if not text:
        return type(error).__name__

    if _CLOUDFLARE_ERROR_RE.search(text):
        return "Cloudflare Tunnel error 1033，LLM endpoint 目前無法連線"

    title = _HTML_TITLE_RE.search(text)
    if title is not None:
        return f"{type(error).__name__}: HTML error page: {title.group(1).strip()}"

    first_line = " ".join(text.splitlines()[:1]).strip()
    if len(first_line) > 240:
        first_line = first_line[:237] + "..."
    return f"{type(error).__name__}: {first_line}"
