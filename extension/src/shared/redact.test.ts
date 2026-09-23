import { describe, expect, it } from "vitest";
import { REDACTED, redact, redactHeaders, redactUrl } from "./redact";

describe("redact", () => {
  it("strips secret headers, keeps others", () => {
    expect(
      redactHeaders({ Cookie: "a=b", Authorization: "Bearer x", "X-Csrf-Token": "t", "Content-Type": "j" }),
    ).toEqual({ Cookie: REDACTED, Authorization: REDACTED, "X-Csrf-Token": REDACTED, "Content-Type": "j" });
  });

  it("keeps prompts, strips secret keys and JWTs", () => {
    const jwt = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3OCJ9.abcdefghijklmnop";
    const out = redact({
      prompt: "a red fox",
      access_token: "abc",
      refreshToken: "def",
      prompt_tokens: 12,
      nested: [{ jwt: "x", note: `Bearer ${jwt}` }],
    }) as Record<string, unknown>;
    expect(out.prompt).toBe("a red fox");
    expect(out.access_token).toBe(REDACTED);
    expect(out.refreshToken).toBe(REDACTED);
    expect(out.prompt_tokens).toBe(12);
    expect(JSON.stringify(out)).not.toContain(jwt);
    expect(JSON.stringify(out)).not.toContain('"x"');
  });

  it("redacts signed URL values but keeps shape", () => {
    const out = redactUrl("https://cdn.example.com/a.mp4?Expires=123&Signature=zzz&X-Amz-Signature=yyy&w=1");
    expect(out).not.toContain("zzz");
    expect(out).not.toContain("yyy");
    expect(out).toContain("Expires=123");
    expect(out).toContain("w=1");
  });

  it("redacts URLs embedded in strings", () => {
    expect(redact("see https://x.io/f?token=abc end")).not.toContain("abc");
  });

  it("redacts signed URLs inside JSON-encoded strings with escaped slashes", () => {
    const pusher = String.raw`{"url":"https:\/\/pikaso.cdnpk.net\/private\/production\/1\/render.png?token=exp=179~hmac=deadbeef","x":1}`;
    const out = redact({ event: "creation.updated", data: pusher }) as { data: string };
    expect(out.data).not.toContain("deadbeef");
    expect(out.data).toContain(String.raw`render.png?token=[REDACTED]`);
    expect(JSON.parse(out.data).x).toBe(1);
  });
});
