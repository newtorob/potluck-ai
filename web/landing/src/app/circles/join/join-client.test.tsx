// @vitest-environment jsdom
import { act, createElement } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it } from "vitest";
import { JoinClient, codeFromHash } from "./join-client";

let host: HTMLDivElement;
let root: Root;

async function render(hash: string) {
  window.history.replaceState(null, "", `/circles/join${hash}`);
  await act(async () => { root.render(createElement(JoinClient)); });
}

beforeEach(() => {
  (globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
  host = document.createElement("div");
  document.body.appendChild(host);
  root = createRoot(host);
});
afterEach(() => {
  act(() => root.unmount());
  host.remove();
});

describe("invite link page", () => {
  it("reads the code from the fragment in whatever form it was typed", () => {
    expect(codeFromHash("#code=ABCD-EFGH-JKMN")).toBe("ABCD-EFGH-JKMN");
    expect(codeFromHash("#code=abcdefghjkmn")).toBe("ABCD-EFGH-JKMN");
    expect(codeFromHash("#code=abcd%20efgh%20jkmn")).toBe("ABCD-EFGH-JKMN");
    for (const bad of ["", "#", "#code=", "#code=short", "#code=ABCD-EFGH-JKM0", "#other=ABCD-EFGH-JKMN"]) {
      expect(codeFromHash(bad)).toBe("");
    }
  });

  it("shows the code, how to accept it and what accepting means", async () => {
    await render("#code=abcd-efgh-jkmn");
    expect(host.querySelector('[data-testid="invite-code"]')?.textContent).toBe("ABCD-EFGH-JKMN");
    const text = host.textContent ?? "";
    expect(text).toContain("Nothing happens until you accept the invite in Potluck.");
    expect(text).toContain("potluck circle join ABCD-EFGH-JKMN");
    expect(text).toContain("The machine that answers sees the conversation.");
    expect(host.querySelector('a[href="/install"]')).not.toBeNull();
  });

  it("says so when the link has no usable code, and shows nothing to paste", async () => {
    await render("#code=nope");
    expect(host.textContent).toContain("This invite link is incomplete");
    expect(host.querySelector('[data-testid="invite-code"]')).toBeNull();
  });
});
