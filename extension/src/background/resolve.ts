// Pure decision: which hash identifies a download, and with what evidence (hard rule 4).
//
//   method 2 "blob"            page hashed the exact Blob that was saved       observed, 1.0
//   direct re-fetch            worker re-fetched the download URL itself        observed, 1.0
//   method 3 "filename_job"    filename → job id → that job's output URL hash   inferred, 0.9
//
// When two methods produced a hash, `agree` records whether they match — a mismatch is
// exactly what the auditor needs to see, so it is surfaced, never hidden.

export type HashMethod = "blob" | "refetch" | "filename_job";

export interface DownloadFingerprint {
  sha256: string | null;
  method: HashMethod | null;
  evidence: "observed" | "inferred" | null;
  confidence: number | null;
  job_id: string | null;
  blob_sha256: string | null;
  refetch_sha256: string | null;
  job_output_sha256: string | null;
  agree: boolean | null; // null when fewer than two hashes exist
}

export function resolveDownloadFingerprint(input: {
  blobSha256: string | null;
  refetchSha256: string | null;
  jobId: string | null;
  jobOutputSha256: string | null;
}): DownloadFingerprint {
  const { blobSha256, refetchSha256, jobId, jobOutputSha256 } = input;
  const hashes = [blobSha256, refetchSha256, jobOutputSha256].filter((h): h is string => !!h);
  const agree = hashes.length >= 2 ? hashes.every((h) => h === hashes[0]) : null;

  let pick: Pick<DownloadFingerprint, "sha256" | "method" | "evidence" | "confidence">;
  if (blobSha256) pick = { sha256: blobSha256, method: "blob", evidence: "observed", confidence: 1 };
  else if (refetchSha256) pick = { sha256: refetchSha256, method: "refetch", evidence: "observed", confidence: 1 };
  else if (jobOutputSha256) pick = { sha256: jobOutputSha256, method: "filename_job", evidence: "inferred", confidence: 0.9 };
  else pick = { sha256: null, method: null, evidence: null, confidence: null };

  return {
    ...pick,
    job_id: jobId,
    blob_sha256: blobSha256,
    refetch_sha256: refetchSha256,
    job_output_sha256: jobOutputSha256,
    agree,
  };
}
