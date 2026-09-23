import { higgsfield } from "./higgsfield";
import { magnific } from "./magnific";
import type { FingerprintAdapter } from "./types";

const ADAPTERS: Record<string, FingerprintAdapter> = { higgsfield, magnific };

export function adapterFor(service: string): FingerprintAdapter | null {
  return ADAPTERS[service] ?? null;
}
