// Secret stripping (hard rule 9). Runs in the page world before anything is relayed.
// Must stay behaviourally identical to server/faible/redact.py.

export const REDACTED = "[REDACTED]";

const SECRET_HEADER =
  /^(cookie|set-cookie|authorization|proxy-authorization)$|token|secret|session|api[-_]?key|csrf|xsrf|signature|auth/i;

const SECRET_KEY =
  /^(password|passwd|pass|token|jwt|secret|cookie|cookies|authorization|auth|api_?key|apikey|client_secret|session|session_?id|sid|signature|credentials?)$|(_|-)?(token|secret|password|api_?key)$/i;

const SECRET_QUERY =
  /^(token|access_token|auth|key|sig|signature|x-amz-signature|x-amz-credential|x-amz-security-token|x-goog-signature|x-goog-credential|policy|key-pair-id|googleaccessid|se?ssion)$/i;

const JWT = /eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}/g;
const BEARER = /\b(Bearer|Basic)\s+[A-Za-z0-9._~+/=-]{8,}/gi;
// Secret query params anywhere in text — works on plain URLs *and* on URLs inside
// JSON-encoded strings with escaped slashes (Magnific's Pusher payloads: `https:\/\/…`).
const QUERY_SECRET_IN_TEXT = new RegExp(
  `([?&](?:${SECRET_QUERY.source.slice(2, -2)})=)[^&\\s"'\\\\<>#]+`,
  "gi",
);

export function redactUrl(url: string): string {
  let u: URL;
  try {
    u = new URL(url);
  } catch {
    return url;
  }
  if (!u.search) return url;
  for (const key of [...u.searchParams.keys()]) {
    if (SECRET_QUERY.test(key)) u.searchParams.set(key, REDACTED);
  }
  return u.toString();
}

export function redactString(s: string): string {
  return s
    .replace(JWT, REDACTED)
    .replace(BEARER, (_m, scheme: string) => `${scheme} ${REDACTED}`)
    .replace(QUERY_SECRET_IN_TEXT, (_m, prefix: string) => `${prefix}${REDACTED}`);
}

export function redact(value: unknown): unknown {
  if (Array.isArray(value)) return value.map(redact);
  if (value !== null && typeof value === "object") {
    const out: Record<string, unknown> = {};
    for (const [k, v] of Object.entries(value as Record<string, unknown>)) {
      out[k] = SECRET_KEY.test(k) ? REDACTED : redact(v);
    }
    return out;
  }
  if (typeof value === "string") return redactString(value);
  return value;
}

export function redactHeaders(headers: Record<string, string>): Record<string, string> {
  const out: Record<string, string> = {};
  for (const [k, v] of Object.entries(headers)) out[k] = SECRET_HEADER.test(k) ? REDACTED : v;
  return out;
}
