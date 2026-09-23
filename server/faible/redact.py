"""Secret stripping (hard rule 9).

The extension redacts before anything leaves the page. This module is the server-side
second pass: anything that reaches disk goes through `redact` again.
Must stay behaviourally identical to `extension/src/shared/redact.ts`.
"""

import re
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

REDACTED = "[REDACTED]"

SECRET_HEADER = re.compile(
    r"^(cookie|set-cookie|authorization|proxy-authorization)$"
    r"|token|secret|session|api[-_]?key|csrf|xsrf|signature|auth",
    re.IGNORECASE,
)

SECRET_KEY = re.compile(
    r"^(password|passwd|pass|token|jwt|secret|cookie|cookies|authorization|auth|"
    r"api_?key|apikey|client_secret|session|session_?id|sid|signature|credentials?)$"
    r"|(_|-)?(token|secret|password|api_?key)$",
    re.IGNORECASE,
)

# Query params on signed URLs whose *values* are secrets. Names are kept so the
# adapter docs can describe the URL shape (and expiry params stay readable).
SECRET_QUERY = re.compile(
    r"^(token|access_token|auth|key|sig|signature|x-amz-signature|x-amz-credential|"
    r"x-amz-security-token|x-goog-signature|x-goog-credential|policy|key-pair-id|"
    r"googleaccessid|se?ssion)$",
    re.IGNORECASE,
)

JWT = re.compile(r"eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}")
BEARER = re.compile(r"\b(Bearer|Basic)\s+[A-Za-z0-9._~+/=-]{8,}", re.IGNORECASE)
# Secret query params anywhere in text — plain URLs and URLs inside JSON-encoded strings
# with escaped slashes (Magnific's Pusher payloads: `https:\/\/…`).
QUERY_SECRET_IN_TEXT = re.compile(
    r"([?&](?:" + SECRET_QUERY.pattern[2:-2] + r")=)[^&\s\"'\\<>#]+", re.IGNORECASE
)


def redact_url(url: str) -> str:
    try:
        parts = urlsplit(url)
    except ValueError:
        return url
    if not parts.query:
        return url
    q = [(k, REDACTED if SECRET_QUERY.search(k) else v) for k, v in parse_qsl(parts.query, True)]
    return urlunsplit(parts._replace(query=urlencode(q, safe="[]")))


def redact_string(s: str) -> str:
    s = JWT.sub(REDACTED, s)
    s = BEARER.sub(lambda m: f"{m.group(1)} {REDACTED}", s)
    return QUERY_SECRET_IN_TEXT.sub(lambda m: m.group(1) + REDACTED, s)


def redact(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            k: (REDACTED if isinstance(k, str) and SECRET_KEY.search(k) else redact(v))
            for k, v in value.items()
        }
    if isinstance(value, list):
        return [redact(v) for v in value]
    if isinstance(value, str):
        return redact_string(value)
    return value


def redact_headers(headers: dict[str, str]) -> dict[str, str]:
    return {k: REDACTED if SECRET_HEADER.search(k) else v for k, v in headers.items()}
