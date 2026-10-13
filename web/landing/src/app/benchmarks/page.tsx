import type { Metadata } from 'next';
import Link from 'next/link';
import { Reveal, CountUp } from '@/components/motion';

export const metadata: Metadata = {
  title: 'Benchmarks',
  description:
    'Honest numbers across memory retrieval and local LLM inference, on Mac Metal, Linux CUDA, and Windows Vulkan, including the unfavorable ones.',
  openGraph: {
    title:
      'Potluck benchmarks: memory retrieval + local inference, including the unfavorable numbers',
    description:
      'LongMemEval oracle results, inference throughput across four models and three platforms, the 70B local-only ceiling, and the known limits we are not hiding.',
  },
};

// --- Data ---------------------------------------------------------------

const ACCURACY = [
  {
    metric: 'recall_any@10',
    value: '1.00',
    note: 'Structurally guaranteed on the oracle split (see note)',
  },
  { metric: 'recall_all@10', value: '1.00', note: 'Structurally guaranteed on the oracle split' },
  {
    metric: 'recall_all@1',
    value: '0.352',
    note: 'Meaningful ranking signal across the 324 multi-session instances',
  },
  { metric: 'nDCG@10', value: '1.00', note: 'Same structural bound as recall_any@10' },
  {
    metric: 'Errors',
    value: '0 / 500 on every machine',
    note: 'Identical accuracy across all three runs',
  },
] as const;

const HARDWARE = [
  {
    label: 'Mac M2 Pro',
    spec: '16 GB unified memory · macOS · Metal backend',
    p50: '10.6 ms',
    p95: '35.0 ms',
    wall: '44.8 s',
  },
  {
    label: 'Linux media server',
    spec: 'i7-13700K · 62 GB · RTX 2080 Ti · CUDA 13.0 (driver 580)',
    p50: '17.8 ms',
    p95: '20.8 ms',
    wall: '64.6 s',
  },
  {
    label: 'Windows workstation',
    spec: 'Ryzen 7 5800X · 64 GB · RX 6600 XT (CPU fallback, ROCm not configured)',
    p50: '18.9 ms',
    p95: '21.7 ms',
    wall: '141.4 s',
  },
] as const;

const MESH_BASELINE = [
  { link: 'Mac local (baseline, empty query)', p50: '14.8 ms', p95: '18.3 ms', min: '7.6 ms' },
  {
    link: 'Mac → Linux peer (10 memories on peer)',
    p50: '35.9 ms',
    p95: '40.7 ms',
    min: '18.1 ms',
  },
  {
    link: 'Mac → Linux peer (100 memories on peer)',
    p50: '36.1 ms',
    p95: '41.2 ms',
    min: '19.1 ms',
  },
  {
    link: 'Mac → Linux peer (500 memories on peer)',
    p50: '36.3 ms',
    p95: '46.0 ms',
    min: '20.9 ms',
  },
  {
    link: 'Mac → Windows peer (10 memories on peer)',
    p50: '43.7 ms',
    p95: '50.7 ms',
    min: '39.7 ms',
  },
  {
    link: 'Mac → Windows peer (100 memories on peer)',
    p50: '44.9 ms',
    p95: '55.6 ms',
    min: '39.7 ms',
  },
  {
    link: 'Mac → Windows peer (500 memories on peer)',
    p50: '49.3 ms',
    p95: '64.3 ms',
    min: '45.5 ms',
  },
] as const;

const MESH_SCALING = [
  {
    link: 'Mac → Linux peer (1,000 memories on peer)',
    p50: '42.0 ms',
    p95: '51.1 ms',
    min: '21.3 ms',
  },
  {
    link: 'Mac → Linux peer (5,000 memories on peer)',
    p50: '40.6 ms',
    p95: '116.5 ms',
    min: '32.0 ms',
  },
  {
    link: 'Mac → Linux peer (10,000 memories on peer)',
    p50: '57.6 ms',
    p95: '131.9 ms',
    min: '47.2 ms',
  },
] as const;

const MESH_CORRECTNESS = [
  {
    metric: 'recall@1',
    value: '1.00',
    note: 'Top-1 hit on every query. The expected memory was always the first result.',
  },
  {
    metric: 'recall@3',
    value: '1.00',
    note: 'All 10 queries returned the right memory in their top 3.',
  },
  { metric: 'recall@10', value: '1.00', note: 'No misses across 10 distinct semantic queries.' },
] as const;

const BY_TYPE = [
  { type: 'knowledge-update', n: 78, recall: '1.00' },
  { type: 'multi-session', n: 133, recall: '1.00' },
  { type: 'single-session-assistant', n: 56, recall: '1.00' },
  { type: 'single-session-preference', n: 30, recall: '1.00' },
  { type: 'single-session-user', n: 70, recall: '1.00' },
  { type: 'temporal-reasoning', n: 133, recall: '1.00' },
] as const;

const EVAL_B = [
  { metric: 'recall@5 (keyword only)', value: '0.48' },
  { metric: 'recall@5 (RRF, default)', value: '0.72' },
  { metric: 'Lift from RRF', value: '+0.24' },
  { metric: 'Cross-project leak rate', value: '0 / 25 queries' },
] as const;

const INFERENCE_HARDWARE = [
  { label: 'Mac', spec: 'M2 Pro · Metal · 16 GB unified memory' },
  { label: 'Linux', spec: 'i7-13700K + RTX 2080 Ti · CUDA 13 · 11 GB VRAM, 62 GB RAM' },
  { label: 'Windows', spec: 'Ryzen 7 5800X + RX 6600 XT · Vulkan · 8 GB VRAM, 64 GB RAM' },
] as const;

const INFERENCE_RESULTS = [
  { model: 'Phi-3-mini 3.8B Q4_K_M', mac: '19.1', linux: '69.2', windows: '16.2', note: '' },
  { model: 'Qwen 2.5 7B Q4_K_M', mac: '12.4', linux: '45.9', windows: '15.9', note: '' },
  {
    model: 'Llama 3.1 8B Q4_K_M',
    mac: '11.6',
    linux: '43.0',
    windows: '2.1',
    note: 'Windows: the 8B exceeds the 6600 XT 8 GB VRAM at 8K context and spills to CPU.',
  },
  {
    model: 'Llama 3.1 70B Q3_K_S',
    mac: 'won’t fit',
    linux: '2.4',
    windows: 'won’t fit',
    note: 'Linux runs with about 20 of 80 layers on GPU, the rest on CPU, at a 2,048-token context. Unusable for chat.',
  },
] as const;

