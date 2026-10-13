import type { Metadata } from 'next';
import Link from 'next/link';
import { RELEASE_VERSION } from '@/lib/downloads';

export const metadata: Metadata = {
  title: 'How it works',
  description:
    'How Potluck runs open models locally, connects your own machines over WireGuard, and gives compatible agents persistent memory. Current capabilities and limits.',
};

export default function HowItWorksPage() {
  return (
    <article className="docs-page">
      <header className="docs-intro">
        <p className="eyebrow-mono">Inside Potluck · Desktop {RELEASE_VERSION}</p>
        <h1>
          One app.
          <br />
          Your machines, connected.
        </h1>
        <p>
          Potluck brings local inference, a private machine network, and persistent context into one
          desktop app. Here is what happens when you use it.
        </p>
      </header>
      <nav className="docs-contents" aria-label="On this page">
        <a href="#local">Local inference</a>
        <a href="#mesh">Your machines</a>
        <a href="#memory">Memory &amp; tools</a>
        <a href="#network">Shared network</a>
      </nav>

      <section className="docs-section" id="local">
        <span className="docs-section-number">01</span>
        <div>
          <h2>A model runs on your computer.</h2>
          <p>
            Pick a model from the library and download its weights. Potluck estimates what fits your
            hardware and verifies the download before installing it. Your CPU or GPU then generates
            the answer locally.
          </p>
          <p>
            A Potluck account is required to use the desktop app. After signing in and installing a
            model, you can keep using local inference offline with your saved account. Initial
            sign-in, model downloads, update checks, and network routes need a connection.
          </p>
          <aside className="docs-note">
            <strong>Local does not mean nothing is saved.</strong> Your chat history and memories
            are stored locally by the app. They are not sent to a hosted model for local inference.
            The model licenses vary, so check the license for a model before using it commercially.
          </aside>
        </div>
      </section>

      <section className="docs-section" id="mesh">
        <span className="docs-section-number">02</span>
        <div>
          <h2>The other computer can do the work.</h2>
          <p>
            Pair computers you own inside the app. Potluck includes a WireGuard mesh: an encrypted
            connection between those machines, with connection setup and diagnostics built in. You
            do not need a separate VPN account.
          </p>
          <ol>
            <li>Install Potluck on each machine and pair them using the in-app pairing flow.</li>
            <li>
              On the computer that will answer, load the model and enable serving from the Network
              view.
            </li>
            <li>
              Check that the machine is reachable and ready, then select{' '}
              <strong>My Machines</strong> as the routing scope in chat.
            </li>
          </ol>
          <p>
            The request can be served by an eligible paired machine with the requested model loaded.
            The model runs on the serving machine; ordinary routing does not merge the memory of
            every GPU into one pool.
          </p>
          <aside className="docs-note">
            <strong>Setup needs system permission.</strong> The mesh uses a background service and
            network adapter. Your operating system may ask for administrator approval. Direct
            connections are preferred; a relay can carry encrypted traffic when a direct path is
            unavailable. Restrictive networks can still need troubleshooting.
          </aside>
          <p>
            WireGuard protects traffic between the machines. The computer generating the answer
            necessarily processes the prompt in plaintext. Only pair machines you control and trust.
          </p>
        </div>
      </section>

      <section className="docs-section" id="memory">
        <span className="docs-section-number">03</span>
        <div>
          <h2>Your context survives the session.</h2>
          <p>
            The bundled Model Context Protocol (MCP) server lets compatible coding tools save and
            retrieve project facts, preferences, decisions, and active tasks. Your coding agent
            stays in the tool you use; Potluck supplies the memory.
          </p>
          <p>
            In <strong>Settings → Connect tools</strong>, choose a client and copy its generated
            configuration. Restart that client, then ask it to save a project fact. You can inspect
            and edit the result in Potluck&rsquo;s memory browser.
          </p>
          <p>
            Local chat selects relevant saved context. You can pin a memory to keep an applicable
            standing instruction in retrieval. Recognizable credential patterns are redacted, but
            the memory store is not a password manager.
          </p>
          <aside className="docs-note">
            <strong>Cross-machine memory is optional.</strong> When enabled on a machine, paired
            peers can search that machine&rsquo;s memory store through read-only peer endpoints.
            This is remote search, not automatic synchronization. Sharing is currently per machine;
            project-level access controls are not yet available.
          </aside>
          <p>
            The agent activity view displays tool-call metadata. It is an observation surface, not
            an agent runtime or a log of external agents&rsquo; raw prompts.
          </p>
        </div>
      </section>

      <section className="docs-section" id="network">
        <span className="docs-section-number">04</span>
        <div>
          <h2>A wider network is the next step.</h2>
          <p>
            The intended routing scopes are your machines, an invited trusted circle, and an open
            contributor pool. The current desktop release supports local use and your own paired
            machines. Trusted-circle inference and a dependable open contributor network are still
            in development.
          </p>
          <p>
            The coordinator code includes blinded job dispatch: account identity is left out of the
            job sent to a contributor. That is one part of the design, not proof that the full
            public service is ready.
          </p>
          <aside className="docs-note">
            <strong>Blinded routing has limits.</strong> A contributor must process prompt content
            to answer it. Removing account metadata cannot stop identifying information in the
            prompt from revealing who you are, nor prove that a modified contributor never records
            it. Cryptographic private inference is not shipped.
          </aside>
          <p>
            Contribution credits, shared-network pricing, and member governance are future work.
            They are not required to run the local app.{' '}
            <Link href="/roadmap">The roadmap tracks the remaining work.</Link>
          </p>
        </div>
      </section>

      <section className="docs-closing">
        <div>
          <h2>Start with one machine.</h2>
          <p>The install guide covers supported platforms, checksums, and first-run setup.</p>
        </div>
        <Link className="btn-primary" href="/install">
          Install Potluck ↗
        </Link>
      </section>
    </article>
  );
}
