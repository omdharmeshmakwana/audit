// Local/offline JSON export of this browser's history, in the standalone exporter's formats
// (higgsfield_exporter/extension/exporter.js), so the exporter's audit page, review page,
// merge script and usage dashboard keep reading the files. Writes into a folder the user
// picked once (File System Access API); chrome.downloads can only write inside ~/Downloads.

import { getGenerations, getUsage, type AccountRecord, type GenerationRecord } from "./db";

// File System Access API surface used here (not in TypeScript's DOM lib).
interface PermissionDescriptor {
  mode: "read" | "readwrite";
}
export interface FolderHandle extends FileSystemDirectoryHandle {
  queryPermission(d: PermissionDescriptor): Promise<PermissionState>;
  requestPermission(d: PermissionDescriptor): Promise<PermissionState>;
}
declare global {
  interface Window {
    showDirectoryPicker(options?: { id?: string; mode?: "read" | "readwrite"; startIn?: string }): Promise<FolderHandle>;
  }
}

export const displayName = (a: Pick<AccountRecord, "label" | "email" | "name" | "username" | "key">) =>
  a.label || a.email || a.name || a.username || a.key;

// ---------------------------------------------------------------- remembered export folder

const SETTINGS_DB = "faible-history-settings";
const FOLDER_KEY = "exportFolder";

function settingsStore(mode: IDBTransactionMode): Promise<IDBObjectStore> {
  return new Promise((resolve, reject) => {
    const req = indexedDB.open(SETTINGS_DB, 1);
    req.onupgradeneeded = () => req.result.createObjectStore("kv");
    req.onsuccess = () => resolve(req.result.transaction("kv", mode).objectStore("kv"));
    req.onerror = () => reject(req.error);
  });
}

export async function getFolder(): Promise<FolderHandle | null> {
  const store = await settingsStore("readonly");
  return new Promise((resolve, reject) => {
    const req = store.get(FOLDER_KEY);
    req.onsuccess = () => resolve((req.result as FolderHandle | undefined) ?? null);
    req.onerror = () => reject(req.error);
  });
}

async function setFolder(handle: FolderHandle): Promise<void> {
  const store = await settingsStore("readwrite");
  return new Promise((resolve, reject) => {
    const req = store.put(handle, FOLDER_KEY);
    req.onsuccess = () => resolve();
    req.onerror = () => reject(req.error);
  });
}

/** The folder handle if it can be written without prompting, else null. */
export async function writableFolder(): Promise<FolderHandle | null> {
  const handle = await getFolder();
  if (handle && (await handle.queryPermission({ mode: "readwrite" })) === "granted") return handle;
  return null;
}

/** Must be called from a click handler on a regular tab (not the popup). */
export async function ensureWritableFolder({ forcePick = false } = {}): Promise<FolderHandle> {
  let handle = forcePick ? null : await getFolder();
  if (!handle) {
    handle = await window.showDirectoryPicker({ id: "faible-history-exports", mode: "readwrite", startIn: "documents" });
    await setFolder(handle);
  }
  if ((await handle.requestPermission({ mode: "readwrite" })) !== "granted") {
    throw new Error("Write access to the export folder was not granted.");
  }
  return handle;
}

// ---------------------------------------------------------------- normalization (exporter generations.json)

const KNOWN_PARAM_KEYS = [
  "prompt", "enhanced_prompt", "negative_prompt", "aspect_ratio", "duration",
  "resolution", "width", "height", "seed", "batch_size", "generate_audio", "fps",
  "steps", "guidance_scale", "strength", "camera_motion", "motion_strength",
];

type Json = Record<string, unknown>;
const isObj = (v: unknown): v is Json => v !== null && typeof v === "object" && !Array.isArray(v);

function firstPresent(obj: Json, ...keys: string[]): unknown {
  for (const k of keys) if (k in obj) return obj[k];
  return null;
}

function normalizeMedia(media: unknown): Json[] {
  if (!Array.isArray(media)) return [];
  return media.map((item) => {
    if (!isObj(item)) return { value: item };
    const data = isObj(item.data) ? item.data : {};
    return { role: item.role ?? null, id: data.id || item.id || null, type: data.type || item.type || null, url: data.url || item.url || null, raw: item };
  });
}

