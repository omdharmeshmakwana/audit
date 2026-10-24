// Higgsfield history collection, ported from the standalone exporter
// (higgsfield_exporter/extension/injected.js + content.js) onto Faible's single capture path:
// these are pure functions over the records the page-world observer already relays, after
// Faible's redaction (DECISIONS #59). Nothing here patches fetch a second time.

/** The Higgsfield login a page is signed in to — the studio's shared subscription. It labels
 *  history exports and is sent as metadata; attribution is always the Faible user (rule 8). */
export interface ServiceAccountInfo {
  key: string;
  id: string | null;
  email: string | null;
  name: string | null;
  username: string | null;
  /** "clerk", the (redacted) API URL it was read from, "manual", or "fallback". */
  source: string;
  manual?: boolean;
}

export type AccountCandidate = Omit<ServiceAccountInfo, "key">;

export const ACCOUNT_URL_RE = /(clerk|\/v1\/client|\/me\b|\/users?\/me|\/user\b|\/profile|\/account|\/auth|\/session)/i;
/** Responses kept for "Save usage page" even when the page path changed (exporter rule). */
export const USAGE_URL_RE = /(usage|billing|credit|transaction|spend|wallet|balance|subscription|invoice|analytics|organization|workspace|member)/i;

type Json = Record<string, unknown>;
const isObj = (v: unknown): v is Json => v !== null && typeof v === "object" && !Array.isArray(v);
const EMAIL_RE = /^[^\s@]+@[^\s@]+\.[^\s@]+$/;

export function isGenerationLike(obj: unknown): obj is Json & { id: string } {
  if (!isObj(obj) || typeof obj.id !== "string") return false;
  const has = (k: string) => k in obj;
  return (has("params") && (has("results") || has("model"))) || (has("model") && has("status") && (has("results") || has("params")));
}

/** Every generation-like object anywhere in a response body. */
export function collectGenerations(root: unknown): (Json & { id: string })[] {
  const found: (Json & { id: string })[] = [];
  const stack: unknown[] = [root];
  while (stack.length) {
    const n = stack.pop();
    if (Array.isArray(n)) {
      for (const item of n) stack.push(item);
    } else if (isObj(n)) {
      if (isGenerationLike(n)) found.push(n);
      for (const v of Object.values(n)) stack.push(v);
    }
  }
  return found;
}

function emailOf(o: Json): string | null {
  for (const k of ["email", "email_address", "emailAddress", "primary_email"]) {
    const v = o[k];
    if (typeof v === "string" && EMAIL_RE.test(v)) return v;
  }
  if (Array.isArray(o.email_addresses) && o.email_addresses.length) {
    const list = o.email_addresses.filter(isObj);
    const primary = list.find((e) => e.id === o.primary_email_address_id) ?? list[0];
    const v = primary?.email_address;
    if (typeof v === "string" && EMAIL_RE.test(v)) return v;
  }
  return null;
}

const s = (v: unknown): string | null => (typeof v === "string" && v ? v : null);

function accountFromUser(o: unknown, source: string): AccountCandidate | null {
  if (!isObj(o)) return null;
  const email = emailOf(o);
  if (!email) return null;
  const name = [s(o.first_name), s(o.last_name)].filter(Boolean).join(" ") || s(o.full_name) || s(o.fullName) || s(o.name) || s(o.display_name);
  return {
    id: typeof o.id === "string" || typeof o.id === "number" ? String(o.id) : null,
    email,
    name: name || null,
    username: s(o.username),
    source,
  };
}

function accountFromClerkClient(client: unknown, source: string): AccountCandidate | null {
  if (!isObj(client) || !Array.isArray(client.sessions)) return null;
  const sessions = client.sessions.filter(isObj);
  const session = sessions.find((x) => x.id === client.last_active_session_id) ?? sessions[0];
  return session ? accountFromUser(session.user, source) : null;
}

/** The signed-in user from an account-like response. Only the top of the payload is read,
 *  so creator info embedded in feeds is never mistaken for the login (exporter rule). */
export function findAccount(data: unknown, url: string): AccountCandidate | null {
  if (!isObj(data)) return null;
  for (const c of [data.client, data.response, data]) {
    const a = accountFromClerkClient(c, url);
    if (a) return a;
  }
  const nested = isObj(data.data) ? data.data : {};
  for (const c of [data, data.user, data.data, nested.user, data.response, data.me, data.profile, data.account]) {
    const a = accountFromUser(c, url);
    if (a) return a;
  }
  return null;
}

export function accountKey(a: Pick<ServiceAccountInfo, "email" | "id" | "username">): string | null {
  if (a.email) return a.email.toLowerCase();
  if (a.id) return `id:${a.id}`;
  if (a.username) return `user:${a.username}`;
  return null;
}

/** Faible's `ServiceLogin` payload (server/faible/events.py) for an account. */
export function serviceLogin(a: ServiceAccountInfo | null, label: string | null = null): Record<string, string | null> | null {
  if (!a || a.key === "unknown") return label ? { label, detected_by: "manual" } : null;
  const detected_by = a.source === "clerk" ? "clerk" : a.source === "manual" ? "manual" : a.source === "fallback" ? "unknown" : "api";
  return {
    service_user_id: a.id,
    email: a.email,
    name: a.name,
    username: a.username,
    label: label ?? (a.manual ? a.name : null),
    detected_by,
  };
}

/** A stable content signature (two 32-bit FNV-style lanes over the JSON text): the same
 *  history object seen again is not re-sent; a changed one (status, results) is. */
export function signature(value: unknown): string {
  const text = JSON.stringify(value) ?? "";
  let a = 0x811c9dc5;
  let b = 0x9747b28c ^ text.length;
  for (let i = 0; i < text.length; i++) {
    const c = text.charCodeAt(i);
    a = Math.imul(a ^ c, 0x01000193) >>> 0;
    b = Math.imul(b ^ c, 0x5bd1e995) >>> 0;
  }
  return `${text.length.toString(36)}-${a.toString(36)}-${b.toString(36)}`;
}
