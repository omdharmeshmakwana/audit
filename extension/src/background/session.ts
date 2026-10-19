// Faible session (hard rule 8: each artist logs in to Faible individually, even though the
// studio shares one service workspace). Tokens live in chrome.storage.local and are only
// ever sent to the Faible server — never service cookies or headers (hard rule 9).

import { SERVER_URL } from "../shared/capture";

const SESSION_KEY = "faibleSession";

export interface ActiveShot {
  id: string;
  code: string;
}

export interface Session {
  access: string;
  refresh: string;
  user: { id: string; email: string; display_name: string };
  activeShot: ActiveShot | null;
}

export class NotLoggedIn extends Error {}

export async function getSession(): Promise<Session | null> {
  const s = await chrome.storage.local.get(SESSION_KEY);
  return (s[SESSION_KEY] as Session | undefined) ?? null;
}

async function saveSession(session: Session | null): Promise<void> {
  if (session) await chrome.storage.local.set({ [SESSION_KEY]: session });
  else await chrome.storage.local.remove(SESSION_KEY);
}

async function refreshAccess(session: Session): Promise<Session | null> {
  const res = await fetch(`${SERVER_URL}/api/v1/auth/refresh`, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({ refresh_token: session.refresh }),
  });
  if (!res.ok) return null;
  const body = (await res.json()) as { access_token: string };
  const next = { ...session, access: body.access_token };
  if ((await getSession())?.user.id !== session.user.id) return null;
  await saveSession(next);
  return next;
}

/** Authenticated request to the Faible API; refreshes the access token once on 401. */
export async function api(path: string, init: RequestInit = {}, expectedUserId?: string): Promise<Response> {
  let session = await getSession();
  if (!session || (expectedUserId && session.user.id !== expectedUserId)) throw new NotLoggedIn();
  const call = (s: Session) =>
    fetch(`${SERVER_URL}/api/v1${path}`, {
      ...init,
      headers: { ...(init.headers as Record<string, string> | undefined), authorization: `Bearer ${s.access}` },
    });
  let res = await call(session);
  if (res.status === 401) {
    const refreshingUserId = session.user.id;
    session = await refreshAccess(session);
    if (!session) {
      if ((await getSession())?.user.id === refreshingUserId) await saveSession(null);
      throw new NotLoggedIn();
    }
    res = await call(session);
  }
  return res;
}

export async function apiJson<T>(path: string, init: RequestInit = {}): Promise<T> {
  const res = await api(path, init);
  if (!res.ok) throw new Error(`${path}: HTTP ${res.status} ${(await res.text()).slice(0, 200)}`);
  return (await res.json()) as T;
}

interface Tree {
  project: { id: string; code: string };
  sequences: { shots: { id: string; code: string }[] }[];
}

async function shotCode(shotId: string): Promise<string | null> {
  const projects = await apiJson<{ id: string }[]>("/projects");
  for (const p of projects) {
    const tree = await apiJson<Tree>(`/projects/${p.id}/tree`);
    for (const seq of tree.sequences) for (const s of seq.shots) if (s.id === shotId) return s.code;
  }
  return null;
}

export async function login(email: string, password: string): Promise<Session> {
  const res = await fetch(`${SERVER_URL}/api/v1/auth/login`, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({ email, password, client: "extension" }),
  });
  if (!res.ok) {
    const body = await res.json() as { detail?: string };
    throw new Error(body.detail ?? "Login failed");
  }
  const tokens = (await res.json()) as { access_token: string; refresh_token: string };
  const partial: Session = {
    access: tokens.access_token,
    refresh: tokens.refresh_token,
    user: { id: "", email, display_name: email },
    activeShot: null,
  };
  await saveSession(partial);
  // Restore the last active shot (spec §4.2) from the server.
  const me = await apiJson<{ id: string; email: string; display_name: string; active_shot_id: string | null }>("/me");
  const code = me.active_shot_id ? await shotCode(me.active_shot_id) : null;
  const session: Session = {
    ...partial,
    user: { id: me.id, email: me.email, display_name: me.display_name },
    activeShot: me.active_shot_id && code ? { id: me.active_shot_id, code } : null,
  };
  await saveSession(session);
  return session;
}

export async function logout(): Promise<void> {
  await saveSession(null);
}

export async function setActiveShot(shot: ActiveShot | null): Promise<void> {
  await apiJson("/me/active-shot", {
    method: "PUT",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({ shot_id: shot?.id ?? null }),
  });
  const session = await getSession();
  if (session) await saveSession({ ...session, activeShot: shot });
}
