import type { Metadata } from 'next';
import { PageIntro } from '@/components/PageIntro';
import Link from 'next/link';
import { Reveal } from '@/components/motion';
import {
  DOWNLOAD_LINKS,
  DISCORD_INVITE,
  CHECKSUMS,
  RELEASE_VERSION,
  LINUX_RELEASE_VERSION,
  WINDOWS_RELEASE_VERSION,
} from '@/lib/downloads';

export const metadata: Metadata = {
  alternates: { canonical: "/install" },
  title: 'Install',
  description:
    'Install the Potluck desktop app: local AI on your own machines, persistent memory for your coding agents, and an embedded mesh that joins your machines into one private network.',
  openGraph: {
    title: 'Install Potluck AI',
    description:
      'One installer per platform. Local models, agent memory over MCP, and an embedded WireGuard mesh. Account required; local inference works offline after setup.',
  },
};

type PlatformTone = 'ember' | 'sage' | 'muted';

const DOT_CLASS: Record<PlatformTone, string> = {
  ember: 'bg-primary',
  sage: 'bg-sage',
  muted: 'bg-muted-foreground',
};

function PlatformLabel({ tone, children }: { tone: PlatformTone; children: string }) {
  return (
    <h3 className="mb-3 flex items-center gap-2 font-mono text-xs font-semibold uppercase tracking-[0.14em] text-foreground">
      <span aria-hidden="true" className={`h-1.5 w-1.5 shrink-0 rounded-full ${DOT_CLASS[tone]}`} />
      {children}
    </h3>
  );
}

// Real download links for a platform. The first artifact is the primary
// (filled) button; any others render as quiet secondary links beside it.
function DownloadRow({
  links,
}: {
  links: Array<{ label: string; href: string; primary?: boolean }>;
}) {
  return (
    <div className="mt-4 flex flex-wrap items-center gap-x-4 gap-y-2">
      {links.map((link) =>
        link.primary ? (
          <a
            key={link.href}
            href={link.href}
            className="btn-primary shrink-0 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 focus-visible:ring-offset-background"
          >
            {link.label}
          </a>
        ) : (
          <a
            key={link.href}
            href={link.href}
            className="font-mono text-xs uppercase tracking-wide text-muted-foreground underline-offset-4 transition-colors duration-150 hover:text-primary hover:underline focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring rounded-sm"
          >
            {link.label}
          </a>
        ),
      )}
    </div>
  );
}

const REQUIREMENTS: Array<{ item: string; tone: PlatformTone | null; detail: string }> = [
  {
    item: 'Mac',
    tone: 'ember',
    detail:
      'macOS 13+, Apple Silicon. Intel downloads are paused while we repair and validate the native build. Signed and notarized, so it opens normally, no right-click needed.',
  },
  {
    item: 'Windows',
    tone: 'muted',
    detail:
      'Windows 10+. The installer is unsigned, so SmartScreen warns once (More info, then Run anyway). We publish a SHA-256 for every build so you can verify it yourself.',
  },
  {
    item: 'Linux',
    tone: 'sage',
    detail:
      'A .deb for Debian, Ubuntu, and Pop!_OS, plus a portable .AppImage for other distributions. 64-bit x86 (amd64), glibc 2.35+ (Ubuntu 22.04 or newer). A compatible Vulkan driver is required for GPU acceleration.',
  },
  {
    item: 'Cross-machine',
    tone: null,
    detail:
      'The WireGuard mesh is included, and machines pair inside the app. Enabling it may request administrator permission for a background service and network adapter. No separate VPN account is needed.',
  },
  {
    item: 'Disk',
    tone: null,
    detail:
      'Models are the bulk of it: a few GB for a starter model, more if you pick larger ones. The app shows the download size before it fetches anything, and every download is SHA-256 verified.',
  },
  {
    item: 'Access',
    tone: null,
    detail:
      'Free to download and run locally. A Potluck account is required to use the desktop app. After signing in and downloading a model, local inference can work offline.',
  },
];

const MAC_INSTALL = [
  'Download the Apple Silicon (arm64) DMG for M1 and newer. Intel downloads are temporarily paused.',
  'Double-click the DMG, drag the app to Applications, then eject.',
  'Open it from Applications. The build is notarized, so it opens normally with no right-click. First launch unpacks the bundled engine (~10-20 seconds).',
];

