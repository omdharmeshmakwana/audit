import type { CaptureRecord } from "../shared/capture";

/** A generated output the service told us about (a location, not yet an asset). */
export interface JobOutput {
  service: string;
  job_id: string;
  url: string;
  media_type: string | null; // "image" | "video" | null when the payload doesn't say
  status: string;
  source: "job_detail" | "history" | "notification";
}

/** What the fingerprinting layer needs from one captured record. */
export interface Observation {
  /** Jobs this browser submitted (attribution: shared workspace, so only these are ours). */
  submittedJobIds: string[];
  outputs: JobOutput[];
}

export interface FingerprintAdapter {
  service: string;
  observe(record: CaptureRecord): Observation;
  /** Job id recoverable from a downloaded file's name, if the service names files that way. */
  jobIdFromFilename(filename: string): string | null;
}

export const EMPTY: Observation = { submittedJobIds: [], outputs: [] };
