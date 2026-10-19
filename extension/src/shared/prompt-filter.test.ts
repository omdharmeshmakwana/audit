import { describe, expect, it } from "vitest";
import { promptWarnings } from "./prompt-filter";

describe("prompt vocabulary", () => {
  it("warns for supplied names and punctuation variants", () => {
    expect(promptWarnings("TAYLOR   SWIFT beside Spider-Man")).toEqual(expect.arrayContaining(["taylor swift", "spider-man", "spiderman"]));
    expect(promptWarnings("quiet forest at sunrise")).toEqual([]);
    expect(promptWarnings("IRON-MAN")).toContain("ironman");
  });
});