const WINDOWS_INSTALL = [
  'Download the installer .exe.',
  'On the SmartScreen warning, click "More info" then "Run anyway." Windows builds are unsigned; the same binaries run on our own machines, and you can match the published SHA-256 before you run it.',
  'After install, Potluck appears in the Start menu.',
];

const FIRST_RUN = [
  'Hardware check. The app reads your RAM and GPU and tells you what it can run well on this machine.',
  'Pick a starter model. It downloads with a visible size and progress, hash-verified before it loads.',
  'Pair your other machines (optional). Machines on the same network find each other; a short pairing code confirms it is really yours.',
  'Readiness check. Real probes, not a green dot: can this machine actually run inference, is a VPN fighting the mesh, is the mesh port reachable.',
  'Sign in to use the app. Enter your email, open the magic link, and confirm the account. Then choose Chat to try your model.',
];

const CLI_MAC_VERSION = '0.1.10';
const CLI_LINUX_VERSION = '0.1.10';
const CLI_TAP = 'https://github.com/newtorob/homebrew-potluck';
const CLI_ARCHIVES = {
  macArm: `https://releases.trypotluck.ai/cli/${CLI_MAC_VERSION}/potluck-cli-${CLI_MAC_VERSION}-darwin-arm64.tar.gz`,
  linuxX64: `https://releases.trypotluck.ai/cli/${CLI_LINUX_VERSION}/potluck-cli-${CLI_LINUX_VERSION}-linux-x64.tar.gz`,
};

const CLI_STEPS: Array<{ command: string; detail: string }> = [
  {
    command: 'brew install newtorob/potluck/potluck',
    detail: 'Installs the CLI, its bundled runtime, and Node.',
  },
  {
    command: 'potluck setup',
    detail:
      'Recommends a model that fits this machine, asks before downloading it, and tests a local reply.',
  },
  {
    command: 'potluck login',
    detail:
      'Starts the engine if it is not running and opens your browser to approve this terminal. Over SSH it prints a link to open on any device. With the desktop app 0.1.7 or later it uses the app\'s sign-in.',
  },
];

const HONEST_LIMITS = [
  {
    title: 'The CLI shares the desktop app\'s engine from desktop 0.1.7.',
    body: 'With 0.1.7 or later on the same computer, CLI commands use the app\'s engine and sign-in. With an older desktop app running, potluck up reports that another runtime is already listening; update the app first.',
  },
  {
    title: 'Windows installers are unsigned.',
    body: 'The Mac build is notarized, so Gatekeeper opens it normally. Windows stays unsigned, so SmartScreen warns once; we publish a SHA-256 for every build so you can verify integrity without a signature.',
  },
  {
    title: 'Trusted-circle inference and the open pool are in development.',
    body: 'Use local inference or My Machines in the current release. The wider contributor network is not ready for dependable public use, and the contribution toggle does not launch an open-pool contributor in this build.',
  },
  {
    title: 'Peer memory sharing is per-machine, all or nothing.',
    body: 'When you opt a machine in, paired peers can search that whole store. Per-project sharing flags are on the roadmap.',
  },
  {
    title: 'Mesh membership is the trust boundary.',
    body: 'Any machine paired into your mesh can call the peer endpoints you opted into. A signed-nonce peer credential replacing reachability-as-trust is planned hardening.',
  },
];

