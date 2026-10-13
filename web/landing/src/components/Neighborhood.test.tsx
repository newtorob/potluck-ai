// @vitest-environment jsdom
import {
  act,
  createElement,
  type ImgHTMLAttributes,
  type AnchorHTMLAttributes,
} from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { Neighborhood } from "./Neighborhood";

vi.mock("next/image", () => ({
  default: ({
    priority: _priority,
    quality: _quality,
    ...props
  }: ImgHTMLAttributes<HTMLImageElement> & {
    priority?: boolean;
    quality?: number;
  }) => createElement("img", props),
}));
vi.mock("next/link", () => ({
  default: (props: AnchorHTMLAttributes<HTMLAnchorElement>) =>
    createElement("a", props),
}));

let host: HTMLDivElement;
let root: Root;
let reduceMotion = false;

beforeEach(() => {
  vi.useFakeTimers();
  reduceMotion = false;
  vi.stubGlobal("IS_REACT_ACT_ENVIRONMENT", true);
  vi.stubGlobal("matchMedia", () => ({
    matches: reduceMotion,
    addEventListener: vi.fn(),
    removeEventListener: vi.fn(),
  }));
  vi.stubGlobal(
    "IntersectionObserver",
    class {
      observe() {}
      disconnect() {}
    },
  );
  host = document.createElement("div");
  document.body.appendChild(host);
  root = createRoot(host);
});
afterEach(() => {
  act(() => root.unmount());
  host.remove();
  vi.useRealTimers();
  vi.unstubAllGlobals();
});

function render() {
  act(() => root.render(<Neighborhood />));
}
function click(selector: string) {
  const button = host.querySelector<HTMLButtonElement>(selector);
  expect(button, selector).not.toBeNull();
  act(() => button!.click());
}
function advance(ms: number) {
  act(() => vi.advanceTimersByTime(ms));
}
function phase() {
  return host.querySelector(".hood-scene")?.getAttribute("data-phase");
}

describe("illustrative neighborhood requests", () => {
  it("shows the request, model work and returned answer for the chosen machine", () => {
    render();
    click(".hood-pin-server");
    click(".hood-examples button:nth-child(3)");
    click(".hood-scene-send");
    expect(phase()).toBe("sending");
    expect(host.querySelector<HTMLButtonElement>(".hood-send")!.disabled).toBe(
      true,
    );
    advance(850);
    expect(phase()).toBe("working");
    advance(1150);
    expect(phase()).toBe("returning");
    advance(850);
    expect(phase()).toBe("complete");
    expect(host.querySelector(".hood-answer")?.textContent).toContain(
      "for neighbor in",
    );
    expect(host.querySelector(".hood-answer-live")?.textContent).toContain(
      "Answered on your home server",
    );
    expect(host.querySelector<HTMLButtonElement>(".hood-send")!.disabled).toBe(
      false,
    );
  });

  it("changing the destination or example cancels the previous pending result", () => {
    render();
    click(".hood-send");
    advance(1000);
    click(".hood-pin-laptop");
    advance(4000);
    expect(phase()).toBe("idle");
    expect(host.querySelector(".hood-answer")).toBeNull();
    click(".hood-send");
    expect(phase()).toBe("working");
    expect(host.querySelector(".hood-route")).toBeNull();
    click(".hood-examples button:nth-child(2)");
    advance(4000);
    expect(phase()).toBe("idle");
    click(".hood-send");
    advance(1300);
    expect(host.querySelector(".hood-answer")?.textContent).toContain(
      "second act",
    );
    expect(host.querySelector(".hood-answer-live")?.textContent).toContain(
      "Answered on this laptop",
    );
  });

  it("honors reduced motion without hiding or delaying the result", () => {
    reduceMotion = true;
    render();
    expect(host.querySelector(".hood-quiet")).not.toBeNull();
    click(".hood-send");
    expect(phase()).toBe("complete");
    expect(vi.getTimerCount()).toBe(0);
    expect(host.querySelector(".hood-courier")).toBeNull();
    expect(host.querySelector(".hood-answer")?.textContent).toContain(
      "Bring a dish",
    );
  });

  it("lets visitors pause motion and still use the illustration controls", () => {
    render();
    click(".hood-motion");
    expect(
      host.querySelector(".hood-motion")?.getAttribute("aria-pressed"),
    ).toBe("true");
    click(".hood-pin-server");
    click(".hood-send");
    expect(phase()).toBe("complete");
    expect(host.querySelector(".hood-answer-live")?.textContent).toContain(
      "your home server",
    );
    click(".hood-motion");
    click(".hood-send");
    expect(phase()).toBe("sending");
  });

  it("cleans up request and cat timers when navigating away", () => {
    render();
    click(".hood-cat");
    click(".hood-send");
    expect(vi.getTimerCount()).toBeGreaterThan(0);
    act(() => root.render(null));
    expect(vi.getTimerCount()).toBe(0);
  });
});
