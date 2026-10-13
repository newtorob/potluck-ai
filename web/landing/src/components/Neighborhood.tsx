"use client";

import Image from "next/image";
import Link from "next/link";
import { useEffect, useRef, useState } from "react";
import { RELEASE_VERSION, LINUX_RELEASE_VERSION, WINDOWS_RELEASE_VERSION } from "@/lib/downloads";
import "@/styles/neighborhood.css";

type Machine = "laptop" | "desktop" | "server";
type Phase = "idle" | "sending" | "working" | "returning" | "complete";
type Example = "write" | "think" | "code";

const MACHINES: Record<
  Machine,
  { name: string; short: string; note: string; route: string }
> = {
  laptop: {
    name: "This laptop",
    short: "laptop",
    note: "A little work, right here.",
    route: "Stays on this laptop",
  },
  desktop: {
    name: "Your desktop",
    short: "desktop",
    note: "Let the desktop do the lifting.",
    route: "Laptop → your desktop → laptop",
  },
  server: {
    name: "Your home server",
    short: "home server",
    note: "That little box has a new job.",
    route: "Laptop → your home server → laptop",
  },
};
const EXAMPLES: Record<
  Example,
  { label: string; prompt: string; answer: string }
> = {
  write: {
    label: "Write",
    prompt: "Write an invitation to a neighborhood potluck.",
    answer:
      "Bring a dish, a friend, or just yourself. There’s a seat for you at our table.",
  },
  think: {
    label: "Think",
    prompt: "What could I do with my old computer?",
    answer:
      "Give it a second act: a home library, a tiny server, or a place to run a local AI model.",
  },
  code: {
    label: "Code",
    prompt: "Write a little Python hello for everyone.",
    answer:
      'for neighbor in ["you", "me", "everyone"]:\n    print(f"There’s a place for {neighbor}.")',
  },
};
// Coordinates belong to the 1536×1024 illustration. Paths follow its garden.
const ROUTES: Record<Exclude<Machine, "laptop">, string> = {
  desktop:
    "M 356 591 C 435 715 455 812 576 799 C 830 765 951 649 791 518 C 704 447 761 430 867 319",
  server:
    "M 356 591 C 435 715 455 812 576 799 C 747 790 899 705 1016 735 C 1130 769 1161 719 1230 636",
};

function Arrow({ direction = "right" }: { direction?: "right" | "up" }) {
  return (
    <svg
      width="18"
      height="18"
      viewBox="0 0 24 24"
      fill="none"
      aria-hidden="true"
    >
      <path
        d={
          direction === "up" ? "M6 18 18 6M6 6h12v12" : "M4 12h15m-6-6 6 6-6 6"
        }
        stroke="currentColor"
        strokeWidth="1.5"
        strokeLinecap="round"
        strokeLinejoin="round"
      />
    </svg>
  );
}

