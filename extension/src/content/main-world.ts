// Page-world observer. Runs in the MAIN world at document_start so it patches
// fetch / XHR / WebSocket / EventSource before the service's own bundle captures them.
//
// Rules: never modify requests or responses; redact before relaying; never throw into
// the page (every observer path is wrapped in try/catch).

import {
  MAX_BODY_CHARS,
  MAX_HASH_BYTES,
  MSG_TAG,
  SIGNED_OUTPUT_HOST,
  serviceForHost,
  type CaptureRecord,
  type Transport,
} from "../shared/capture";
import { sha256Hex } from "../shared/hash";
import { redact, redactHeaders, redactString, redactUrl } from "../shared/redact";

const SERVICE = serviceForHost(location.hostname);

// Third-party telemetry: pure noise for adapter discovery.
const NOISE_HOST =
  /(google-analytics|googletagmanager|doubleclick|sentry|posthog|amplitude|segment\.(io|com)|mixpanel|intercom|hotjar|clarity\.ms|datadoghq|facebook|tiktok|stripe\.com|hcaptcha|recaptcha|cloudflareinsights|clerk\.)/i;

// Non-JSON GETs of these types are skipped entirely (page chrome, thumbnails).
const STATIC_TYPE = /^(text\/(html|css|javascript)|application\/javascript|font\/|image\/)/i;

/** Signed output-CDN URLs in a body, keyed by the redacted form redact() will produce. */
function collectSignedUrls(body: unknown, out: Record<string, string> = {}): Record<string, string> {
  if (typeof body === "string") {
    if (/^https:\/\//.test(body)) {
      try {
        if (SIGNED_OUTPUT_HOST.test(new URL(body).hostname)) {
          const key = redactString(body);
          if (key !== body) out[key] = body;
        }
      } catch {
        /* not a URL */
      }
    }
  } else if (Array.isArray(body)) {
    for (const v of body) collectSignedUrls(v, out);
  } else if (body !== null && typeof body === "object") {
    for (const v of Object.values(body)) collectSignedUrls(v, out);
  }
  return out;
}

function post(rec: Omit<CaptureRecord, "service" | "captured_at" | "page_url">): void {
  try {
    const signed = collectSignedUrls(rec.response_body);
    const full: CaptureRecord = {
      service: SERVICE,
      captured_at: new Date().toISOString(),
      page_url: redactUrl(location.href),
      ...rec,
      url: rec.url ? redactUrl(rec.url) : rec.url,
      request_headers: redactHeaders(rec.request_headers),
      response_headers: redactHeaders(rec.response_headers),
      request_body: redact(rec.request_body),
      response_body: redact(rec.response_body),
      extra: redact(rec.extra) as Record<string, unknown>,
      ...(Object.keys(signed).length ? { signed_urls: signed } : {}),
    };
    window.postMessage({ [MSG_TAG]: true, record: full }, location.origin);
  } catch {
    /* never break the page */
  }
}

// First-party proxies for telemetry: Higgsfield's /api/i/* (PostHog), dd.* (Datadog),
// Magnific's fpkph.cdnpk.net (PostHog), Braze, Google collectors, Cloudflare RUM,
// GrowthBook flags, cookie banner.
const NOISE_URL =
  /^https:\/\/(dd\.higgsfield\.ai\/|higgsfield\.ai\/api\/i\/|fpkph\.cdnpk\.net\/|[^/]+\/cdn-cgi\/rum|www\.google\.com\/(ccm|measurement)\/|analytics\.google\.com\/)|growthbook\.io|securiti\.ai|braze\.(eu|com)/i;

function isNoise(url: string): boolean {
  try {
    const u = new URL(url, location.href);
    return NOISE_HOST.test(u.hostname) || NOISE_URL.test(u.href);
  } catch {
    return false;
  }
}

function truncate(s: string): { text: string; truncated: boolean } {
  return s.length > MAX_BODY_CHARS
    ? { text: s.slice(0, MAX_BODY_CHARS), truncated: true }
    : { text: s, truncated: false };
}

/** Parse a text body: JSON if it parses, otherwise redacted (truncated) text. */
function parseBody(s: string): { body: unknown; json: boolean; truncated: boolean } {
  const { text, truncated } = truncate(s);
  if (!truncated) {
    const t = text.trimStart();
    if (t.startsWith("{") || t.startsWith("[")) {
      try {
        return { body: JSON.parse(text), json: true, truncated };
      } catch {
        /* fall through */
      }
    }
  }
  return { body: redactString(text), json: false, truncated };
}

