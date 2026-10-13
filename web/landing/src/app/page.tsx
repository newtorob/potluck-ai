import type { Metadata } from 'next';
import Link from 'next/link';
import { Neighborhood } from '@/components/Neighborhood';
import { DownloadButtons } from '@/components/DownloadButtons';
import { CommercialFilm } from '@/components/CommercialFilm';
import { DISCORD_INVITE } from '@/lib/downloads';
import '@/styles/landing.css';

export const metadata: Metadata = {
  title: { absolute: 'Potluck | Good machines. Better together.' },
  description:
    'Run open models on your own hardware. Connect your laptop, desktop, and home server with Potluck. Local AI today, a member-operated compute network in the making.',
};

const QUESTIONS = [
  {
    question: 'Do I need more than one computer?',
    answer:
      'No. Start with local chat and a model that fits your machine. Connecting another computer is optional. You can add it later, without changing how you work.',
  },
  {
    question: 'Does my prompt leave my laptop?',
    answer:
      'In local mode, inference stays on that machine. When you select My Machines, your prompt goes to a paired computer over the encrypted mesh so that computer can answer it. You choose the routing scope.',
  },
  {
    question: 'Does this combine all my GPUs into one?',
    answer:
      'The everyday path sends a request to a machine that can run the model. Splitting a single model across machines is experimental; it is not a promise of pooled GPU memory or datacenter performance.',
  },
  {
    question: 'Can I use the public network today?',
    answer:
      'Trusted-circle routing and the open contributor pool are still in development. The current desktop release is for local inference and your own paired machines. You can follow the next steps on the roadmap.',
  },
  {
    question: 'What does it cost?',
    answer:
      'The desktop app is free to download and run locally. Your hardware, electricity, and connectivity are yours to provide. Shared-network pricing and contribution credits are not launched.',
  },
  {
    question: 'Is Potluck open source?',
    answer:
      'Potluck runs models with downloadable weights; their licenses vary by model. The Potluck application source is not publicly released yet. We will publish source and licensing details when that changes.',
  },
];