const GAPS = [
  'MCP latency bench: p50 / p95 from memory_search tool-call to result, against a 500-memory store.',
  'NVIDIA on Linux via CUDA, enabled 2026-06-01 (driver 580 / CUDA 13.0 on the RTX 2080 Ti). Real but modest: p50 22.2 → 17.8 ms (~20%), p95 34.6 → 20.8 ms (~40%). Not the order-of-magnitude win, same dispatch-bound dynamic as AMD, but less severe since CUDA dispatch overhead is lower. Wall time actually went up (35.6 → 64.6 s) because of GPU init / context setup cost amortized across cold per-instance runs; that overhead disappears in a long-lived production sidecar.',
  'AMD on Windows via DirectML, investigated 2026-06-01 (RX 6600 XT). torch-directml runs the bge-small embedder on the GPU with full recall preserved, but p50 is break-even with CPU (17.9 ms vs 18.9 ms). The 33M-parameter model at batch=1 is dispatch-bound, not compute-bound: per-call kernel-dispatch and host↔device transfer roughly equal the CPU forward pass. ROCm/HIP is N/A; gfx1032 is dropped from the current HIP SDK. AMD will become useful again at the inference benchmark, where 7-8B-param models are compute-bound.',
  'Cross-machine peer retrieval shipped 2026-06-02, measured latency + correctness above. Open follow-ups: (a) signed-nonce challenge replacing mesh-IP-as-credential, (b) per-project sharing flags instead of all-or-nothing per-machine opt-in, (c) full latency-at-scale measurements against Windows + bigger store sizes (1K, 5K, 10K memories) to characterize the linear-scan ceiling, (d) graceful federation, a peer-aware retriever that automatically merges results from multiple opted-in peers.',
  'Approximate-nearest-neighbor indexing. The current linear embedding scan degrades past ~10,000 memories.',
  'Publish the Eval B corpus for full methodology transparency.',
  'Inference benchmark next runs: a dedicated long-context bench (8K-32K input tokens), batched-inference throughput, multi-user concurrent serving on the same machine, and Llama 70B at quants we haven’t tested (Q4_K_M, Q5_K_M). The shared-compute story needs the multi-tenant numbers eventually.',
  'Inference hardware extensions: 24 GB + 48 GB cards (e.g. RTX 3090, 4090, A6000) to find the ceiling where a single consumer card runs 70B usably. Apple Silicon higher tiers (M3 Max, M4 Pro) to know the real Mac ceiling.',
] as const;

const STATS = [
  { label: 'P50 · Mac local retrieval', target: 10.6, decimals: 1, suffix: ' ms' },
  { label: 'Errors · oracle split, all machines', target: 0, decimals: 0, suffix: ' / 500' },
  { label: 'Platforms benchmarked', target: 3, decimals: 0, suffix: '' },
  { label: 'recall_all@1 · the meaningful number', target: 0.352, decimals: 3, suffix: '' },
] as const;

// --- Figure wrapper -------------------------------------------------------
// Captioned scientific-table treatment shared by every data table on this
// page. Server-safe, no hooks: local to this file since it isn't reused
// elsewhere yet.

function Figure({
  title,
  date,
  label,
  foot,
  className,
  children,
}: {
  title: string;
  date?: string;
  label: string;
  foot?: React.ReactNode;
  className?: string;
  children: React.ReactNode;
}) {
  return (
    <div
      className={`fig-frame${className ? ` ${className}` : ''}`}
      role="region"
      aria-label={label}
      tabIndex={0}
    >
      <div className="fig-cap">
        <span>{title}</span>
        {date ? <span className="fig-cap-date">{date}</span> : null}
      </div>
      {children}
      {foot ? <div className="fig-foot">{foot}</div> : null}
    </div>
  );
}

// --- Page ---------------------------------------------------------------

