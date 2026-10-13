import type { Metadata } from 'next';
import Link from 'next/link';
import { RELEASE_VERSION, LINUX_RELEASE_VERSION, WINDOWS_RELEASE_VERSION } from '@/lib/downloads';

export const metadata: Metadata = {
  title: 'Roadmap',
  description:
    'What you can use in Potluck today, what is being tested, and what is still planned. Updated October 6, 2026.',
};
const GROUPS = [
  {
    id: 'available',
    title: 'Available today',
    description: 'Included in the public desktop releases.',
    items: [
      [
        'Local AI on your hardware',
        'Sign in, download a model, load it on your CPU or GPU, and chat locally. After setup, local inference can work offline.',
      ],
      [
        'Your own machine network',
        'In-app pairing, a bundled WireGuard mesh, connection diagnostics, and inference on eligible paired machines with a loaded model.',
      ],
      [
        'Persistent memory and tasks',
        'A local memory browser and MCP server for compatible agents, with pinned context and optional read-only memory search across your paired machines.',
      ],
      [
        'Desktop 0.1.7',
        'The app and the Potluck CLI share one engine and sign-in on Mac and Linux. An interrupted answer from one of your computers can restart on another, and the last model you loaded comes back when the engine starts.',
      ],
      [
        'Linux GPU acceleration in 0.1.8',
        'The Linux desktop 0.1.8 and CLI 0.1.7 onward include Vulkan GPU acceleration for local models, with automatic CPU fallback. Validated on NVIDIA RTX 2080 Ti and Intel integrated graphics. AMD hardware validation is pending.',
      ],
      [
        'Public installers and updates',
        `Apple Silicon Mac is on ${RELEASE_VERSION}; x86-64 Linux is on ${LINUX_RELEASE_VERSION}. Windows x86-64 remains on ${WINDOWS_RELEASE_VERSION}. Mac builds are signed and notarized; Windows is unsigned. Checksums are published.`,
      ],
    ],
  },
  {
    id: 'in-development',
    title: 'In development',
    description: 'Engineering work and validation still needed before we call it ready.',
    items: [
      [
        'Contributor reliability',
        'Connect the desktop contribution controls to a supervised contributor, validate real serving across machines, and complete sustained recovery and reconnect testing. This is not in the public desktop build.',
      ],
      [
        'Trusted circles and the open pool',
        'Turn the existing coordinator and routing pieces into reliable, permissioned shared inference. Coordinator dispatch code alone does not make the public network ready.',
      ],
      [
        'Intel Mac distribution',
        'Repair the native dependency and packaging path, then validate and notarize an actual Intel build. Intel downloads are paused.',
      ],
      [
        'Windows release parity',
        `Build and validate the ${RELEASE_VERSION} update on native Windows hardware. Keep the verified ${WINDOWS_RELEASE_VERSION} installer available meanwhile.`,
      ],
      [
        'Peer access and recovery',
        'Strengthen peer authentication, machine removal, and recovery behavior before extending trust beyond machines you own.',
      ],
    ],
  },
  {
    id: 'planned',
    title: 'On the map',
    description: 'Direction, not a release commitment. There are no promised launch dates.',
    items: [
      [
        'Contribution accounting',
        'Compute credits tied to useful work, with clear usage accounting and network pricing. Credit earning and redemption are not publicly launched.',
      ],
      [
        'Member governance',
        'A staged design for a network its members help govern, distinct from the founder-owned company. Any governance token, structure, or distribution remains subject to review.',
      ],
      [
        'Independent security review',
        'An external review of the mesh, coordinator, and privacy boundaries. Do not read internal testing as an independent audit.',
      ],
      [
        'More control over shared context',
        'Project-level memory access rules and better retrieval across multiple opted-in machines.',
      ],
      [
        'Source publication',
        'Publish application source and licensing details. The app repository is currently private; the downloadable models have their own licenses.',
      ],
      [
        'Larger models across machines',
        'Continue evaluating experimental split-model execution, with hardware, bandwidth, and latency limits stated plainly. It is not a general pooled-GPU guarantee.',
      ],
    ],
  },
] as const;

export default function RoadmapPage() {
  return (
    <article className="docs-page">
      <header className="docs-intro">
        <p className="eyebrow-mono">Build notes · Updated October 6, 2026</p>
        <h1>
          What is real.
          <br />
          What comes next.
        </h1>
        <p>
          Local AI and your own machine network are available now. The shared network is being built
          on that foundation. We keep the two separate here.
        </p>
      </header>
      <nav className="docs-contents" aria-label="Roadmap sections">
        {GROUPS.map((group) => (
          <a key={group.id} href={`#${group.id}`}>
            {group.title}
          </a>
        ))}
      </nav>
      {GROUPS.map((group, index) => (
        <section key={group.id} id={group.id} className="roadmap-group">
          <div className="roadmap-group-heading">
            <span className="eyebrow-mono">0{index + 1}</span>
            <h2>{group.title}</h2>
            <p>{group.description}</p>
          </div>
          <dl>
            {group.items.map(([title, description]) => (
              <div key={title}>
                <dt>{title}</dt>
                <dd>{description}</dd>
              </div>
            ))}
          </dl>
        </section>
      ))}
      <section className="docs-closing">
        <div>
          <h2>Try what is ready.</h2>
          <p>The installer page always lists the currently published version for each platform.</p>
        </div>
        <Link className="btn-primary" href="/install">
          Get Potluck ↗
        </Link>
      </section>
    </article>
  );
}