/** Summarise a request body without consuming anything the page still needs. */
function describeRequestBody(body: unknown): unknown {
  if (body == null) return undefined;
  if (typeof body === "string") return parseBody(body).body;
  if (body instanceof URLSearchParams) return Object.fromEntries(body.entries());
  if (body instanceof FormData) {
    const out: Record<string, unknown> = {};
    body.forEach((v, k) => {
      out[k] =
        typeof v === "string"
          ? parseBody(v).body
          : { __file: true, name: v.name, type: v.type, size: v.size };
    });
    return { __formdata: out };
  }
  if (body instanceof Blob) return { __blob: true, type: body.type, size: body.size };
  if (body instanceof ArrayBuffer || ArrayBuffer.isView(body)) {
    return { __binary: true, size: (body as ArrayBuffer).byteLength };
  }
  return { __unknown_body: Object.prototype.toString.call(body) };
}

// ---------------------------------------------------------------- file hashing
//
// Uploads (Higgsfield S3 PUT, Magnific multipart) and media Blobs behind object URLs are
// hashed in the page: the bytes are right here, and the worker could never get them (a
// Blob can't cross chrome.runtime messaging). Blobs are immutable, so reading one never
// disturbs the page's own use of it. One hash per Blob, however many times it's used.

const blobHashes = new WeakMap<Blob, Promise<string>>();

function hashBlob(blob: Blob): Promise<string> | null {
  if (blob.size === 0 || blob.size > MAX_HASH_BYTES) return null;
  let p = blobHashes.get(blob);
  if (!p) {
    p = blob.arrayBuffer().then(sha256Hex);
    blobHashes.set(blob, p);
  }
  return p;
}

/** `describeRequestBody` plus `__sha256` on each file (Blob body, or Files in FormData). */
async function describeWithFileHashes(body: unknown): Promise<unknown> {
  const described = describeRequestBody(body);
  try {
    if (body instanceof Blob && described && typeof described === "object") {
      const sha = await hashBlob(body);
      if (sha) (described as Record<string, unknown>).__sha256 = sha;
    } else if (body instanceof FormData && described && typeof described === "object") {
      const form = (described as { __formdata: Record<string, Record<string, unknown>> }).__formdata;
      for (const [k, v] of body.entries()) {
        if (v instanceof Blob && form[k]) {
          const sha = await hashBlob(v);
          if (sha) form[k].__sha256 = sha;
        }
      }
    }
  } catch {
    /* unhashable: record the upload without a hash */
  }
  return described;
}

function headersToObject(h: HeadersInit | Headers | undefined | null): Record<string, string> {
  const out: Record<string, string> = {};
  if (!h) return out;
  try {
    new Headers(h).forEach((v, k) => (out[k] = v));
  } catch {
    /* ignore */
  }
  return out;
}

function parseXhrHeaders(raw: string): Record<string, string> {
  const out: Record<string, string> = {};
  for (const line of raw.trim().split(/[\r\n]+/)) {
    const i = line.indexOf(":");
    if (i > 0) out[line.slice(0, i).trim().toLowerCase()] = line.slice(i + 1).trim();
  }
  return out;
}

// ---------------------------------------------------------------- fetch

const origFetch = window.fetch;

