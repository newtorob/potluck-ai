import type { Metadata } from 'next';
import Link from 'next/link';

export const metadata: Metadata = {
  title: 'Governance',
  description:
    'Potluck is built by a founder-owned company. Member governance of the future compute network is a separate, staged ambition, not ownership of the company.',
};

export default function GovernancePage() {
  return (
    <article className="docs-page">
      <header className="docs-intro">
        <p className="eyebrow-mono">Governance · Who is responsible for what</p>
        <h1>
          A company that builds.
          <br />A network people help shape.
        </h1>
        <p>
          Potluck is a founder-owned company building toward a member-operated compute network.
          Those are different things, and the distinction matters.
        </p>
      </header>
      <section className="docs-section">
        <span className="docs-section-number">01</span>
        <div>
          <h2>The company is accountable for the product.</h2>
          <p>
            Potluck is operated by Coshippr Inc., doing business as Potluck, as named in our{' '}
            <Link href="/terms">Terms of Service</Link> and{' '}
            <Link href="/privacy">Privacy Policy</Link>. The company builds and maintains the
            software and operates the services it provides.
          </p>
          <p>
            Using Potluck or contributing compute does not give you ownership of the company. The
            founder remains responsible for the product and commercial decisions.
          </p>
          <p>
            Our commitments include local-first operation, no sale of your data, and no training on
            it without explicit opt-in. The privacy policy explains the information needed for
            account and network services, and what stays on your device.
          </p>
        </div>
      </section>
      <section className="docs-section">
        <span className="docs-section-number">02</span>
        <div>
          <h2>Member governance is a direction we are building toward.</h2>
          <p>
            The long-term aim is for the people who power the shared network to help decide how it
            runs. That could include model availability, routing policy, contribution rules, and
            funding for shared infrastructure.
          </p>
          <p>
            These mechanisms are not live governance rights in the current desktop release. The
            company currently operates the coordinator and shared infrastructure. We should earn the
            description of a distributed, member-governed network through working systems and clear
            rules.
          </p>
          <aside className="docs-note">
            <strong>“No single owner” is the ambition for the network.</strong> It does not describe
            the ownership of the company, and it is not a claim that today&rsquo;s hosted
            infrastructure has no central operator.
          </aside>
        </div>
      </section>
      <section className="docs-section">
        <span className="docs-section-number">03</span>
        <div>
          <h2>Contribution should count.</h2>
          <p>
            People who provide useful compute should receive something for that work. The proposed
            compute-credit system would account for contribution and consumption separately from any
            governance mechanism.
          </p>
          <p>
            Public credit earning, redemption, and shared-network pricing are not launched. A
            possible governance token belongs to a later phase; its structure and any distribution
            are undecided and subject to legal review. Nothing here offers or promises a token,
            financial return, or allocation.
          </p>
          <p>
            You do not need a token to download Potluck or run it on your own machines. The local
            product stands on its own.
          </p>
        </div>
      </section>
      <section className="docs-closing">
        <div>
          <h2>The work is still in front of us.</h2>
          <p>
            See the capabilities you can use today and the network work still under development.
          </p>
        </div>
        <Link className="btn-outline" href="/roadmap">
          Read the roadmap ↗
        </Link>
      </section>
    </article>
  );
}