export default function InstallPage() {
  return (
    <article className="site-page">
      <PageIntro title={<>Install on one machine. Or all of them.</>} detail="Desktop downloads and setup">Potluck is a desktop app that runs open models on hardware you own. Chat with them, give
            your coding agents a persistent memory over MCP, and join your machines into one private
            network with the WireGuard mesh built into the app. Choose local inference or route a
            request to your own paired machines. Trusted-circle inference and the open contributor
            pool are still in development.</PageIntro>

      {/* Prereqs */}
      <p className="docs-note">
        Published versions: Apple Silicon Mac <strong>{RELEASE_VERSION}</strong>; Linux{' '}
        <strong>{LINUX_RELEASE_VERSION}</strong>; Windows{' '}
        <strong>{WINDOWS_RELEASE_VERSION}</strong>. Intel Mac downloads are paused.
      </p>

      <section className="mt-2 pt-8 border-t border-border">
        <Reveal>
          <h2 className="font-serif text-xl font-light text-foreground mb-6">Before you start</h2>
          <div className="fig-frame">
            <div className="fig-cap">
              <span>Fig. 1 · Requirements</span>
              <span className="fig-cap-date">{REQUIREMENTS.length} items</span>
            </div>
            <table className="fact-table">
              <caption className="sr-only">
                Requirements for installing Potluck, by platform
              </caption>
              <thead>
                <tr>
                  <th scope="col" className="w-40">
                    Item
                  </th>
                  <th scope="col">Requirement</th>
                </tr>
              </thead>
              <tbody>
                {REQUIREMENTS.map((req) => (
                  <tr key={req.item}>
                    <td className="align-top">
                      {req.tone ? (
                        <span className="inline-flex items-center gap-2 font-mono text-xs uppercase tracking-wide text-muted-foreground">
                          <span
                            aria-hidden="true"
                            className={`h-1.5 w-1.5 shrink-0 rounded-full ${DOT_CLASS[req.tone]}`}
                          />
                          {req.item}
                        </span>
                      ) : (
                        <span className="font-mono text-xs uppercase tracking-wide text-muted-foreground">
                          {req.item}
                        </span>
                      )}
                    </td>
                    <td className="font-sans text-sm leading-[1.6] text-foreground">
                      {req.detail}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </Reveal>
      </section>

      {/* Install */}
      <section className="mt-12 pt-10 border-t border-border">
        <Reveal>
          <h2 className="font-serif text-xl font-light text-foreground mb-3">Install</h2>
          <p className="max-w-[60ch] mb-8 font-sans text-sm leading-[1.72] text-muted-foreground">
            One installer per platform. The app carries everything it needs: the inference engine,
            the memory store, the MCP server, and the mesh. There is no step two where you install
            something else.
          </p>
        </Reveal>

        <div className="space-y-6">
          <Reveal delay={0}>
            <div className="rounded-md border border-border bg-card/60 p-5">
              <PlatformLabel tone="ember">macOS</PlatformLabel>
              <ol className="font-sans text-sm leading-[1.72] text-muted-foreground space-y-2 list-decimal pl-5 max-w-[68ch]">
                {MAC_INSTALL.map((step) => (
                  <li key={step}>{step}</li>
                ))}
              </ol>
              <DownloadRow
                links={[
                  {
                    label: 'Download for macOS (Apple Silicon)',
                    href: DOWNLOAD_LINKS.macArm,
                    primary: true,
                  },
                  { label: 'SHA-256 checksums', href: CHECKSUMS.current },
                ]}
              />
            </div>
          </Reveal>
          <Reveal delay={110}>
            <div className="rounded-md border border-border bg-card/60 p-5">
              <PlatformLabel tone="muted">Windows</PlatformLabel>
              <ol className="font-sans text-sm leading-[1.72] text-muted-foreground space-y-2 list-decimal pl-5 max-w-[68ch]">
                {WINDOWS_INSTALL.map((step) => (
                  <li key={step}>{step}</li>
                ))}
              </ol>
              <DownloadRow
                links={[
                  { label: 'Download for Windows', href: DOWNLOAD_LINKS.windows, primary: true },
                  { label: 'SHA-256 checksum', href: CHECKSUMS.windows },
                ]}
              />
            </div>
          </Reveal>
          <Reveal delay={220}>
            <div className="rounded-md border border-border bg-card/60 p-5">
              <PlatformLabel tone="sage">Linux</PlatformLabel>
              <p className="mb-3 font-sans text-sm leading-[1.72] text-muted-foreground">
                GPU acceleration uses your installed Vulkan driver, with automatic CPU fallback
                when a compatible GPU is unavailable. Validated on NVIDIA RTX 2080 Ti and Intel
                integrated graphics; AMD hardware validation is still pending.
              </p>
              <ol className="font-sans text-sm leading-[1.72] text-muted-foreground space-y-2 list-decimal pl-5 max-w-[68ch]">
                <li>
                  On Debian, Ubuntu, or Pop!_OS, download the .deb and install it with{' '}
                  <code className="font-mono text-xs">
                    sudo apt install ./potluck-ai-desktop_{LINUX_RELEASE_VERSION}_amd64.deb
                  </code>
                  , then open Potluck from the app menu.
                </li>
                <li>
                  On other distributions, download the .AppImage, make it executable with{' '}
                  <code className="font-mono text-xs">chmod +x</code>, and run it directly.
                </li>
                <li>
                  Turning on the mesh installs a small background service. It asks for your password
                  once through the system polkit prompt, no terminal required.
                </li>
              </ol>
              <DownloadRow
                links={[
                  {
                    label: 'Download for Linux (.deb)',
                    href: DOWNLOAD_LINKS.linuxDeb,
                    primary: true,
                  },
                  { label: 'AppImage', href: DOWNLOAD_LINKS.linuxAppImage },
                  { label: 'SHA-256 checksums', href: CHECKSUMS.linux },
                ]}
              />
            </div>
          </Reveal>
        </div>
      </section>

      {/* Command line */}
      <section id="cli" className="mt-12 pt-10 border-t border-border">
        <Reveal>
          <h2 className="font-serif text-xl font-light text-foreground mb-3">
            Command line (Mac and Linux)
          </h2>
          <p className="max-w-[68ch] mb-6 font-sans text-sm leading-[1.72] text-muted-foreground">
            Prefer a terminal, or setting up a machine over SSH? The Potluck CLI runs the same local
            engine without the desktop app. It sets up and tests a model, manages models, connects
            your machines, and shows usage. It also has a terminal coding agent and can launch Aider
            against your local models.
          </p>
          <div className="rounded-md border border-border bg-card/60 p-5">
            <PlatformLabel tone="ember">{`Potluck CLI: Linux ${CLI_LINUX_VERSION} · Mac ${CLI_MAC_VERSION}`}</PlatformLabel>
            <ol className="font-sans text-sm leading-[1.72] text-muted-foreground space-y-3 list-decimal pl-5 max-w-[68ch]">
              {CLI_STEPS.map((step) => (
                <li key={step.command}>
                  <code className="font-mono text-xs text-foreground">{step.command}</code>
                  <br />
                  {step.detail}
                </li>
              ))}
            </ol>
            <p className="mt-4 max-w-[68ch] font-sans text-sm leading-[1.72] text-muted-foreground">
              macOS 15 or newer on Apple Silicon, or Linux x86-64 with glibc 2.35 or newer (Ubuntu
              22.04 and later). Standalone archives install without Homebrew and need Node.js 20 or
              newer; each has a <code className="font-mono text-xs">.sha256</code> file beside it.
              Run <code className="font-mono text-xs">potluck --help</code> for every command.
            </p>
            <DownloadRow
              links={[
                { label: 'Homebrew tap', href: CLI_TAP, primary: true },
                { label: 'macOS archive (arm64)', href: CLI_ARCHIVES.macArm },
                { label: 'Linux archive (x64)', href: CLI_ARCHIVES.linuxX64 },
              ]}
            />
          </div>
        </Reveal>
      </section>

      {/* First-run */}
      <section className="mt-12 pt-10 border-t border-border">
        <Reveal>
          <h2 className="font-serif text-xl font-light text-foreground mb-3">First run</h2>
          <p className="max-w-[68ch] mb-4 font-sans text-sm leading-[1.72] text-muted-foreground">
            The app walks you through hardware, model setup, and optional machine pairing. A Potluck
            account is required to use the app. Initial sign-in and model downloads need a connection;
            local inference can keep working offline after setup.
          </p>
          <ol className="max-w-[68ch] font-sans text-sm leading-[1.72] text-muted-foreground space-y-2 list-decimal pl-5">
            {FIRST_RUN.map((step) => (
              <li key={step}>{step}</li>
            ))}
          </ol>
        </Reveal>
      </section>

      {/* Coding agents / MCP */}
      <section className="mt-12 pt-10 border-t border-border">
        <Reveal>
          <h2 className="font-serif text-xl font-light text-foreground mb-3">
            Bring your coding agents
          </h2>
          <div className="max-w-[68ch] font-sans text-sm leading-[1.72] text-muted-foreground space-y-3">
            <p>
              A compatible MCP client, such as Codex, Cursor, Zed, Cline, Continue, or Claude Code,
              can use Potluck as its persistent memory: project facts, decisions, and multi-session
              tasks that survive across sessions. Access from another machine requires optional peer
              memory sharing.
            </p>
            <p>
              You do not hand-write the config. Open Settings, then Connect tools, pick your agent,
              and the app generates the exact MCP entry pointing at its bundled server. Paste it
              where your agent expects MCP servers and restart the agent. It will see{' '}
              <code className="font-mono text-xs">memory_search</code>,{' '}
              <code className="font-mono text-xs">memory_save</code>,{' '}
              <code className="font-mono text-xs">task_create</code>, and the rest of the Potluck
              tools.
            </p>
            <p>
              The fastest proof it works: ask the agent to remember something, then find it in
              Potluck&apos;s memory browser. The agent activity view shows connected MCP sessions
              with tool-call metadata; it does not expose external agents' raw prompt payloads.
            </p>
          </div>
        </Reveal>
      </section>

      <aside className="docs-note">Connecting another computer? Follow the <Link href="/blog/run-a-local-llm-on-another-computer">two-machine setup guide</Link>, including model loading, serving, and connection checks.</aside>

      {/* Cross-machine */}
      <section className="mt-12 pt-10 border-t border-border">
        <Reveal>
          <h2 className="font-serif text-xl font-light text-foreground mb-3">
            Your machines, one network
          </h2>
          <div className="max-w-[68ch] font-sans text-sm leading-[1.72] text-muted-foreground space-y-3">
            <p>
              Pairing happens inside the app. Machines on the same network discover each other; you
              confirm with a short pairing code. Under the hood this builds a private WireGuard mesh
              between machines you own, with no third-party VPN account and no config to edit.
            </p>
            <p>
              For another machine to answer, it must be reachable, have the requested model loaded,
              and have serving enabled. Choose My Machines in chat to use eligible paired computers.
              Keep local mode selected to run inference on this computer. Trusted-circle inference
              and the open pool are not ready in this release.
            </p>
            <p>
              Memory works across the mesh too, if you opt in per machine: your agent on the laptop
              can search memories saved on the desktop. Peers only ever reach the narrow peer
              endpoints; your full memory store, tasks, conversations, and settings stay
              loopback-only. Peer reads are read-only; writes stay local.
            </p>
          </div>
        </Reveal>
      </section>

      {/* Honest limits */}
      <section className="mt-12 pt-10 border-t border-border">
        <Reveal>
          <h2 className="font-serif text-xl font-light text-foreground mb-3">Honest limits</h2>
          <ul className="max-w-[68ch] font-sans text-sm leading-[1.72] text-muted-foreground space-y-3 list-disc pl-5">
            {HONEST_LIMITS.map((limit) => (
              <li key={limit.title}>
                <strong className="font-semibold text-foreground">{limit.title}</strong>{' '}
                {limit.body}
              </li>
            ))}
          </ul>
        </Reveal>
      </section>

      {/* Where to go next */}
      <section className="mt-12 pt-10 border-t border-border">
        <Reveal>
          <h2 className="font-serif text-xl font-light text-foreground mb-3">Where to go next</h2>
          <ul className="max-w-[68ch] font-sans text-sm leading-[1.72] text-muted-foreground space-y-2 list-disc pl-5">
            <li>
              <Link
                href="/how-it-works"
                className="text-primary underline-offset-4 hover:underline"
              >
                /how-it-works
              </Link>{' '}
              · the routing scopes, the mesh, and what a contributor can and cannot see.
            </li>
            <li>
              <Link href="/benchmarks" className="text-primary underline-offset-4 hover:underline">
                /benchmarks
              </Link>{' '}
              · measured numbers across memory retrieval, inference, and cross-machine latency on
              Mac/Linux/Windows.
            </li>
            <li>
              <Link href="/manifesto" className="text-primary underline-offset-4 hover:underline">
                /manifesto
              </Link>{' '}
              · why Potluck is built as a member-operated network and a founder-led company.
            </li>
            <li>
              <Link href="/roadmap" className="text-primary underline-offset-4 hover:underline">
                /roadmap
              </Link>{' '}
              · what is shipped, in flight, designed, and to be decided.
            </li>
          </ul>
        </Reveal>
      </section>

      {/* Nav */}
      <Reveal>
        <div className="mt-14 pt-8 border-t border-border flex flex-col gap-3 sm:flex-row sm:items-center sm:justify-between">
          <p className="font-sans text-sm text-muted-foreground">Questions, or want to shape it?</p>
          <a
            href={DISCORD_INVITE}
            target="_blank"
            rel="noopener noreferrer"
            className="btn-outline shrink-0 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 focus-visible:ring-offset-background"
          >
            Join the Discord
          </a>
        </div>
      </Reveal>
    </article>
  );
}