export default function BenchmarksPage() {
  return (
    <article className="mx-auto w-full max-w-[1160px] px-6 md:px-10 py-16">
      {/* Page header */}
      <Reveal>
        <div className="mb-10">
          <p className="eyebrow-mono mb-5">Lab report · May–June 2026 measurements</p>
          <h1 className="font-serif text-4xl font-light leading-[1.12] tracking-[-0.02em] text-foreground text-balance max-w-[32ch] md:text-[2.75rem]">
            Honest numbers, including the unfavorable ones.
          </h1>
          <p className="mt-5 max-w-[54ch] font-sans text-base leading-[1.72] text-muted-foreground">
            Publishing numbers is better than not publishing them, even when some are below state of
            the art. This page is the current published run. It is honest about what we measure,
            what we don&apos;t, and where the comparisons are and aren&apos;t fair.
          </p>
        </div>
      </Reveal>

      <p className="docs-note">
        <strong>Historical measurements.</strong> These runs were recorded in May and June 2026 on
        the hardware below. They have not been rerun for desktop 0.1.6. They describe those runs,
        not a guarantee for your hardware or current model catalog.
      </p>

      {/* Headline figures */}
      <Reveal delay={90}>
        <div
          className="mb-14 grid grid-cols-2 gap-px overflow-hidden rounded-lg border border-border bg-border sm:grid-cols-4"
          role="group"
          aria-label="Headline benchmark figures"
        >
          {STATS.map((stat) => (
            <div key={stat.label} className="bg-card px-5 py-5">
              <p className="font-serif text-2xl text-foreground md:text-3xl">
                <CountUp target={stat.target} decimals={stat.decimals} />
                {stat.suffix}
              </p>
              <p className="eyebrow-mono mt-2">{stat.label}</p>
            </div>
          ))}
        </div>
      </Reveal>

      {/* Run context */}
      <Reveal>
        <section className="mt-2 pt-8 border-t border-border">
          <h2 className="font-serif text-xl font-light text-foreground mb-6">Run context</h2>
          <dl className="grid grid-cols-1 gap-y-3 sm:grid-cols-[12rem_1fr] font-sans text-sm">
            <dt className="text-muted-foreground">Benchmark</dt>
            <dd className="text-foreground">
              LongMemEval (ICLR 2025), oracle split, 500 instances
            </dd>

            <dt className="text-muted-foreground">Retriever</dt>
            <dd className="text-foreground">
              <code className="font-mono text-xs">semantic_search()</code> with
              BAAI/bge-small-en-v1.5 (384-dim, asymmetric with BGE query prefix)
            </dd>

            <dt className="text-muted-foreground">Indexing</dt>
            <dd className="text-foreground">
              Each LongMemEval session flattened to user-turn text, embedded as one memory. No
              LLM-based extraction.
            </dd>

            <dt className="text-muted-foreground">Hardware</dt>
            <dd className="text-foreground">
              Mac M2 Pro (16 GB), Linux i7-13700K (62 GB + RTX 2080 Ti), Windows Ryzen 7 5800X (64
              GB + RX 6600 XT). See hardware table below.
            </dd>

            <dt className="text-muted-foreground">Date</dt>
            <dd className="text-foreground">2026-05-31</dd>
          </dl>
        </section>
      </Reveal>

      {/* Oracle split note */}
      <Reveal>
        <section className="mt-12 pt-10 border-t border-border">
          <h2 className="font-serif text-xl font-light text-foreground mb-3">
            About the oracle split
          </h2>
          <div className="max-w-[68ch] font-sans text-sm leading-[1.72] text-muted-foreground space-y-3">
            <p>
              The LongMemEval oracle split only provides the evidence sessions for each question.
              There is no filler noise. Every instance&apos;s haystack equals its answer-session set
              (verified: 500 of 500). Maximum sessions per instance is 6; median is 2; 176 of 500
              instances have exactly 1 session.
            </p>
            <p>
              That structure means{' '}
              <strong className="font-semibold text-foreground">
                recall_any@10 = 1.00 is structurally guaranteed
              </strong>{' '}
              for any retriever that returns all sessions, with k=10 and a maximum corpus of 6, you
              cannot miss. The same applies to nDCG@10.
            </p>
            <p>
              The meaningful number is{' '}
              <strong className="font-semibold text-foreground">recall_all@1 = 0.352</strong>:
              across the 324 multi-session instances, the embedder ranks all required sessions at
              position 1 about 35% of the time. The hard retrieval challenge, filler noise, lives in
              the LongMemEval-S and -M splits, which require pinning a local generation model. That
              run is the priority follow-up.
            </p>
          </div>
        </section>
      </Reveal>

      {/* Accuracy results, same across all 3 machines */}
      <section className="mt-12 pt-10 border-t border-border">
        <Reveal>
          <h2 className="font-serif text-xl font-light text-foreground mb-3">
            Accuracy: LongMemEval oracle
          </h2>
          <p className="max-w-[60ch] mb-6 font-sans text-sm leading-[1.72] text-muted-foreground">
            Embedder behavior is identical across hardware, so the accuracy numbers are the same on
            all three machines. The structurally-bounded scores are listed for transparency, not as
            a flex.
          </p>
        </Reveal>
        <Reveal delay={100}>
          <Figure
            title="Table I · LongMemEval oracle accuracy"
            date="Measured · 2026-05-31"
            label="LongMemEval oracle accuracy results"
            foot={
              <>
                recall_any@10 = 1.00 and nDCG@10 = 1.00 are structurally guaranteed by the oracle
                split&rsquo;s k=10 vs. max-6-session design, not a retrieval win · see &ldquo;About
                the oracle split&rdquo; above.
              </>
            }
          >
            <table className="fact-table w-full">
              <caption className="sr-only">
                LongMemEval oracle accuracy results for the Potluck memory retriever
              </caption>
              <thead>
                <tr>
                  <th scope="col" className="w-56">
                    Metric
                  </th>
                  <th scope="col" className="w-44">
                    Value
                  </th>
                  <th scope="col">Notes</th>
                </tr>
              </thead>
              <tbody>
                {ACCURACY.map((row) => (
                  <tr key={row.metric}>
                    <td className="font-sans text-sm font-medium text-foreground">{row.metric}</td>
                    <td className="font-mono text-sm text-foreground">{row.value}</td>
                    <td className="font-sans text-sm text-muted-foreground leading-relaxed">
                      {row.note}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </Figure>
        </Reveal>
      </section>

      {/* Latency results, by hardware */}
      <section className="mt-12 pt-10 border-t border-border">
        <Reveal>
          <h2 className="font-serif text-xl font-light text-foreground mb-3">
            Latency: by machine
          </h2>
          <p className="max-w-[60ch] mb-6 font-sans text-sm leading-[1.72] text-muted-foreground">
            Same benchmark, same 500 instances, run end-to-end on three different boxes. Both x86
            machines have GPUs available but unused, see the gaps section.
          </p>
        </Reveal>
        <Reveal delay={100}>
          <Figure
            title="Table II · Latency by machine"
            date="Measured · 2026-05-31"
            label="Retrieval latency by machine"
            foot={
              <>
                GPUs on both x86 machines sat idle this run · CUDA (Linux) and DirectML (Windows)
                follow-ups are logged under Known limits below.
              </>
            }
          >
            <table className="fact-table w-full">
              <thead>
                <tr>
                  <th scope="col" className="w-44">
                    Machine
                  </th>
                  <th scope="col">Spec / backend</th>
                  <th scope="col" className="w-24">
                    p50
                  </th>
                  <th scope="col" className="w-24">
                    p95
                  </th>
                  <th scope="col" className="w-28">
                    Wall time
                  </th>
                </tr>
              </thead>
              <tbody>
                {HARDWARE.map((row) => (
                  <tr key={row.label}>
                    <td className="font-sans text-sm font-medium text-foreground">{row.label}</td>
                    <td className="font-sans text-sm text-muted-foreground leading-relaxed">
                      {row.spec}
                    </td>
                    <td className="font-mono text-sm text-foreground">{row.p50}</td>
                    <td className="font-mono text-sm text-foreground">{row.p95}</td>
                    <td className="font-mono text-sm text-muted-foreground">{row.wall}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </Figure>
        </Reveal>
      </section>

      {/* Cross-machine peer retrieval, measured */}
      <section className="mt-12 pt-10 border-t border-border">
        <Reveal>
          <h2 className="font-serif text-xl font-light text-foreground mb-3">
            Cross-machine peer retrieval (measured)
          </h2>
          <div className="max-w-[68ch] mb-6 font-sans text-sm leading-[1.72] text-muted-foreground space-y-3">
            <p>
              The Mac issues a memory query. The Tailscale-managed WireGuard mesh routes it to a
              household peer (Linux or Windows). The peer&apos;s sidecar runs{' '}
              <code className="font-mono text-xs">semantic_search</code> against its own local
              bge-small embedding store. The result returns over the same tunnel. End-to-end latency
              below.
            </p>
            <p>
              The peer endpoint is{' '}
              <code className="font-mono text-xs">POST /peer/memory/search</code>, gated behind
              <code className="font-mono text-xs"> POTLUCK_PEER_MEMORY_ENABLED=1</code> on the
              machine being queried (opt-in, default off). Trust model: mesh-IP reachability is the
              credential. The
              <code className="font-mono text-xs"> peer_access_middleware</code> on the sidecar
              already rejects non-loopback callers from{' '}
              <code className="font-mono text-xs">/memory</code> and all other strictly-local paths;
              <code className="font-mono text-xs"> /peer/*</code> is the only surface peers can
              reach, and within that surface the new endpoint is the only memory path.
            </p>
          </div>
        </Reveal>

        <Reveal delay={100}>
          <Figure
            title="Table III · Cross-machine peer retrieval, baseline"
            date="Measured · 2026-06-02"
            label="Cross-machine peer retrieval baseline latency"
            className="mb-8"
            foot={
              <>
                30 samples per cell, warm embedder · Tailscale-managed WireGuard between peers ·
                baseline row uses an empty store, peer rows populated with the indicated memory
                count.
              </>
            }
          >
            <table className="fact-table w-full">
              <thead>
                <tr>
                  <th scope="col" className="w-72">
                    Path
                  </th>
                  <th scope="col" className="w-24">
                    p50
                  </th>
                  <th scope="col" className="w-24">
                    p95
                  </th>
                  <th scope="col" className="w-24">
                    min
                  </th>
                </tr>
              </thead>
              <tbody>
                {MESH_BASELINE.map((row) => (
                  <tr key={row.link}>
                    <td className="font-sans text-sm font-medium text-foreground">{row.link}</td>
                    <td className="font-mono text-sm text-foreground">{row.p50}</td>
                    <td className="font-mono text-sm text-foreground">{row.p95}</td>
                    <td className="font-mono text-sm text-foreground">{row.min}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </Figure>
        </Reveal>

        <Reveal delay={150}>
          <p className="max-w-[68ch] mb-6 font-sans text-sm leading-[1.72] text-muted-foreground">
            Linux peer p50 stays remarkably flat at ~36 ms as the store grows from 10 to 500
            memories, the embedder cost dominates and the linear scan over a few hundred vectors is
            negligible. Windows peer p50 runs ~8-13 ms higher (~44 ms at small stores, ~49 ms at
            500) because the embedder runs on CPU rather than CUDA. Cross-machine overhead vs Mac
            local: ~21 ms to Linux, ~30 ms to Windows. Day-to-day variance is real: across three
            runs spanning evening and morning, the same Linux probe returned p50s between 35 ms and
            45 ms, tracking Tailscale path conditions. Even at the worst measurement on file,
            cross-machine retrieval feels instant for any interactive workflow. Where the linear
            scan starts to matter is characterized below.
          </p>
        </Reveal>

        <Reveal>
          <h3 className="font-serif text-lg font-light text-foreground mt-10 mb-3">
            Scaling envelope: where the linear scan starts to bite
          </h3>
          <p className="max-w-[68ch] mb-6 font-sans text-sm leading-[1.72] text-muted-foreground">
            The retriever is a brute-force cosine scan over every embedding in the store, no ANN
            index. For typical stores (hundreds to a couple thousand memories) this is fine; the
            embedder cost dominates and the scan is essentially free. The question is where that
            stops being true.
          </p>
        </Reveal>

        <Reveal delay={100}>
          <Figure
            title="Table IV · Mesh scaling envelope"
            date="Measured · 2026-06-02"
            label="Mesh retrieval latency at increasing store sizes"
            className="mb-6"
            foot={
              <>
                Same Linux peer, same query · store populated stepwise at 1,000 / 5,000 / 10,000
                synthetic memories.
              </>
            }
          >
            <table className="fact-table w-full">
              <thead>
                <tr>
                  <th scope="col" className="w-72">
                    Path
                  </th>
                  <th scope="col" className="w-24">
                    p50
                  </th>
                  <th scope="col" className="w-24">
                    p95
                  </th>
                  <th scope="col" className="w-24">
                    min
                  </th>
                </tr>
              </thead>
              <tbody>
                {MESH_SCALING.map((row) => (
                  <tr key={row.link}>
                    <td className="font-sans text-sm font-medium text-foreground">{row.link}</td>
                    <td className="font-mono text-sm text-foreground">{row.p50}</td>
                    <td className="font-mono text-sm text-foreground">{row.p95}</td>
                    <td className="font-mono text-sm text-foreground">{row.min}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </Figure>
        </Reveal>

        <Reveal delay={150}>
          <p className="max-w-[68ch] mb-6 font-sans text-sm leading-[1.72] text-muted-foreground">
            p50 holds at ~40 ms through 5K, then jumps ~15 ms by 10K, the embedder still dominates
            median latency, but the scan is now contributing visibly. p95 is where the linear cost
            shows up first: ~50 ms at 1K doubles to ~115 ms at 5K and ~130 ms at 10K, as the tail
            samples catch the scan doing real work. ANN indexing becomes worth prioritizing
            somewhere between 5K and 10K memories per user. Below that, the brute-force scan is the
            right choice: it&apos;s simpler, deterministic, and free.
          </p>
        </Reveal>

        <Reveal>
          <h3 className="font-serif text-lg font-light text-foreground mt-10 mb-3">
            Correctness: recall preserved across the mesh
          </h3>
          <p className="max-w-[68ch] mb-6 font-sans text-sm leading-[1.72] text-muted-foreground">
            A separate probe verifies the architecture preserves retrieval accuracy. The peer is
            populated with 10 known memories, each semantically distinct, then queried 10 times from
            the Mac via <code className="font-mono text-xs">/peer/memory/search</code> with the
            natural-language question matching each memory. Expected behavior: top-1 is the right
            memory every time.
          </p>
        </Reveal>

        <Reveal delay={100}>
          <Figure
            title="Table V · Mesh correctness"
            date="Measured · 2026-06-02"
            label="Cross-machine retrieval correctness"
            foot={
              <>
                Expected result: semantic_search is deterministic and the wire carries JSON, so a
                clean sweep confirms no silent corruption, no truncation, no reordering · probe
                repeated independently against Linux and Windows peers; both returned recall@1 =
                recall@3 = recall@10 = 1.00 across 10 distinct queries, confirming the architecture
                is symmetric across the mesh.
              </>
            }
          >
            <table className="fact-table w-full">
              <thead>
                <tr>
                  <th scope="col" className="w-40">
                    Metric
                  </th>
                  <th scope="col" className="w-24">
                    Value
                  </th>
                  <th scope="col">Note</th>
                </tr>
              </thead>
              <tbody>
                {MESH_CORRECTNESS.map((row) => (
                  <tr key={row.metric}>
                    <td className="font-sans text-sm font-medium text-foreground">{row.metric}</td>
                    <td className="font-mono text-sm text-foreground">{row.value}</td>
                    <td className="font-sans text-sm text-muted-foreground leading-relaxed">
                      {row.note}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </Figure>
        </Reveal>
      </section>

      {/* By question type */}
      <section className="mt-12 pt-10 border-t border-border">
        <Reveal>
          <h2 className="font-serif text-xl font-light text-foreground mb-3">
            By question type (recall_any@10)
          </h2>
        </Reveal>
        <Reveal delay={100}>
          <Figure
            title="Table VI · Oracle recall by question type"
            date="Measured · 2026-05-31"
            label="Oracle recall by question type"
            foot={
              <>
                Same structural bound as the oracle headline · included for completeness, not as a
                flex.
              </>
            }
          >
            <table className="fact-table w-full">
              <thead>
                <tr>
                  <th scope="col">Question type</th>
                  <th scope="col" className="w-24">
                    n
                  </th>
                  <th scope="col" className="w-32">
                    recall_any@10
                  </th>
                </tr>
              </thead>
              <tbody>
                {BY_TYPE.map((row) => (
                  <tr key={row.type}>
                    <td className="font-sans text-sm font-medium text-foreground">{row.type}</td>
                    <td className="font-mono text-sm text-muted-foreground">{row.n}</td>
                    <td className="font-mono text-sm text-foreground">{row.recall}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </Figure>
        </Reveal>
      </section>

      {/* Eval B */}
      <section className="mt-12 pt-10 border-t border-border">
        <Reveal>
          <h2 className="font-serif text-xl font-light text-foreground mb-3">
            Internal Eval B: Potluck custom corpus
          </h2>
          <p className="max-w-[60ch] mb-6 font-sans text-sm leading-[1.72] text-muted-foreground">
            42 memories, 25 queries. Exercises the actual product retrieval path (RRF
            keyword-semantic fusion, Tier-0 pinned injection, cross-project isolation). Historical
            baseline measured May 28, 2026; not rerun for desktop 0.1.6.
          </p>
        </Reveal>
        <Reveal delay={100}>
          <Figure
            title="Table VII · Eval B custom corpus"
            date="Measured · 2026-05-28"
            label="Internal Eval B retrieval results"
          >
            <table className="fact-table w-full">
              <thead>
                <tr>
                  <th scope="col">Metric</th>
                  <th scope="col" className="w-40">
                    Value
                  </th>
                </tr>
              </thead>
              <tbody>
                {EVAL_B.map((row) => (
                  <tr key={row.metric}>
                    <td className="font-sans text-sm font-medium text-foreground">{row.metric}</td>
                    <td className="font-mono text-sm text-foreground">{row.value}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </Figure>
        </Reveal>
      </section>

      {/* LongMemEval-S, the noise-heavy split */}
      <section className="mt-12 pt-10 border-t border-border">
        <Reveal>
          <h2 className="font-serif text-xl font-light text-foreground mb-3">
            LongMemEval-S: the noise-heavy split
          </h2>
          <div className="max-w-[68ch] mb-6 font-sans text-sm leading-[1.72] text-muted-foreground space-y-3">
            <p>
              The oracle results above are structurally bounded. The S split is where retrieval
              actually has to work. Each of the 500 instances includes ~48 sessions (median; range
              38-62), with the 1-2 gold-evidence sessions hidden among them. We report retrieval
              metrics for this split, not end-to-end answer accuracy.
            </p>
            <p>
              We ran the same retriever (BAAI/bge-small-en-v1.5, 33M params, plain semantic search,
              no graph extraction, no LLM-based memory extraction) against this split on the Mac M2
              Pro.
            </p>
          </div>
        </Reveal>

        <Reveal delay={100}>
          <Figure
            title="Table VIII · LongMemEval-S retrieval quality"
            label="LongMemEval-S retrieval quality results"
          >
            <table className="fact-table w-full">
              <thead>
                <tr>
                  <th scope="col" className="w-56">
                    Metric
                  </th>
                  <th scope="col" className="w-32">
                    Value
                  </th>
                  <th scope="col">Notes</th>
                </tr>
              </thead>
              <tbody>
                <tr>
                  <td className="font-sans text-sm font-medium text-foreground">recall_any@10</td>
                  <td className="font-mono text-sm text-foreground">0.980</td>
                  <td className="font-sans text-sm text-muted-foreground leading-relaxed">
                    At least one gold session in top-10 across 500 instances
                  </td>
                </tr>
                <tr>
                  <td className="font-sans text-sm font-medium text-foreground">recall_all@10</td>
                  <td className="font-mono text-sm text-foreground">0.938</td>
                  <td className="font-sans text-sm text-muted-foreground leading-relaxed">
                    All gold sessions for an instance present in top-10
                  </td>
                </tr>
                <tr>
                  <td className="font-sans text-sm font-medium text-foreground">nDCG@10</td>
                  <td className="font-mono text-sm text-foreground">0.885</td>
                  <td className="font-sans text-sm text-muted-foreground leading-relaxed">
                    Discounted-gain ranking quality
                  </td>
                </tr>
                <tr>
                  <td className="font-sans text-sm font-medium text-foreground">p50 latency</td>
                  <td className="font-mono text-sm text-foreground">12.9 ms</td>
                  <td className="font-sans text-sm text-muted-foreground leading-relaxed">
                    Per retrieval call; slight bump over oracle (48 sessions vs 3)
                  </td>
                </tr>
                <tr>
                  <td className="font-sans text-sm font-medium text-foreground">p95 latency</td>
                  <td className="font-mono text-sm text-foreground">39.8 ms</td>
                  <td className="font-sans text-sm text-muted-foreground leading-relaxed">
                    Tail expands modestly with corpus size
                  </td>
                </tr>
                <tr>
                  <td className="font-sans text-sm font-medium text-foreground">Errors</td>
                  <td className="font-mono text-sm text-foreground">0 / 500</td>
                  <td className="font-sans text-sm text-muted-foreground leading-relaxed">
                    Wall time 369 s on Mac M2 Pro Metal
                  </td>
                </tr>
              </tbody>
            </table>
          </Figure>
        </Reveal>

        <Reveal delay={150}>
          <h3 className="font-serif text-lg font-light text-foreground mt-10 mb-3">
            By question type (recall_any@10)
          </h3>
          <Figure
            title="Table IX · LongMemEval-S recall by question type"
            label="LongMemEval-S recall by question type"
          >
            <table className="fact-table w-full">
              <thead>
                <tr>
                  <th scope="col">Question type</th>
                  <th scope="col" className="w-24">
                    n
                  </th>
                  <th scope="col" className="w-32">
                    recall_any@10
                  </th>
                </tr>
              </thead>
              <tbody>
                <tr>
                  <td className="font-sans text-sm font-medium text-foreground">
                    knowledge-update
                  </td>
                  <td className="font-mono text-sm text-muted-foreground">78</td>
                  <td className="font-mono text-sm text-foreground">1.000</td>
                </tr>
                <tr>
                  <td className="font-sans text-sm font-medium text-foreground">multi-session</td>
                  <td className="font-mono text-sm text-muted-foreground">133</td>
                  <td className="font-mono text-sm text-foreground">1.000</td>
                </tr>
                <tr>
                  <td className="font-sans text-sm font-medium text-foreground">
                    single-session-assistant
                  </td>
                  <td className="font-mono text-sm text-muted-foreground">56</td>
                  <td className="font-mono text-sm text-foreground">0.964</td>
                </tr>
                <tr>
                  <td className="font-sans text-sm font-medium text-foreground">
                    single-session-preference
                  </td>
                  <td className="font-mono text-sm text-muted-foreground">30</td>
                  <td className="font-mono text-sm text-foreground">0.933</td>
                </tr>
                <tr>
                  <td className="font-sans text-sm font-medium text-foreground">
                    single-session-user
                  </td>
                  <td className="font-mono text-sm text-muted-foreground">70</td>
                  <td className="font-mono text-sm text-foreground">0.986</td>
                </tr>
                <tr>
                  <td className="font-sans text-sm font-medium text-foreground">
                    temporal-reasoning
                  </td>
                  <td className="font-mono text-sm text-muted-foreground">133</td>
                  <td className="font-mono text-sm text-foreground">0.962</td>
                </tr>
              </tbody>
            </table>
          </Figure>

          <div className="mt-8 max-w-[68ch] font-sans text-sm leading-[1.72] text-muted-foreground space-y-3 border-l-2 border-primary pl-4">
            <p className="font-semibold text-foreground">What this number is and what it isn’t.</p>
            <p>
              The 0.98 result measures whether at least one gold-evidence session appears in the top
              ten retrieved sessions. It does not measure answer accuracy, reasoning quality, or the
              fraction of all required evidence retrieved.
            </p>
            <p>
              This retrieval-only experiment is not a head-to-head comparison with systems that
              evaluate generated answers. A fair comparison needs the same corpus, evidence
              granularity, metric, and evaluation procedure. We do not claim a performance lead from
              unlike measurements.
            </p>
            <p>
              The other caveat: bge-small was trained on broad web data that may overlap with
              LongMemEval’s academic distribution. Our internal Eval B is the more honest
              dogfood-distribution check.
            </p>
          </div>
        </Reveal>
      </section>

      {/* ============ INFERENCE BENCHMARK ============ */}

      <div className="mt-20">
        <div className="rail-rule" role="presentation" />
      </div>

      {/* Inference section header */}
      <Reveal>
        <section className="pt-12">
          <p className="eyebrow-mono mb-4">Local model inference</p>
          <h2 className="font-serif text-2xl font-light text-foreground leading-[1.2] mb-5 max-w-[40ch]">
            Throughput across machines.
          </h2>
          <p className="max-w-[68ch] font-sans text-sm leading-[1.72] text-muted-foreground">
            Same retrieval workloads were dispatch-bound. Inference at 7-8B parameters is the
            opposite, compute-bound, where GPUs actually earn their keep. We ran the same four
            models across the same three machines and the story is materially different.
          </p>
        </section>
      </Reveal>

      {/* Inference run context */}
      <Reveal>
        <section className="mt-12 pt-10 border-t border-border">
          <h2 className="font-serif text-xl font-light text-foreground mb-6">Run context</h2>
          <dl className="grid grid-cols-1 gap-y-3 sm:grid-cols-[12rem_1fr] font-sans text-sm">
            <dt className="text-muted-foreground">Runtime</dt>
            <dd className="text-foreground">
              <code className="font-mono text-xs">llama-cpp-python</code> 0.3.23 / 0.3.24 (built
              from source on Linux + Windows for CUDA / Vulkan)
            </dd>

            <dt className="text-muted-foreground">Models</dt>
            <dd className="text-foreground">
              Phi-3-mini 3.8B, Qwen 2.5 7B, Llama 3.1 8B (all Q4_K_M); Llama 3.1 70B (Q3_K_S).
              Pinned by SHA-256.
            </dd>

            <dt className="text-muted-foreground">Prompts</dt>
            <dd className="text-foreground">
              10 fixed prompts (2 short Q&amp;A, 3 code-gen, 3 summarization, 2 long-context). Seed
              42, temp 0.7, top_p 0.9. 3 runs per cell, report median.
            </dd>

            <dt className="text-muted-foreground">Hardware</dt>
            <dd className="text-foreground">
              Same three machines as above. Backends switch per platform: Metal / CUDA / Vulkan.
            </dd>

            <dt className="text-muted-foreground">Date</dt>
            <dd className="text-foreground">2026-06-01</dd>
          </dl>
        </section>
      </Reveal>

      {/* Inference machines context */}
      <Reveal>
        <section className="mt-12 pt-10 border-t border-border">
          <h2 className="font-serif text-xl font-light text-foreground mb-6">Machines</h2>
          <dl className="grid grid-cols-1 gap-y-3 sm:grid-cols-[6rem_1fr] font-sans text-sm">
            {INFERENCE_HARDWARE.map((m) => (
              <div key={m.label} className="contents">
                <dt className="text-muted-foreground">{m.label}</dt>
                <dd className="text-foreground">{m.spec}</dd>
              </div>
            ))}
          </dl>
        </section>
      </Reveal>

      {/* Inference headline results */}
      <section className="mt-12 pt-10 border-t border-border">
        <Reveal>
          <h2 className="font-serif text-xl font-light text-foreground mb-3">
            Generation throughput: tokens per second
          </h2>
          <p className="max-w-[60ch] mb-6 font-sans text-sm leading-[1.72] text-muted-foreground">
            Median across the 10-prompt suite. Higher is better. The story changes from row to row
            and column to column, that is the finding.
          </p>
        </Reveal>
        <Reveal delay={100}>
          <Figure
            title="Table XI · Generation throughput, tokens/sec"
            date="Measured · 2026-06-01"
            label="Generation throughput in tokens per second"
            foot={
              <>
                Adapter footnote: llama-cpp-python prefix-matches the KV cache across calls. Without
                an explicit model.reset() between runs, TTFT on run 2+ measures cache hits, not cold
                prompt evaluation. Not in the upstream API docs, a real footgun for benchmark
                writers.
              </>
            }
          >
            <table className="fact-table w-full">
              <thead>
                <tr>
                  <th scope="col" className="w-56">
                    Model
                  </th>
                  <th scope="col" className="w-24">
                    Mac
                  </th>
                  <th scope="col" className="w-24">
                    Linux
                  </th>
                  <th scope="col" className="w-24">
                    Windows
                  </th>
                  <th scope="col">Notes</th>
                </tr>
              </thead>
              <tbody>
                {INFERENCE_RESULTS.map((row) => (
                  <tr key={row.model}>
                    <td className="font-sans text-sm font-medium text-foreground">{row.model}</td>
                    <td className="font-mono text-sm text-foreground">{row.mac}</td>
                    <td className="font-mono text-sm text-foreground">{row.linux}</td>
                    <td className="font-mono text-sm text-foreground">{row.windows}</td>
                    <td className="font-sans text-sm text-muted-foreground leading-relaxed">
                      {row.note}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </Figure>
        </Reveal>
      </section>

      {/* Inference notable findings */}
      <section className="mt-12 pt-10 border-t border-border">
        <Reveal>
          <h2 className="font-serif text-xl font-light text-foreground mb-3">
            Three findings worth surfacing
          </h2>
        </Reveal>
        <div className="max-w-[72ch] font-sans text-sm leading-[1.72] text-muted-foreground space-y-6">
          <Reveal delay={90}>
            <div>
              <p className="font-semibold text-foreground mb-1">1. There is no universal winner.</p>
              <p>
                The RX 6600 XT <em>edges</em> the Mac M2 Pro on Qwen 7B (15.9 vs 12.4 tok/s, ~28%
                faster),
                <em> trails</em> it on the smaller Phi-3 mini 3.8B (16.2 vs 19.1), and{' '}
                <em>collapses</em> on Llama 8B (2.1 vs 11.6) once the model no longer fits the
                card’s 8 GB of VRAM and spills to CPU. Apple’s unified-memory cache wins the small
                model, the AMD card wins the 7B that fits its VRAM, and neither wins the 8B the same
                way. The right hardware depends on whether the model fits the card, not the brand.
              </p>
            </div>
          </Reveal>

          <Reveal delay={160}>
            <div>
              <p className="font-semibold text-foreground mb-1">
                2. AMD/Vulkan is viable up to its VRAM ceiling.
              </p>
              <p>
                On Qwen 7B, the model both cards run cleanly, the RTX 2080 Ti delivers ~2.9× the
                throughput of the RX 6600 XT (45.9 vs 15.9 tok/s) for roughly 2× the used-market
                price, so cost per token is in the same range. The catch is capacity: the 2080 Ti’s
                11 GB holds the 8B model, the 6600 XT’s 8 GB does not. The “you need NVIDIA for
                local inference” reflex is a generation out of date for models that fit, and VRAM
                size, not vendor, decides which models those are.
              </p>
            </div>
          </Reveal>

          <Reveal delay={230}>
            <div>
              <p className="font-semibold text-foreground mb-1">
                3. 70B is the local-only ceiling on this hardware tier.
              </p>
              <p>
                Llama 3.1 70B at Q3_K_S won’t load in 16 GB Mac unified memory or 8 GB AMD VRAM. On
                the Linux box with 62 GB system RAM and 11 GB VRAM, it runs with about 20 of its 80
                layers offloaded to the GPU and the rest on CPU, at a 2,048-token context. The
                result is 2.4 tok/s. Technically possible, unusable for chat. The gap between the
                consumer-hardware ceiling and a usable 70B is roughly 10 to 20×, and a single
                consumer card does not close it. Pooled compute does.
              </p>
              <p className="mt-3">
                And 70B isn’t even the local-model frontier in 2026. DeepSeek V3 and R1 (671B MoE,
                ~37B active) at Unsloth’s most aggressive Q1.58 quant need ~131 GB of unified memory
                or 192 GB+ of system RAM, a hardware tier above any of the three machines benched
                here. People do run these models on consumer hardware; they run them on Mac Studio
                M3 Ultras and 192 GB+ DDR5 workstations. The shared-compute argument isn’t abstract.
                It’s whichever model your hardware can’t fit.
              </p>
            </div>
          </Reveal>
        </div>
      </section>

      {/* What this measures and doesn't */}
      <Reveal>
        <section className="mt-12 pt-10 border-t border-border">
          <h2 className="font-serif text-xl font-light text-foreground mb-3">
            What the oracle memory bench measures and what it doesn&apos;t
          </h2>
          <div className="max-w-[68ch] font-sans text-sm leading-[1.72] text-muted-foreground space-y-4">
            <div>
              <p className="font-semibold text-foreground mb-1">It measures:</p>
              <ul className="list-disc pl-5 space-y-1">
                <li>
                  Whether the bge-small embedder can rank evidence sessions correctly when given
                  only those sessions.
                </li>
                <li>Per-call retrieval latency on Apple Silicon (the reference hardware tier).</li>
                <li>Absence of crashes or OOMs on the full 500-instance suite.</li>
              </ul>
            </div>
            <div>
              <p className="font-semibold text-foreground mb-1">It does not measure:</p>
              <ul className="list-disc pl-5 space-y-1">
                <li>
                  Retrieval quality against filler noise. The separate S-split results above address
                  that question.
                </li>
                <li>End-to-end answer accuracy (needs an LLM judge and a generation model).</li>
                <li>
                  The full <code className="font-mono text-xs">get_relevant_memories()</code> path
                  with RRF and ambient-preference injection. Oracle split has no ambient
                  preferences, so this would add noise without measuring anything.
                </li>
                <li>
                  The latency of every hardware/backend combination. See the platform measurements
                  for the specific machines tested.
                </li>
                <li>Memory store scalability past ~10,000 entries (no ANN index yet).</li>
              </ul>
            </div>
          </div>
        </section>
      </Reveal>

      {/* Gaps to fix before next publish */}
      <Reveal>
        <section className="mt-12 pt-10 border-t border-border">
          <h2 className="font-serif text-xl font-light text-foreground mb-3">
            Known gaps to fix before next publish
          </h2>
          <Figure
            title="Known limits · read this before quoting us"
            label="Known gaps to fix before the next publish"
          >
            <div className="px-1 py-1">
              <ol className="list-decimal pl-9 pr-5 py-4 max-w-[68ch] space-y-2 font-sans text-sm leading-[1.72] text-muted-foreground">
                {GAPS.map((gap) => (
                  <li key={gap}>{gap}</li>
                ))}
              </ol>
            </div>
          </Figure>
        </section>
      </Reveal>

      {/* Reproducibility + attribution */}
      <Reveal>
        <section className="mt-12 pt-10 border-t border-border">
          <h2 className="font-serif text-xl font-light text-foreground mb-3">
            Recorded results and reproducibility
          </h2>
          <div className="max-w-[68ch] font-sans text-sm leading-[1.72] text-muted-foreground space-y-3">
            <p>
              The application repository and benchmark harness are not public yet, so these are
              first-party measurements, not independently reproduced results.{' '}
              <a href="/benchmarks/longmemeval-s-2026-06-02.txt">
                Read the recorded LongMemEval-S summary
              </a>
              . The internal commands below document how the runs were made; source publication is
              still on the roadmap.
            </p>
            <p>
              <code className="font-mono text-xs">make bench</code> from the repo root runs the full
              500-instance suite. Results land in{' '}
              <code className="font-mono text-xs">bench/results/&lt;timestamp&gt;/</code>. The
              embedder (BAAI/bge-small-en-v1.5) downloads on first run from HuggingFace (~33 million
              parameters). No OpenAI API key, no GPU required.
            </p>
            <p>
              LongMemEval data is used under the original MIT license. Vendored at{' '}
              <code className="font-mono text-xs">
                bench/longmemeval/data/longmemeval_oracle.json
              </code>
              . Source:{' '}
              <a
                href="https://github.com/xiaowu0162/LongMemEval"
                target="_blank"
                rel="noopener noreferrer"
                className="text-primary underline-offset-4 hover:underline focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 focus-visible:ring-offset-background"
              >
                github.com/xiaowu0162/LongMemEval
              </a>
              .
            </p>
            <p>
              For the inference bench:{' '}
              <code className="font-mono text-xs">
                make bench-inference MODEL=bench/models/&lt;name&gt;.gguf
              </code>
              . Models are pinned by SHA-256 in{' '}
              <code className="font-mono text-xs">bench/models/SHA256SUMS.txt</code> and downloaded
              on first run via the verification helper. Phi-3-mini-3.8B, Qwen-2.5-7B, and
              Llama-3.1-8B are ~2-5 GB each at Q4_K_M; Llama-3.1-70B-Q3_K_S is ~29 GB.
            </p>
          </div>
        </section>
      </Reveal>

      {/* Refresh cadence */}
      <Reveal>
        <section className="mt-12 pt-10 border-t border-border">
          <h2 className="font-serif text-xl font-light text-foreground mb-3">Refresh cadence</h2>
          <p className="max-w-[68ch] font-sans text-sm leading-[1.72] text-muted-foreground">
            We update the measurement dates when new runs are reviewed. A desktop release does not
            automatically mean these benchmarks have been rerun. The results on this page remain the
            May–June 2026 runs.
          </p>
        </section>
      </Reveal>

      {/* Nav */}
      <div className="mt-16">
        <div className="rail-rule" role="presentation" />
      </div>
      <div className="mt-8 flex flex-col gap-3 sm:flex-row sm:items-center sm:justify-between">
        <p className="font-sans text-sm text-muted-foreground">
          Read the rest of the operating documents.
        </p>
        <div className="flex flex-wrap gap-3">
          <Link
            href="/roadmap"
            className="btn-outline shrink-0 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 focus-visible:ring-offset-background"
          >
            Roadmap
          </Link>
          <Link
            href="/manifesto"
            className="btn-outline shrink-0 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 focus-visible:ring-offset-background"
          >
            Manifesto
          </Link>
        </div>
      </div>
    </article>
  );
}