async function observeFetch(
  input: RequestInfo | URL,
  init: RequestInit | undefined,
  // A clone taken the instant the response resolved, before the page can read the body.
  responsePromise: Promise<Response>,
  started: number,
): Promise<void> {
  const req = input instanceof Request ? input : null;
  const url = req ? req.url : String(input instanceof URL ? input.href : input);
  if (isNoise(url)) return;
  const method = (init?.method ?? req?.method ?? "GET").toUpperCase();
  const requestHeaders = { ...headersToObject(req?.headers), ...headersToObject(init?.headers) };

  let requestBody: unknown;
  if (init?.body !== undefined) requestBody = await describeWithFileHashes(init.body);
  else if (req && req.body) {
    try {
      requestBody = parseBody(await req.clone().text()).body;
    } catch {
      requestBody = { __unreadable: true };
    }
  }

  let res: Response;
  try {
    res = await responsePromise;
  } catch (err) {
    post({
      transport: "fetch",
      method,
      url,
      request_headers: requestHeaders,
      request_body: requestBody,
      response_headers: {},
      extra: { network_error: String(err), duration_ms: Date.now() - started },
    });
    return;
  }

  const responseHeaders = headersToObject(res.headers);
  const ctype = res.headers.get("content-type") ?? "";
  const base = {
    method,
    url,
    status: res.status,
    request_headers: requestHeaders,
    request_body: requestBody,
    response_headers: responseHeaders,
  };

  if (/text\/event-stream|ndjson|x-ndjson/i.test(ctype) && res.body) {
    const reader = res.body!.getReader();
    const dec = new TextDecoder();
    let chunkIndex = 0;
    for (;;) {
      const { done, value } = await reader.read();
      if (done || chunkIndex > 500) break;
      post({
        ...base,
        transport: "fetch-stream",
        response_body: parseBody(dec.decode(value, { stream: true })).body,
        extra: { chunk_index: chunkIndex++ },
      });
    }
    return;
  }

  const isJsonType = /json/i.test(ctype);
  if (!isJsonType && (method === "GET" && (STATIC_TYPE.test(ctype) || /^video\/|^audio\//.test(ctype)))) {
    // Media GETs: metadata only — enough to learn output URL shapes.
    if (/^video\/|^audio\/|^image\//.test(ctype)) {
      post({ ...base, transport: "fetch", extra: { body_skipped: ctype, duration_ms: Date.now() - started } });
    }
    return;
  }

  let parsed: ReturnType<typeof parseBody> | null = null;
  if (isJsonType || /^text\//.test(ctype) || ctype === "") {
    try {
      parsed = parseBody(await res.text());
    } catch {
      parsed = null;
    }
  }
  post({
    ...base,
    transport: "fetch" as Transport,
    response_body: parsed?.body,
    extra: {
      json: parsed?.json ?? false,
      truncated: parsed?.truncated ?? false,
      body_skipped: parsed ? undefined : ctype,
      duration_ms: Date.now() - started,
    },
  });
}

window.fetch = function faibleFetch(this: unknown, input: RequestInfo | URL, init?: RequestInit) {
  const started = Date.now();
  const p = origFetch.call(this ?? window, input, init);
  try {
    // Registered before the page's own `.then`/`await`, so it runs first and the clone
    // exists before the page consumes the body. The page gets `p` itself, untouched.
    const cloned = p.then((r) => r.clone());
    void observeFetch(input, init, cloned, started).catch(() => undefined);
  } catch {
    /* never break the page */
  }
  return p;
} as typeof window.fetch;

// ---------------------------------------------------------------- XHR

interface XhrMeta {
  method: string;
  url: string;
  headers: Record<string, string>;
  started: number;
}
const xhrMeta = new WeakMap<XMLHttpRequest, XhrMeta>();
const XHR = XMLHttpRequest.prototype;
const origOpen = XHR.open;
const origSetHeader = XHR.setRequestHeader;
const origSend = XHR.send;

XHR.open = function (this: XMLHttpRequest, method: string, url: string | URL, ...rest: unknown[]) {
  try {
    xhrMeta.set(this, { method: method.toUpperCase(), url: new URL(url, location.href).href, headers: {}, started: 0 });
  } catch {
    /* ignore */
  }
  return (origOpen as (...a: unknown[]) => void).call(this, method, url, ...rest);
} as typeof XHR.open;

XHR.setRequestHeader = function (this: XMLHttpRequest, name: string, value: string) {
  const m = xhrMeta.get(this);
  if (m) m.headers[name.toLowerCase()] = value;
  return origSetHeader.call(this, name, value);
};

XHR.send = function (this: XMLHttpRequest, body?: Document | XMLHttpRequestBodyInit | null) {
  const m = xhrMeta.get(this);
  if (m && !isNoise(m.url)) {
    try {
      const hashedBody = describeWithFileHashes(body);
      m.started = Date.now();
      this.addEventListener("loadend", () => {
        try {
          const ctype = this.getResponseHeader("content-type") ?? "";
          let responseBody: unknown;
          let extra: Record<string, unknown> = { duration_ms: Date.now() - m.started };
          if (this.responseType === "" || this.responseType === "text") {
            const p = parseBody(this.responseText);
            responseBody = p.body;
            extra = { ...extra, json: p.json, truncated: p.truncated };
          } else if (this.responseType === "json") {
            responseBody = this.response;
            extra = { ...extra, json: true };
          } else {
            extra = { ...extra, body_skipped: `responseType=${this.responseType}` };
          }
          if (!/json/i.test(ctype) && m.method === "GET" && STATIC_TYPE.test(ctype)) return;
          const status = this.status;
          const responseHeaders = parseXhrHeaders(this.getAllResponseHeaders());
          void hashedBody.then((requestBody) =>
            post({
              transport: "xhr",
              method: m.method,
              url: m.url,
              status,
              request_headers: m.headers,
              request_body: requestBody,
              response_headers: responseHeaders,
              response_body: responseBody,
              extra,
            }),
          );
        } catch {
          /* ignore */
        }
      });
    } catch {
      /* ignore */
    }
  }
  return origSend.call(this, body);
};

// ---------------------------------------------------------------- WebSocket

const OrigWS = window.WebSocket;
window.WebSocket = new Proxy(OrigWS, {
  construct(target, args: ConstructorParameters<typeof WebSocket>) {
    const ws = Reflect.construct(target, args) as WebSocket;
    try {
      const url = String(args[0]);
      if (!isNoise(url)) {
        const msg = (direction: "in" | "out", data: unknown) => {
          const body =
            typeof data === "string" ? parseBody(data).body : { __binary: true, type: Object.prototype.toString.call(data) };
          // Pusher (Magnific) double-encodes: {"event": …, "data": "<JSON string>"}.
          if (body && typeof body === "object" && typeof (body as { data?: unknown }).data === "string") {
            const inner = parseBody((body as { data: string }).data);
            if (inner.json) (body as { data: unknown }).data = inner.body;
          }
          post({ transport: "ws", url, request_headers: {}, response_headers: {}, response_body: body, extra: { direction } });
        };
        post({ transport: "ws", url, request_headers: {}, response_headers: {}, extra: { event: "open_requested" } });
        ws.addEventListener("message", (e: MessageEvent) => msg("in", e.data));
        const send = ws.send.bind(ws);
        ws.send = (data: Parameters<WebSocket["send"]>[0]) => {
          try {
            msg("out", data);
          } catch {
            /* ignore */
          }
          return send(data);
        };
      }
    } catch {
      /* ignore */
    }
    return ws;
  },
});

// ---------------------------------------------------------------- EventSource

if (typeof window.EventSource === "function") {
  const OrigES = window.EventSource;
  window.EventSource = new Proxy(OrigES, {
    construct(target, args: ConstructorParameters<typeof EventSource>) {
      const es = Reflect.construct(target, args) as EventSource;
      try {
        const url = new URL(String(args[0]), location.href).href;
        if (!isNoise(url)) {
          es.addEventListener("message", (e: MessageEvent) =>
            post({
              transport: "sse",
              url,
              request_headers: {},
              response_headers: {},
              response_body: typeof e.data === "string" ? parseBody(e.data).body : undefined,
              extra: { event: e.type, last_event_id: e.lastEventId },
            }),
          );
        }
      } catch {
        /* ignore */
      }
      return es;
    },
  });
}

// ---------------------------------------------------------------- object URLs (method 2)
//
// Services save downloads from blob: URLs (Higgsfield does), which the worker cannot
// re-fetch. The Blob behind the URL is exactly the bytes that land on disk, so hash it here.
// The page gets the original URL back untouched; hashing happens after, asynchronously.

const HASHABLE_BLOB = /^(image|video|audio)\/|^application\/octet-stream$|^$/i;
const origCreateObjectURL = URL.createObjectURL.bind(URL);

URL.createObjectURL = function faibleCreateObjectURL(obj: Blob | MediaSource): string {
  const blobUrl = origCreateObjectURL(obj);
  try {
    if (obj instanceof Blob && HASHABLE_BLOB.test(obj.type) && obj.size > 0) {
      const started = Date.now();
      if (obj.size > MAX_HASH_BYTES) {
        post({
          transport: "blob",
          request_headers: {},
          response_headers: {},
          extra: { blob_url: blobUrl, size: obj.size, type: obj.type, sha256: null, skipped: "over 500 MB" },
        });
      } else {
        hashBlob(obj)?.then(
          (sha256) =>
            post({
              transport: "blob",
              request_headers: {},
              response_headers: {},
              extra: { blob_url: blobUrl, size: obj.size, type: obj.type, sha256, hash_ms: Date.now() - started },
            }),
          () => undefined,
        );
      }
    }
  } catch {
    /* never break the page */
  }
  return blobUrl;
};
