import type { Metadata } from 'next';
import { PageIntro } from '@/components/PageIntro';
import Link from 'next/link';
import { RELEASE_VERSION } from '@/lib/downloads';

export const metadata: Metadata = {
  alternates: { canonical: "/how-it-works" },
  title: 'How it works',
  description:
    'How Potluck splits one model across computers you own, with verified placement and explicit worker choice. Learn what is tested and what current downloads support.',
};

export default function HowItWorksPage() {
  return (
    <article className="docs-page">
      <PageIntro title={<>
          One model.
          <br />
          Your machines, working together.
        </>} detail={<>Inside Potluck · Desktop {RELEASE_VERSION}</>}>
          Potluck is built to spread one model across computers you own, letting them contribute
          memory and compute to the same answer. Here is how splitting works, what we have tested,
          and what the current downloads support.
        </PageIntro>
      <nav className="docs-contents" aria-label="On this page">
        <a href="#model-splitting">One model, several machines</a>
        <a href="#local">Local inference</a>
        <a href="#mesh">Route to one machine</a>
        <a href="#memory">Memory &amp; tools</a>
        <a href="#network">Shared network</a>
      </nav>

      <section className="docs-section" id="model-splitting">
        <span className="docs-section-number">01</span>
        <div>
          <h2>Several computers run parts of the same model.</h2>
          <p>
            A larger model needs room for its weights: the data it uses to generate an answer.
            Potluck can place parts of those weights on different computers and connect their
            work over your private machine network. Each selected computer contributes to the
            same inference.
          </p>
          <ol>
            <li>
              Choose the main computer, called the head. It keeps the complete model file and
              handles your requests.
            </li>
            <li>
              Choose one to three other computers you own as workers. Enable sharing on each;
              they receive their assigned weights without downloading a separate model file.
            </li>
            <li>
              Check the available memory, then start the split. Potluck verifies that every
              selected worker received model weights before marking the model ready.
            </li>
          </ol>
          <p>
            The computers need compatible split engines and must stay connected. Free memory,
            model size, and context length determine placement. Some weights may stay on the
            main computer&rsquo;s CPU. Splitting adds capacity; response speed still depends on
            the hardware and network. If a worker disconnects, restore it and start the split again.
          </p>
          <aside className="docs-note">
            <strong>What you can use today.</strong> Linux CLI 0.1.7 includes a CPU split engine.
            Our tested 32B model across a Linux GPU and a Mac used development builds. Mac CLI
            0.1.6 does not include the split engine, and split setup is not yet enabled in the
            desktop app. The current Linux GPU download accelerates models running on one
            computer; its split engine still uses the CPU.
          </aside>
          <p>
            <Link href="/benchmarks#model-splitting">See the measured two-computer configuration</Link>,
            including exactly where the model weights ran.
          </p>
        </div>
      </section>

      <section className="docs-section" id="local">
        <span className="docs-section-number">02</span>
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
        <span className="docs-section-number">03</span>
        <div>
          <h2>You can also send a request to one computer.</h2>
          <p>
            The desktop app also supports a simpler workflow: one paired computer runs the entire
            model and answers a request from another. Potluck includes a WireGuard mesh: an
            encrypted connection between those machines, with connection setup and diagnostics
            built in. You do not need a separate VPN account.
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
            This routing workflow uses one serving machine. To spread the same model across
            machines, use the separate splitting workflow described above.
          </p>
          <aside className="docs-note">
            <strong>Setup needs system permission.</strong> The mesh uses a background service and
            network adapter. Your operating system may ask for administrator approval. Direct
            connections are preferred; a relay can carry encrypted traffic when a direct path is
            unavailable. Restrictive networks can still need troubleshooting.
          </aside>
          <p>
            <Link href="/blog/run-a-local-llm-on-another-computer">Follow the two-machine setup guide</Link> for the complete walkthrough.
          </p>
          <p>
            WireGuard protects traffic between the machines. The computer generating the answer
            necessarily processes the prompt in plaintext. Only pair machines you control and trust.
          </p>
        </div>
      </section>

      <section className="docs-section" id="memory">
        <span className="docs-section-number">04</span>
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
        <span className="docs-section-number">05</span>
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