export function Neighborhood() {
  const [machine, setMachine] = useState<Machine>("desktop");
  const [example, setExample] = useState<Example>("write");
  const [phase, setPhase] = useState<Phase>("idle");
  const [run, setRun] = useState(0);
  const [paused, setPaused] = useState(false);
  const [reduced, setReduced] = useState(false);
  const [visible, setVisible] = useState(true);
  const [cat, setCat] = useState(false);
  const [artFailed, setArtFailed] = useState(false);
  const section = useRef<HTMLElement>(null);
  const timers = useRef<ReturnType<typeof setTimeout>[]>([]);
  const catTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const busy = phase !== "idle" && phase !== "complete";
  const quiet = paused || reduced || !visible;
  const selected = MACHINES[machine];
  const sample = EXAMPLES[example];

  function clearRun() {
    timers.current.forEach(clearTimeout);
    timers.current = [];
  }

  useEffect(() => {
    const preference = window.matchMedia("(prefers-reduced-motion: reduce)");
    const update = () => setReduced(preference.matches);
    update();
    preference.addEventListener("change", update);
    let inView = true;
    const visibility = () => setVisible(inView && !document.hidden);
    const observer = new IntersectionObserver(([entry]) => {
      inView = Boolean(entry?.isIntersecting);
      visibility();
    });
    if (section.current) observer.observe(section.current);
    document.addEventListener("visibilitychange", visibility);
    return () => {
      preference.removeEventListener("change", update);
      observer.disconnect();
      document.removeEventListener("visibilitychange", visibility);
      clearRun();
      if (catTimer.current) clearTimeout(catTimer.current);
    };
  }, []);

  function chooseMachine(next: Machine) {
    clearRun();
    setMachine(next);
    setPhase("idle");
  }

  function chooseExample(next: Example) {
    clearRun();
    setExample(next);
    setPhase("idle");
  }

  function sendWork() {
    clearRun();
    setRun((value) => value + 1);
    // Reduced motion and the pause control provide the same result without
    // making a visitor wait through an animation they chose not to watch.
    if (quiet) {
      setPhase("complete");
      return;
    }
    if (machine === "laptop") {
      setPhase("working");
      timers.current = [setTimeout(() => setPhase("complete"), 1300)];
    } else {
      setPhase("sending");
      timers.current = [
        setTimeout(() => setPhase("working"), 850),
        setTimeout(() => setPhase("returning"), 2000),
        setTimeout(() => setPhase("complete"), 2850),
      ];
    }
  }

  function petCat() {
    setCat(true);
    if (catTimer.current) clearTimeout(catTimer.current);
    catTimer.current = setTimeout(() => setCat(false), 3500);
  }

  const status =
    phase === "sending"
      ? `Your request is on its way to your ${selected.short}.`
      : phase === "working"
        ? `The model on ${machine === "laptop" ? "this" : "your"} ${selected.short} is answering.`
        : phase === "returning"
          ? "A little something, on its way back."
          : phase === "complete"
            ? `Answered on ${machine === "laptop" ? "this" : "your"} ${selected.short}. Back with you.`
            : "Pick a machine. Send a little work.";

  return (
    <section
      ref={section}
      className={`neighborhood ${quiet ? "hood-quiet" : ""}`}
      aria-labelledby="home-title"
    >
      <div className="hood-hero">
        <div className="hood-copy">
          <Link href="/install" className="hood-release">
            <span className="hood-release-dot" /> Linux GPU support is
            here <Arrow />
          </Link>
          <h1 id="home-title">
            Good machines.
            <br />
            <em>Better together.</em>
          </h1>
          <p className="hood-description">
            Your laptop. The gaming PC upstairs.
            <br className="hood-desktop-break" /> That little server with a
            second life.
          </p>
          <p className="hood-description secondary">
            Run open models on the hardware you own. Potluck connects your
            machines, so you choose where the work happens.
          </p>
          <div className="hood-actions">
            <Link href="/install" className="hood-download">
              Download Potluck <Arrow />
            </Link>
            <Link href="/blog/run-a-local-llm-on-another-computer" className="hood-film-link">
              Set up a second computer <Arrow />
            </Link>
          </div>
          <p className="hood-availability">
            Free to run locally. Sign-in required.
            <br />
            <span>
              Mac {RELEASE_VERSION} · Linux {LINUX_RELEASE_VERSION} · Windows{" "}
              {WINDOWS_RELEASE_VERSION}
            </span>
          </p>
        </div>

        <div className="hood-world">
          <div className="hood-world-caption">
            <span>A little world of your own</span>
            <svg
              width="47"
              height="30"
              viewBox="0 0 47 30"
              fill="none"
              aria-hidden="true"
            >
              <path
                d="M2 2c27-2 17 25 42 22m-9-6 9 6-9 4"
                stroke="currentColor"
                strokeWidth="1.2"
              />
            </svg>
          </div>
          <button
            className="hood-motion"
            type="button"
            aria-label={paused ? "Play scene motion" : "Pause scene motion"}
            aria-pressed={paused}
            onClick={() => setPaused((value) => !value)}
          >
            {paused ? (
              <svg
                width="14"
                height="14"
                viewBox="0 0 14 14"
                aria-hidden="true"
              >
                <path d="m4 2 8 5-8 5Z" fill="currentColor" />
              </svg>
            ) : (
              <svg
                width="14"
                height="14"
                viewBox="0 0 14 14"
                aria-hidden="true"
              >
                <path
                  d="M4 2v10M10 2v10"
                  stroke="currentColor"
                  strokeWidth="2"
                />
              </svg>
            )}
          </button>
          <div
            className={`hood-scene ${artFailed ? "hood-art-failed" : ""}`}
            data-machine={machine}
            data-phase={phase}
          >
            <Image
              className="hood-art"
              src="/images/potluck-neighborhood.png"
              alt="Three tiny garden workrooms: a laptop study, a gaming desktop studio, and a home server workshop, linked by a winding path."
              width={1536}
              height={1024}
              sizes="(max-width: 700px) 100vw, (max-width: 1100px) 85vw, 900px"
              priority
              fetchPriority="high"
              quality={85}
              onError={() => setArtFailed(true)}
            />
            {artFailed && (
              <p className="hood-art-fallback">
                The neighborhood illustration couldn’t load.
                <br />
                You can still try the demo below.
              </p>
            )}
            <svg
              className="hood-life"
              viewBox="0 0 1536 1024"
              fill="none"
              aria-hidden="true"
            >
              <defs>
                <radialGradient id="hood-window-glow">
                  <stop stopColor="#f3c780" stopOpacity=".6" />
                  <stop offset="1" stopColor="#e8a566" stopOpacity="0" />
                </radialGradient>
              </defs>
              <g
                className="hood-chimney"
                stroke="#a58d72"
                strokeWidth="4"
                strokeLinecap="round"
              >
                <path
                  className="hood-smoke hood-smoke-one"
                  d="M259 270c-15-20 18-24 1-49"
                />
                <path
                  className="hood-smoke hood-smoke-two"
                  d="M259 270c-15-20 18-24 1-49"
                />
                <path
                  className="hood-smoke hood-smoke-three"
                  d="M259 270c-15-20 18-24 1-49"
                />
              </g>
              <g
                className="hood-birds"
                stroke="#625c42"
                strokeWidth="3.5"
                strokeLinecap="round"
              >
                <path d="M185 149q12-9 23 2 11-10 23-4" />
                <path d="M245 121q9-7 18 2 8-8 17-3" />
              </g>
              <g
                className={`hood-screen hood-screen-laptop ${machine === "laptop" ? "chosen" : ""}`}
              >
                <ellipse
                  cx="355"
                  cy="588"
                  rx="57"
                  ry="42"
                  fill="url(#hood-window-glow)"
                />
                <path
                  d="m333 574 40-6 6 30-39 7Z"
                  fill="#adc3a5"
                  fillOpacity=".8"
                />
                <path
                  className="hood-screen-type"
                  d="m342 581 23-3m-22 10 16-2m-15 9 21-3"
                  stroke="#31513f"
                  strokeWidth="2"
                />
              </g>
              <g
                className={`hood-screen hood-screen-desktop ${machine === "desktop" ? "chosen" : ""}`}
              >
                <ellipse
                  cx="881"
                  cy="298"
                  rx="88"
                  ry="64"
                  fill="url(#hood-window-glow)"
                />
                <path
                  d="m846 273 71 8v37l-71-7Z"
                  fill="#eec681"
                  fillOpacity=".65"
                />
                <path
                  className="hood-screen-type"
                  d="m856 285 46 4m-46 6 32 3m-32 6 45 4"
                  stroke="#4e6145"
                  strokeWidth="3"
                />
              </g>
              <g className="hood-fan" transform="translate(965 307)">
                <g className="hood-fan-spin" stroke="#e6bc71" strokeWidth="2">
                  <ellipse rx="7" ry="10" />
                  <path d="M0-8v16m-5-13 10 10m-10 0 10-10" />
                </g>
              </g>
              <g
                className={`hood-server-lights ${machine === "server" ? "chosen" : ""}`}
                fill="#ffd893"
              >
                {[606, 620, 634, 647, 661, 675, 688].map((y, index) => (
                  <rect
                    key={y}
                    x="1227"
                    y={y}
                    width="5"
                    height="6"
                    rx="1"
                    style={{ animationDelay: `${index * -0.31}s` }}
                  />
                ))}
              </g>
              {machine !== "laptop" && (
                <g className="hood-route">
                  <path d={ROUTES[machine]} className="hood-route-base" />
                  {(phase === "sending" || phase === "returning") && (
                    <path
                      key={`${run}-${phase}`}
                      d={ROUTES[machine]}
                      pathLength="1"
                      className={`hood-courier ${phase === "returning" ? "returning" : ""}`}
                    />
                  )}
                </g>
              )}
              {phase === "working" && (
                <circle
                  className="hood-working-ring"
                  cx={
                    machine === "laptop"
                      ? 356
                      : machine === "desktop"
                        ? 883
                        : 1230
                  }
                  cy={
                    machine === "laptop"
                      ? 589
                      : machine === "desktop"
                        ? 297
                        : 636
                  }
                  r="32"
                />
              )}
            </svg>
            <div
              className="hood-pins"
              role="group"
              aria-label="Choose the machine for this illustrative demo"
            >
              {(Object.keys(MACHINES) as Machine[]).map((id, index) => (
                <button
                  key={id}
                  type="button"
                  className={`hood-pin hood-pin-${id} ${machine === id ? "selected" : ""}`}
                  aria-pressed={machine === id}
                  onClick={() => chooseMachine(id)}
                >
                  <span className="hood-pin-number" aria-hidden="true">
                    0{index + 1}
                  </span>
                  <span>{MACHINES[id].name}</span>
                  <span className="hood-pin-light" aria-hidden="true" />
                </button>
              ))}
            </div>
            <button
              type="button"
              className="hood-cat"
              aria-label="Say hello to the neighborhood cat"
              onClick={petCat}
            >
              <span className="hood-cat-hint" aria-hidden="true">
                pspsps…
              </span>
            </button>
            {cat && (
              <div className="hood-cat-message" role="status">
                <span aria-hidden="true">♡</span> Head of taking breaks.
              </div>
            )}
            {phase !== "idle" && (
              <div
                className={`hood-scene-message ${phase === "complete" ? "complete" : ""}`}
                aria-hidden="true"
              >
                <span>
                  {phase === "complete"
                    ? "A little something, back with you."
                    : status}
                </span>
                {phase === "complete" && (
                  <p>
                    {example === "code"
                      ? "A little Python hello. See the example below. ↓"
                      : sample.answer}
                  </p>
                )}
              </div>
            )}
          </div>
          <div className="hood-world-note">
            <button
              type="button"
              className="hood-scene-send"
              onClick={sendWork}
              disabled={busy}
              aria-label="Send a little work through the neighborhood"
            >
              {busy
                ? "A little work in motion…"
                : phase === "complete"
                  ? "Try another little job"
                  : "Send a little work"}
              <Arrow />
            </button>
            <span>Pick a machine above. See what happens.</span>
          </div>
        </div>
      </div>

      <div className="hood-demo-wrap">
        <div
          className="hood-demo"
          aria-label="Try an illustrative Potluck request"
        >
          <div className="hood-demo-prompt">
            <div className="hood-demo-heading">
              <span className="hood-step">01 / A LITTLE IDEA</span>
              <div
                className="hood-examples"
                role="group"
                aria-label="Choose an example request"
              >
                {(Object.keys(EXAMPLES) as Example[]).map((id) => (
                  <button
                    key={id}
                    type="button"
                    aria-pressed={example === id}
                    onClick={() => chooseExample(id)}
                  >
                    {EXAMPLES[id].label}
                  </button>
                ))}
              </div>
            </div>
            <p className="hood-prompt">{sample.prompt}</p>
          </div>
          <div className="hood-demo-send">
            <span className="hood-step">02 / YOUR CHOICE</span>
            <p>{selected.name}</p>
            <button
              className="hood-send"
              type="button"
              onClick={sendWork}
              disabled={busy}
            >
              {busy
                ? "A little work in motion"
                : phase === "complete"
                  ? "Send another little job"
                  : "Send a little work"}
              {busy ? (
                <span className="hood-working-dots" aria-hidden="true">
                  <i />
                  <i />
                  <i />
                </span>
              ) : (
                <Arrow />
              )}
            </button>
          </div>
          <div
            className={`hood-demo-answer ${phase === "complete" ? "answered" : ""}`}
          >
            <span className="hood-step">03 / BACK TO YOU</span>
            <div
              className="hood-answer-live"
              role="status"
              aria-live="polite"
              aria-atomic="true"
            >
              {phase === "complete" ? (
                <p
                  className={
                    example === "code" ? "hood-answer code" : "hood-answer"
                  }
                >
                  {sample.answer}
                </p>
              ) : (
                <p className="hood-answer-placeholder">{status}</p>
              )}
              <p className="hood-route-caption">
                {phase === "complete" ? status : selected.route}
              </p>
            </div>
          </div>
        </div>
        <p className="hood-disclosure">
          A tiny illustrated demo, with example answers. In Potluck, paired
          machines must be reachable and have a model ready.
        </p>
      </div>
      <div className="hood-bottom-line">
        <span>Start with one machine.</span>
        <span>Bring the others along.</span>
        <span>Keep the choice yours.</span>
      </div>
    </section>
  );
}