export function normalizeGeneration(record: GenerationRecord): Json {
  const g = record.data;
  const params = isObj(g.params) ? g.params : {};
  const settings: Json = {};
  for (const k of KNOWN_PARAM_KEYS) if (k in params) settings[k] = params[k];
  return {
    generation_id: g.id,
    type: g.type ?? null,
    status: g.status ?? null,
    model: g.model ?? null,
    created_at: firstPresent(g, "createdAt", "created_at", "created"),
    prompt: params.prompt ?? null,
    enhanced_prompt: params.enhanced_prompt ?? null,
    negative_prompt: params.negative_prompt ?? null,
    input_media: normalizeMedia(firstPresent(params, "medias", "media", "inputs", "references")),
    output: g.results ?? null,
    source_response_urls: [...record.sources].sort(),
    all_params: params,
    settings,
    raw: g,
  };
}

function createdSortKey(item: Json): number {
  const v = item.created_at;
  if (typeof v === "number") return v < 1e12 ? v * 1000 : v;
  if (typeof v === "string") return Date.parse(v) || 0;
  return 0;
}

// ---------------------------------------------------------------- writing

const safeName = (s: string) => String(s).replace(/[^\w.@+-]+/g, "_").slice(0, 80) || "account";

async function writeJson(dir: FileSystemDirectoryHandle, name: string, obj: unknown): Promise<void> {
  const file = await dir.getFileHandle(name, { create: true });
  const writable = await file.createWritable();
  await writable.write(new Blob([JSON.stringify(obj, null, 2)], { type: "application/json" }));
  await writable.close();
}

/**
 * Writes <folder>/<account>/generations.json, generations_raw.json and usage_raw.json,
 * replacing old copies. A file is only written when this browser captured that kind of
 * data, so a usage-only capture can't blank an earlier generations export.
 */
export async function exportAccount(folder: FolderHandle, account: AccountRecord): Promise<{ folder: string; count: number; usage: number }> {
  const records = await getGenerations(account.key);
  const usage = (await getUsage(account.key)).sort((a, b) => a.capturedAt.localeCompare(b.capturedAt));
  const normalized = records.map(normalizeGeneration).sort((a, b) => createdSortKey(b) - createdSortKey(a));
  const accountMeta = { label: displayName(account), email: account.email || null, name: account.name || null, username: account.username || null, id: account.id || null };
  const meta = {
    exported_at: new Date().toISOString(),
    exported_by: "faible-extension",
    account: accountMeta,
    generation_count: normalized.length,
    note:
      "Data is captured from JSON responses returned to the Higgsfield web UI, after Faible's redaction. " +
      "Fields not returned by Higgsfield cannot be reconstructed. The account is the Higgsfield login, not an employee.",
  };
  const dir = await folder.getDirectoryHandle(safeName(displayName(account)), { create: true });
  if (records.length) {
    await writeJson(dir, "generations.json", { meta, generations: normalized });
    await writeJson(dir, "generations_raw.json", { meta, generations: records.map((r) => r.data) });
  }
  if (usage.length) {
    await writeJson(dir, "usage_raw.json", {
      meta: {
        exported_at: meta.exported_at,
        exported_by: meta.exported_by,
        account: accountMeta,
        capture_count: usage.length,
        note:
          "Each capture is one Higgsfield page (e.g. Usage or Organization) saved from the popup: " +
          "the JSON responses it loaded, plus the tables and text it displayed at that moment.",
      },
      captures: usage.map((u) => ({
        captured_at: u.capturedAt,
        page_url: u.pageUrl,
        page_title: u.pageTitle,
        responses: u.responses.map((r) => ({ url: r.url, page_path: r.pagePath, captured_at: r.capturedAt, data: r.data })),
        tables: u.tables,
        text: u.text,
      })),
    });
  }
  return { folder: `${folder.name}/${dir.name}`, count: normalized.length, usage: usage.length };
}
