'use client';

/**
 * Shared motion primitives for the Living Mesh site.
 *
 * Both components are client components but are safe to compose from server
 * pages (children pass through as serialized RSC payload). Both respect
 * prefers-reduced-motion: Reveal falls back to always-visible via CSS
 * (globals.css), CountUp renders the final value immediately.
 */

import { useState, useEffect, useRef } from 'react';

// ─── Reveal — scroll-triggered entrance, once per element ────────────────────

export function Reveal({
  children,
  delay = 0,
  className,
}: {
  children: React.ReactNode;
  delay?: number;
  className?: string;
}) {
  const ref = useRef<HTMLDivElement | null>(null);
  const [inView, setInView] = useState(false);

  useEffect(() => {
    const el = ref.current;
    if (!el) return;
    const io = new IntersectionObserver(
      (entries) => {
        if (entries[0]?.isIntersecting) {
          setInView(true);
          io.disconnect();
        }
      },
      { threshold: 0.12, rootMargin: '0px 0px -36px 0px' }
    );
    io.observe(el);
    return () => io.disconnect();
  }, []);

  return (
    <div
      ref={ref}
      className={`lm-reveal${inView ? ' is-in' : ''}${className ? ` ${className}` : ''}`}
      style={delay ? { transitionDelay: `${delay}ms` } : undefined}
    >
      {children}
    </div>
  );
}

// ─── CountUp — numerals ease from 0 when they enter the viewport ─────────────

export function CountUp({
  target,
  decimals = 0,
  duration = 1300,
}: {
  target: number;
  decimals?: number;
  duration?: number;
}) {
  // Initialize at the final value so prerendered HTML and no-JS readers see
  // the real number; the animation replaces it only once the stat is visible.
  const [display, setDisplay] = useState(() => target.toFixed(decimals));
  const ref = useRef<HTMLSpanElement | null>(null);

  useEffect(() => {
    const el = ref.current;
    if (!el) return;
    if (window.matchMedia('(prefers-reduced-motion: reduce)').matches) return;
    let raf = 0;
    const io = new IntersectionObserver(
      (entries) => {
        if (!entries[0]?.isIntersecting) return;
        io.disconnect();
        const start = performance.now();
        const tick = (now: number) => {
          const p = Math.min((now - start) / duration, 1);
          const eased = 1 - Math.pow(1 - p, 3);
          setDisplay((target * eased).toFixed(decimals));
          if (p < 1) raf = requestAnimationFrame(tick);
        };
        raf = requestAnimationFrame(tick);
      },
      { threshold: 0.4 }
    );
    io.observe(el);
    return () => {
      io.disconnect();
      cancelAnimationFrame(raf);
    };
  }, [target, decimals, duration]);

  return <span ref={ref}>{display}</span>;
}
