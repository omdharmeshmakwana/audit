import { higgsfield } from "./higgsfield";
import { magnific } from "./magnific";
import type { Adapter } from "./types";

const ADAPTERS: Record<string, Adapter> = { higgsfield, magnific };

export function adapterFor(service: string): Adapter | null {
  return ADAPTERS[service] ?? null;
}