export default function HomePage() {
  return (
    <div className="potluck-home">
      <Neighborhood />

      <section id="film" className="home-wrap home-film" aria-labelledby="film-heading">
        <div className="home-section-intro">
          <p className="home-kicker">The Potluck film · 30 seconds</p>
          <h2 id="film-heading">
            Bring what you&rsquo;ve got.
            <br />
            <em>Build something bigger.</em>
          </h2>
          <p>
            From the computers at home to friends across the country, a glimpse of the network
            we&rsquo;re building. More people bringing more ideas to life, together.
          </p>
        </div>
        <CommercialFilm />
        <div className="home-film-caption">
          <p>
            Concept screens show the vision. Trusted circles, the open pool, and model splitting
            are still in development.
          </p>
          <Link className="home-text-link" href="/roadmap">
            Explore the roadmap <span aria-hidden="true">↗</span>
          </Link>
        </div>
        <details className="home-film-transcript">
          <summary>Read the film transcript</summary>
          <p>
            It starts with an idea. Connect the computers around you. At home. At work. With
            friends. Across the country. Together, power larger AI models. Bring your ideas to
            life. Potluck. Bring what you&rsquo;ve got. Build something bigger.
          </p>
        </details>
      </section>

      <section id="your-machines" className="home-paper" aria-labelledby="machines-heading">
        <div className="home-wrap home-story-grid">
          <div className="home-story-heading">
            <p className="home-kicker">The hardware is already here</p>
            <h2 id="machines-heading">
              A useful little
              <br />
              network of
              <br />
              <em>your own.</em>
            </h2>
            <p>One app for the machines you have. Room to grow when you need it.</p>
            <Link href="/how-it-works" className="home-text-link">
              How the pieces fit <span aria-hidden="true">↗</span>
            </Link>
          </div>
          <div className="home-use-cases">
            <article>
              <span className="home-index">01 / ON YOUR MACHINE</span>
              <h3>Keep everyday work close.</h3>
              <p>
                Draft, think, ask, and code with a model running locally. Choose a model that fits
                your hardware, download it, and get to work. Once it is installed, local inference
                does not need an internet connection.
              </p>
              <span className="home-detail">Your model. Your CPU or GPU.</span>
            </article>
            <article>
              <span className="home-index">02 / ACROSS YOUR MACHINES</span>
              <h3>Let the desktop do the lifting.</h3>
              <p>
                Stay on your laptop while a paired computer runs a model it has loaded. Potluck
                brings the WireGuard connection into the app, including the setup and readiness
                checks.
              </p>
              <span className="home-detail">A private mesh. No separate VPN account.</span>
            </article>
            <article>
              <span className="home-index">03 / BETWEEN YOUR TOOLS</span>
              <h3>Pick up where you left off.</h3>
              <p>
                Give compatible coding agents a local memory for project facts, decisions, and
                unfinished tasks through MCP. The context you save stays available when the session
                ends or you switch tools.
              </p>
              <span className="home-detail">Useful on day one, even with one machine.</span>
            </article>
          </div>
        </div>
      </section>

      <section className="home-wrap home-network" aria-labelledby="network-heading">
        <div className="home-section-intro">
          <p className="home-kicker">Where this is going</p>
          <h2 id="network-heading">
            Start at home.
            <br />
            <em>Build something bigger.</em>
          </h2>
          <p>
            Potluck is building toward a member-operated compute network. People bring the hardware.
            Every request has a boundary you choose.
          </p>
        </div>
        <ol className="home-scope-list">
          <li>
            <span className="scope-number">01</span>
            <div>
              <h3>Your machines</h3>
              <p>
                Local inference and your own paired computers. The foundation you can use today.
              </p>
            </div>
            <span className="scope-status">Available today</span>
          </li>
          <li>
            <span className="scope-number">02</span>
            <div>
              <h3>Trusted circle</h3>
              <p>
                An invited group sharing capacity, with explicit access rules. The next boundary to
                open.
              </p>
            </div>
            <span className="scope-status planned">In development</span>
          </li>
          <li>
            <span className="scope-number">03</span>
            <div>
              <h3>Open pool</h3>
              <p>
                A wider network of contributors, with opt-in routing and clear rules for the work
                they serve.
              </p>
            </div>
            <span className="scope-status planned">In development</span>
          </li>
        </ol>
        <div className="home-network-note">
          <p>
            The company builds the software. The ambition is a network powered and governed by its
            members. We publish the current limits alongside the vision.
          </p>
          <Link className="home-text-link" href="/roadmap">
            See what is built <span aria-hidden="true">↗</span>
          </Link>
        </div>
      </section>

      <section className="home-evidence" aria-labelledby="evidence-heading">
        <div className="home-wrap home-evidence-grid">
          <div>
            <p className="home-kicker">The details matter</p>
            <h2 id="evidence-heading">
              Curiosity welcome.
              <br />
              <em>So is scrutiny.</em>
            </h2>
            <p>
              Model speed depends on your hardware. A private connection is different from private
              inference. A roadmap is different from a release. Here is the detail behind the page.
            </p>
          </div>
          <div className="home-reading-list">
            <Link href="/benchmarks">
              <span className="home-index">LAB NOTES</span>
              <h3>Measured, with the limits attached.</h3>
              <p>Hardware, model sizes, retrieval results, and the runs that were too slow.</p>
              <span className="reading-arrow" aria-hidden="true">
                ↗
              </span>
            </Link>
            <Link href="/how-it-works">
              <span className="home-index">UNDER THE HOOD</span>
              <h3>Follow a request through the system.</h3>
              <p>Local inference, the encrypted mesh, and the boundary around shared compute.</p>
              <span className="reading-arrow" aria-hidden="true">
                ↗
              </span>
            </Link>
            <Link href="/manifesto">
              <span className="home-index">THE REASON FOR ALL THIS</span>
              <h3>Everyone brings what they can.</h3>
              <p>Why we want more people to have a say in where AI goes next.</p>
              <span className="reading-arrow" aria-hidden="true">
                ↗
              </span>
            </Link>
          </div>
        </div>
      </section>

      <section className="home-wrap home-faq" aria-labelledby="faq-heading">
        <div>
          <p className="home-kicker">A few fair questions</p>
          <h2 id="faq-heading">
            Before you
            <br />
            <em>pull up a chair.</em>
          </h2>
        </div>
        <div className="home-questions">
          {QUESTIONS.map(({ question, answer }, index) => (
            <details key={question}>
              <summary>
                <span className="faq-number">0{index + 1}</span>
                {question}
                <span className="faq-toggle" aria-hidden="true">
                  +
                </span>
              </summary>
              <p>
                {answer}
                {question === 'Can I use the public network today?' && (
                  <>
                    {' '}
                    <Link href="/roadmap">Read the roadmap →</Link>
                  </>
                )}
              </p>
            </details>
          ))}
        </div>
      </section>

      <section id="join" className="home-join" aria-labelledby="join-heading">
        <div className="home-wrap">
          <p className="home-kicker">There is a seat for you</p>
          <h2 id="join-heading">
            Bring a machine.
            <br />
            <em>See what happens.</em>
          </h2>
          <p>
            Start with something useful on your own computer.
            <br />
            Help us build the network from there.
          </p>
          <DownloadButtons />
          <a
            className="home-text-link"
            href={DISCORD_INVITE}
            target="_blank"
            rel="noopener noreferrer"
          >
            Come talk with us on Discord <span aria-hidden="true">↗</span>
          </a>
        </div>
      </section>
    </div>
  );
}
