// @vitest-environment jsdom
import { act, createElement } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { DeviceClient } from "./device-client";

type Call = { url: string; init: RequestInit; body: Record<string, unknown> | undefined };

let host: HTMLDivElement;
let root: Root;
let calls: Call[];
let routes: Record<string, (body?: Record<string, unknown>) => [number, object | null]>;

function respond(status: number, body: object | null) {
  return {
    ok: status >= 200 && status < 300,
    status,
    json: async () => body ?? {},
  } as Response;
}

async function settle() {
  for (let i = 0; i < 5; i += 1) {
    await act(async () => {
      await Promise.resolve();
    });
  }
}

async function render(path: string) {
  window.history.replaceState(null, "", path);
  await act(async () => {
    root.render(createElement(DeviceClient));
  });
  await settle();
}

function button(label: string): HTMLButtonElement {
  const found = [...host.querySelectorAll("button")].find((b) => b.textContent?.trim().startsWith(label));
  if (!found) throw new Error(`no button "${label}" in: ${host.textContent}`);
  return found as HTMLButtonElement;
}

function type(input: HTMLInputElement, value: string) {
  const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value")!.set!;
  setter.call(input, value);
  input.dispatchEvent(new Event("input", { bubbles: true }));
}

async function click(label: string) {
  await act(async () => {
    button(label).click();
  });
  await settle();
}

function paths() {
  return calls.map((c) => c.url.replace("https://api.trypotluck.ai", ""));
}

beforeEach(() => {
  (globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
  host = document.createElement("div");
  document.body.append(host);
  root = createRoot(host);
  calls = [];
  localStorage.clear();
  routes = {
    "/auth/device/session": () => [200, { signedIn: false }],
    "/auth/device/request-link": () => [202, { status: "sent" }],
    "/auth/device/review": () => [200, { email: "rob@example.com" }],
    "/auth/device/approve": (b) => [200, { status: b?.approved ? "approved" : "denied" }],
    "/auth/device/approve-session": (b) => [200, { status: b?.approved ? "approved" : "denied" }],
    "/auth/logout": () => [204, null],
  };
  vi.stubGlobal("fetch", vi.fn(async (url: string, init: RequestInit) => {
    const body = init.body ? JSON.parse(String(init.body)) : undefined;
    calls.push({ url, init, body });
    const route = routes[url.replace("https://api.trypotluck.ai", "")];
    if (!route) throw new TypeError("unexpected " + url);
    const [status, payload] = route(body);
    return respond(status, payload);
  }));
});

afterEach(async () => {
  await act(async () => root.unmount());
  host.remove();
  vi.unstubAllGlobals();
});

describe("DeviceClient", () => {
  it("approves a linked code in one click when this browser is signed in", async () => {
    routes["/auth/device/session"] = () => [200, { signedIn: true, email: "rob@example.com" }];
    await render("/auth/device?code=bcdfghjk");

    expect(host.textContent).toContain("Approve this terminal");
    expect(host.textContent).toContain("rob@example.com");
    expect(host.querySelector('[data-testid="device-code"]')?.textContent).toBe("BCDF-GHJK");
    expect(host.textContent).toContain("Only approve if you just ran potluck login");

    await click("Approve");

    const approval = calls.find((c) => c.url.endsWith("/auth/device/approve-session"))!;
    expect(approval.body).toEqual({ user_code: "BCDF-GHJK", approved: true });
    expect(approval.init.credentials).toBe("include");
    expect(host.textContent).toContain("Your terminal is connected");
    expect(paths()).not.toContain("/auth/device/request-link");
  });

  it("declines in one click", async () => {
    routes["/auth/device/session"] = () => [200, { signedIn: true, email: "rob@example.com" }];
    await render("/auth/device?code=BCDF-GHJK");
    await click("Decline");
    expect(calls.at(-1)!.body).toEqual({ user_code: "BCDF-GHJK", approved: false });
    expect(host.textContent).toContain("Request declined");
  });

  it("keeps the code across the email round trip in the same browser", async () => {
    await render("/auth/device?code=BCDF-GHJK");
    expect(host.textContent).toContain("Connect your terminal");
    type(host.querySelector<HTMLInputElement>("#device-email")!, "rob@example.com");
    await click("Send approval email");
    expect(calls.find((c) => c.url.endsWith("/request-link"))!.body).toEqual({ user_code: "BCDF-GHJK", email: "rob@example.com" });
    expect(host.textContent).toContain("Check your email");

    // The emailed link opens a fresh page in the same browser.
    await act(async () => root.unmount());
    root = createRoot(host);
    await render("/auth/device#token=proof-1");

    expect(window.location.hash).toBe("");
    expect(host.textContent).toContain("Approve your terminal");
    expect(host.querySelector('[data-testid="device-code"]')?.textContent).toBe("BCDF-GHJK");
    await click("Approve sign-in");
    expect(calls.find((c) => c.url.endsWith("/auth/device/approve"))!.body).toEqual({
      token: "proof-1", user_code: "BCDF-GHJK", approved: true, remember: true,
    });
    expect(host.textContent).toContain("Your terminal is connected");
    expect(localStorage.getItem("potluck.device.pending")).toBeNull();
  });

  it("asks for the code when the emailed link opens in another browser", async () => {
    await render("/auth/device#token=proof-2");
    const input = host.querySelector<HTMLInputElement>("#device-code");
    expect(input).not.toBeNull();
    expect(input!.value).toBe("");
  });

  it("can approve by email without remembering the browser", async () => {
    localStorage.setItem("potluck.device.pending", JSON.stringify({ code: "BCDF-GHJK", at: Date.now() }));
    await render("/auth/device#token=proof-3");
    await act(async () => {
      host.querySelector<HTMLInputElement>('input[type="checkbox"]')!.click();
    });
    await click("Approve sign-in");
    expect(calls.find((c) => c.url.endsWith("/auth/device/approve"))!.body).toMatchObject({ remember: false });
  });

  it("ignores a stale saved code", async () => {
    localStorage.setItem("potluck.device.pending", JSON.stringify({ code: "BCDF-GHJK", at: Date.now() - 16 * 60 * 1000 }));
    await render("/auth/device#token=proof-4");
    expect(host.querySelector<HTMLInputElement>("#device-code")!.value).toBe("");
  });

  it("falls back to email when the browser session has ended", async () => {
    routes["/auth/device/session"] = () => [200, { signedIn: true, email: "rob@example.com" }];
    routes["/auth/device/approve-session"] = () => [401, { error: "not_signed_in" }];
    await render("/auth/device?code=BCDF-GHJK");
    await click("Approve");
    expect(host.textContent).toContain("Connect your terminal");
    expect(host.textContent).toContain("no longer signed in");
    expect(host.querySelector("#device-email")).not.toBeNull();
  });

  it("signs this browser out", async () => {
    routes["/auth/device/session"] = () => [200, { signedIn: true, email: "rob@example.com" }];
    await render("/auth/device?code=BCDF-GHJK");
    await click("Not rob@example.com?");
    const logout = calls.find((c) => c.url.endsWith("/auth/logout"))!;
    expect(logout.init.method).toBe("POST");
    expect(logout.init.credentials).toBe("include");
    expect(host.textContent).toContain("Connect your terminal");
  });

  it("treats an unreachable session check as signed out", async () => {
    routes["/auth/device/session"] = () => {
      throw new TypeError("network down");
    };
    await render("/auth/device");
    expect(host.textContent).toContain("Connect your terminal");
    expect(host.querySelector("#device-code")).not.toBeNull();
  });
});
