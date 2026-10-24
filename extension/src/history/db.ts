// Per-browser history store for offline JSON export, shared by the worker, the popup and the
// export page (same extension origin). Ported from higgsfield_exporter/extension/db.js with
// its schema unchanged (DECISIONS #59):
//   accounts:    {key, email, name, username, id, label, source, firstSeen, lastSeen}
//   generations: {accountKey, id, data, sources[], firstCapturedAt, lastCapturedAt}
//   usage:       {accountKey, id, capturedAt, pageUrl, pageTitle, responses[], tables[], text}
// Everything stored here has already been through Faible's redaction.

const DB_NAME = "faible-history";
const VERSION = 1;

export interface AccountRecord {
  key: string;
  email: string | null;
  name: string | null;
  username: string | null;
  id: string | null;
  label: string | null;
  source: string | null;
  firstSeen: string;
  lastSeen: string;
  count?: number;
  usageCount?: number;
}

export interface GenerationRecord {
  accountKey: string;
  id: string;
  data: Record<string, unknown>;
  sources: string[];
  firstCapturedAt: string;
  lastCapturedAt: string;
}

export interface UsageResponse {
  url: string;
  pagePath: string;
  capturedAt: string;
  data: unknown;
}

export interface UsageCapture {
  capturedAt: string;
  pageUrl: string;
  pageTitle: string;
  responses: UsageResponse[];
  tables: string[][][];
  text: string;
}

export interface UsageRecord extends UsageCapture {
  accountKey: string;
  id: string;
}

let dbPromise: Promise<IDBDatabase> | null = null;

function open(): Promise<IDBDatabase> {
  dbPromise ??= new Promise((resolve, reject) => {
    const req = indexedDB.open(DB_NAME, VERSION);
    req.onupgradeneeded = (event) => {
      const db = req.result;
      if (event.oldVersion < 1) {
        db.createObjectStore("accounts", { keyPath: "key" });
        db.createObjectStore("generations", { keyPath: ["accountKey", "id"] }).createIndex("byAccount", "accountKey");
        db.createObjectStore("usage", { keyPath: ["accountKey", "id"] }).createIndex("byAccount", "accountKey");
      }
    };
    req.onsuccess = () => {
      const db = req.result;
      // Don't block a newer version (after an extension update) from upgrading.
      db.onversionchange = () => {
        db.close();
        dbPromise = null;
      };
      resolve(db);
    };
    req.onerror = () => reject(req.error);
  });
  return dbPromise;
}

const done = (tx: IDBTransaction) =>
  new Promise<void>((resolve, reject) => {
    tx.oncomplete = () => resolve();
    tx.onerror = tx.onabort = () => reject(tx.error);
  });

const result = <T>(req: IDBRequest<T>) =>
  new Promise<T>((resolve, reject) => {
    req.onsuccess = () => resolve(req.result);
    req.onerror = () => reject(req.error);
  });

type AccountUpdate = Partial<AccountRecord> & { key: string };

function mergeAccount(store: IDBObjectStore, incoming: AccountUpdate, now: string): void {
  const get = store.get(incoming.key) as IDBRequest<AccountRecord | undefined>;
  get.onsuccess = () => {
    const prev = get.result;
    store.put({
      ...prev,
      key: incoming.key,
      email: incoming.email ?? prev?.email ?? null,
      name: incoming.name ?? prev?.name ?? null,
      username: incoming.username ?? prev?.username ?? null,
      id: incoming.id ?? prev?.id ?? null,
      source: incoming.source ?? prev?.source ?? null,
      label: prev?.label ?? null,
      firstSeen: prev?.firstSeen ?? now,
      lastSeen: now,
    } satisfies AccountRecord);
  };
}

export async function upsertAccount(account: AccountUpdate): Promise<void> {
  const db = await open();
  const tx = db.transaction("accounts", "readwrite");
  mergeAccount(tx.objectStore("accounts"), account, new Date().toISOString());
  return done(tx);
}

export async function saveGenerations(accountKey: string, url: string, items: Record<string, unknown>[]): Promise<void> {
  const db = await open();
  const now = new Date().toISOString();
  const tx = db.transaction(["accounts", "generations"], "readwrite");
  const store = tx.objectStore("generations");
  mergeAccount(tx.objectStore("accounts"), { key: accountKey }, now);
  for (const item of items) {
    const id = item.id as string;
    const get = store.get([accountKey, id]) as IDBRequest<GenerationRecord | undefined>;
    get.onsuccess = () => {
      const prev = get.result;
      const sources = new Set(prev?.sources ?? []);
      sources.add(url);
      store.put({
        accountKey,
        id,
        data: item,
        sources: [...sources],
        firstCapturedAt: prev?.firstCapturedAt ?? now,
        lastCapturedAt: now,
      } satisfies GenerationRecord);
    };
  }
  return done(tx);
}

/** One capture of a usage/billing page: the JSON it loaded plus what it displayed. */
export async function saveUsage(accountKey: string, capture: UsageCapture): Promise<void> {
  const db = await open();
  const tx = db.transaction(["accounts", "usage"], "readwrite");
  mergeAccount(tx.objectStore("accounts"), { key: accountKey }, new Date().toISOString());
  tx.objectStore("usage").put({ ...capture, accountKey, id: capture.capturedAt } satisfies UsageRecord);
  return done(tx);
}

export async function getAccount(key: string): Promise<AccountRecord | undefined> {
  const db = await open();
  return result(db.transaction("accounts", "readonly").objectStore("accounts").get(key) as IDBRequest<AccountRecord | undefined>);
}

export async function listAccounts(): Promise<AccountRecord[]> {
  const db = await open();
  const tx = db.transaction(["accounts", "generations", "usage"], "readonly");
  const accounts = await result(tx.objectStore("accounts").getAll() as IDBRequest<AccountRecord[]>);
  const gens = tx.objectStore("generations").index("byAccount");
  const usage = tx.objectStore("usage").index("byAccount");
  await Promise.all(
    accounts.map(async (a) => {
      [a.count, a.usageCount] = await Promise.all([result(gens.count(a.key)), result(usage.count(a.key))]);
    }),
  );
  return accounts.sort((a, b) => (b.lastSeen || "").localeCompare(a.lastSeen || ""));
}

export async function getGenerations(accountKey: string): Promise<GenerationRecord[]> {
  const db = await open();
  return result(db.transaction("generations", "readonly").objectStore("generations").index("byAccount").getAll(accountKey) as IDBRequest<GenerationRecord[]>);
}

export async function getUsage(accountKey: string): Promise<UsageRecord[]> {
  const db = await open();
  return result(db.transaction("usage", "readonly").objectStore("usage").index("byAccount").getAll(accountKey) as IDBRequest<UsageRecord[]>);
}

export async function renameAccount(key: string, label: string | null): Promise<void> {
  const db = await open();
  const tx = db.transaction("accounts", "readwrite");
  const store = tx.objectStore("accounts");
  const acct = await result(store.get(key) as IDBRequest<AccountRecord | undefined>);
  if (acct) store.put({ ...acct, label: label || null });
  return done(tx);
}

/** Deletes this browser's copy only; what was already sent to Faible stays in its log. */
export async function deleteAccount(key: string): Promise<void> {
  const db = await open();
  const tx = db.transaction(["accounts", "generations", "usage"], "readwrite");
  tx.objectStore("accounts").delete(key);
  tx.objectStore("generations").delete(IDBKeyRange.bound([key], [key, []]));
  tx.objectStore("usage").delete(IDBKeyRange.bound([key], [key, []]));
  return done(tx);
}
