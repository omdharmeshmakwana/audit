import { describe, expect, it } from "vitest";
import { resolveDownloadFingerprint } from "./resolve";

const A = "a".repeat(64);
const B = "b".repeat(64);
const J = "253edf08-5200-4819-9677-5f98df466685";

describe("resolveDownloadFingerprint", () => {
  it("prefers the blob hash (observed) and confirms agreement with the job output", () => {
    const fp = resolveDownloadFingerprint({ blobSha256: A, refetchSha256: null, jobId: J, jobOutputSha256: A });
    expect(fp).toMatchObject({ sha256: A, method: "blob", evidence: "observed", confidence: 1, agree: true, job_id: J });
  });

  it("surfaces a disagreement instead of hiding it", () => {
    const fp = resolveDownloadFingerprint({ blobSha256: A, refetchSha256: null, jobId: J, jobOutputSha256: B });
    expect(fp.sha256).toBe(A);
    expect(fp.agree).toBe(false);
  });

  it("uses a direct re-fetch when there is no blob", () => {
    const fp = resolveDownloadFingerprint({ blobSha256: null, refetchSha256: B, jobId: null, jobOutputSha256: null });
    expect(fp).toMatchObject({ sha256: B, method: "refetch", evidence: "observed", agree: null });
  });

  it("falls back to the filename job link as inferred", () => {
    const fp = resolveDownloadFingerprint({ blobSha256: null, refetchSha256: null, jobId: J, jobOutputSha256: A });
    expect(fp).toMatchObject({ sha256: A, method: "filename_job", evidence: "inferred", confidence: 0.9 });
  });

  it("reports unhashed when nothing worked", () => {
    const fp = resolveDownloadFingerprint({ blobSha256: null, refetchSha256: null, jobId: null, jobOutputSha256: null });
    expect(fp).toMatchObject({ sha256: null, method: null, evidence: null, agree: null });
  });
});
