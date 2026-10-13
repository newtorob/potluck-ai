import type { Metadata } from 'next';
import Link from 'next/link';
import { Reveal } from '@/components/motion';

export const metadata: Metadata = {
  title: 'Manifesto',
  description:
    'Everyone brings what they can. Nobody goes hungry. The founding document for Potluck AI.',
  openGraph: {
    title: 'Potluck AI manifesto: everyone brings what they can',
    description:
      "The founding document. Why the people who use AI should have a real stake in it. What we're building, what we're not, and what we believe.",
  },
};

export default function ManifestoPage() {
  return (
    <article className="mx-auto w-full max-w-[1160px] px-6 md:px-10 py-16">
      <div className="reading-width mx-auto">
        {/* Masthead: eyebrow-mono label, serif h1, italic epigraph, rail-rule close. */}
        <Reveal>
          <p className="eyebrow-mono mb-6">Founding document · MMXXVI</p>

          <h1 className="font-serif text-4xl font-light leading-[1.1] tracking-[-0.02em] text-foreground md:text-5xl">
            Potluck AI
          </h1>
          <p className="mt-3 font-serif text-xl font-light italic text-muted-foreground">
            Everyone brings what they can. Nobody goes hungry.
          </p>

          <div className="rail-rule mt-10" />
          <p className="docs-note">
            This is the direction we are building toward. For what you can use today, read the{' '}
            <Link href="/roadmap">current roadmap</Link>. Shared-network participation, credits, and
            governance are not launched.
          </p>
        </Reveal>

        <div className="mt-12 font-sans text-base leading-[1.78] text-muted-foreground space-y-5">
          {/* Opening */}
          <h2 className="font-serif text-2xl font-light text-foreground not-italic mt-10 mb-3">
            We think the people who use AI should have a real stake in it.
          </h2>

          <p>
            Right now, the most powerful technology of our lifetimes is being built by a small group
            of companies. Much of the time, the rest of us meet it through a login page and a
            monthly bill. We want running AI on your own hardware to be a practical choice, too.
          </p>

          <p>That is not the only way this can go.</p>

          {/* What we're building */}
          <Reveal>
            <hr className="border-border my-8" />
            <h2 className="font-serif text-2xl font-light text-foreground mt-2 mb-3">
              What we are building
            </h2>
          </Reveal>

          <p>
            Potluck AI is building a member-operated AI network, with a company that answers for the
            software and governed, over time, by the people who power it. Or, well, a potluck:
            everyone brings what they can, everyone eats, and nobody leaves hungry because they
            could not afford to host alone.
          </p>

          <p>
            <strong className="font-semibold text-foreground">Your AI runs on your device.</strong>{' '}
            We ship an app that runs models with downloadable weights locally. Your chat history is
            stored on your device. We do not use it to train models. No ads. The conversations you
            have with your AI stay on your computer unless you explicitly choose otherwise.
          </p>

          <p>
            <strong className="font-semibold text-foreground">
              When you need something bigger, the community has your back.
            </strong>{' '}
            A college student on a Chromebook should not be locked out of powerful AI because they
            cannot afford $200 a month. We are building a way for people with idle gaming PCs,
            workstations, and Apple Silicon Macs to contribute spare compute when they choose. Over
            time, that shared capacity helps deliver stronger models and better access to the people
            who need it.
          </p>

          <p>
            <strong className="font-semibold text-foreground">
              Here is the honest version of how the network protects you.
            </strong>{' '}
            The planned open pool uses blinded routing: the job sent to a contributor omits account
            identity. The serving machine still processes the prompt itself, and the content could
            identify you. Software rules about keeping requests in memory cannot prove a modified
            machine never records them. Cryptographic private inference is not shipped. The public
            contributor network is still in development; local inference and your own paired
            machines are the available starting point.
          </p>

          <p>
            <strong className="font-semibold text-foreground">
              Contributors are recognized for the work they do.
            </strong>{' '}
            The person whose 4090 sat idle all night helping other members deserves something for
            it. The proposed credit ledger would recognize useful compute and offset usage. Public
            earning and redemption are not launched. Credits are intended to be separate from
            governance. A governance token is planned for a later phase, subject to legal review,
            and we are not promising it to anyone. What stays constant: compute should not buy
            control of the network, and the richest member does not get the loudest voice.
          </p>

          <p>
            <strong className="font-semibold text-foreground">
              The people who power it should help govern it.
            </strong>{' '}
            Our aim is for the network to be governed over time by the people who keep it alive,
            across a bounded set of decisions: which models the network runs, routing and privacy
            standards, and how the shared treasury is spent. That voice is scoped so compute cannot
            buy control of the network. The rules should be public. The books should be transparent.
            Wherever possible, the technology should stay open enough that if we ever stop being
            worth it, you can leave with your setup intact.
          </p>

          <hr className="border-border my-8" />

          {/* How this is different */}
          <h2 className="font-serif text-2xl font-light text-foreground mt-2 mb-3">
            How this is different
          </h2>

          <p>
            There are token networks, GPU marketplaces, open-source communities, and venture-backed
            AI labs. Some are interesting. Some are useful. None of them are the thing we are trying
            to build.
          </p>

          <p>
            We are building a founder-led company whose whole purpose is to stand up a network with
            no single owner: member-operated compute, governed over time by the people who power it.
            The company builds and answers for the software. No single owner is the ambition for
            that network, not a claim about today's company-operated infrastructure.
          </p>

          <p>
            Concretely, that means four things under one roof, accountable to the same members: AI
            that runs on your own device, a memory layer that travels with you across the tools you
            already use, a contributor network that puts community compute behind larger models, and
            a governance structure that decides where any of it goes next. Plenty of organizations
            do one of those. None of them do all four, and none of them hand the network to the
            people who run it.
          </p>

          <hr className="border-border my-8" />

          {/* Why this, why now */}
          <h2 className="font-serif text-2xl font-light text-foreground mt-2 mb-3">
            Why this, why now
          </h2>

          <p>Three things frighten people about AI right now, and all three are real.</p>

          <p>
            <strong className="font-semibold text-foreground">
              Jobs get automated away and the value flows up.
            </strong>{' '}
            Potluck cannot stop automation, but a network owned by no single company can make sure
            that when AI becomes part of daily work, some of the value belongs to the people who
            actually power and use it.
          </p>

          <p>
            <strong className="font-semibold text-foreground">Surveillance gets normalized.</strong>{' '}
            Every prompt you type into a hosted chatbot can become a data point. Local-first AI is
            the clearest answer to that, and the models are finally good enough to make it
            practical.
          </p>

          <p>
            <strong className="font-semibold text-foreground">
              AI gets pointed at things we would never sign off on
            </strong>{' '}
            like ad targeting, military targeting, and political targeting, with no real say from
            the people it affects. A network with tens of thousands of members is a voice. A million
            is a constituency. That is how ordinary people stop being spectators.
          </p>

          <p>
            We do not think we are going to beat frontier models on a benchmark. We are not trying
            to. We are trying to build a dignified, durable piece of the AI landscape that the
            people who use it help power and govern.
          </p>

          <hr className="border-border my-8" />

          {/* What we're not */}
          <h2 className="font-serif text-2xl font-light text-foreground mt-2 mb-3">
            What we are not
          </h2>

          <p>
            We are not a pump. A governance token is a possibility for the network, planned for a
            later phase, but you never need it to use Potluck on your own machines, and the credit
            you earn for work cannot be traded or speculated on. We are not promising you will get
            rich. We are not claiming we will train a frontier model next year on volunteer laptops;
            the networking physics do not work yet, and we would rather be honest than hype.
          </p>

          <p>
            We are not a charity, either. Members pay dues, contributors earn rewards, and the whole
            thing should sustain itself on real value or it does not deserve to exist.
          </p>

          <p>
            We are not anti-AI. We love this technology. We just think more than five companies
            should get to decide what it becomes.
          </p>

          <hr className="border-border my-8" />

          {/* What we believe */}
          <h2 className="font-serif text-2xl font-light text-foreground mt-2 mb-5">
            What we believe
          </h2>

          <ol className="space-y-3 list-decimal pl-5">
            <li>AI should work for the people who use it, not the other way around.</li>
            <li>Privacy is a feature, not a premium tier.</li>
            <li>Access should not be gated by a $200 monthly subscription.</li>
            <li>The network should belong to the people who power it, not to any single owner.</li>
            <li>Contributions deserve rewards, but rewards should not buy power.</li>
            <li>Honesty about what we can and cannot do is worth more than hype.</li>
          </ol>

          <hr className="border-border my-8" />

          {/* What we need */}
          <h2 className="font-serif text-2xl font-light text-foreground mt-2 mb-3">What we need</h2>

          <p>We need the first hundred people.</p>

          <p>
            People who will pay a founding-member fee because they want this to exist, not because
            the product is finished. People with idle GPUs willing to help test the earliest version
            of a shared network. Lawyers who care about getting a member-governed network right.
            Designers who would rather ship a beautiful app for a small cause than a mediocre one
            for a big company. Moderators, writers, skeptics, critics.
          </p>

          <Reveal>
            <p>If you are reading this and something in your chest said yes, come build it.</p>

            <p className="font-serif text-xl font-light italic text-foreground mt-6">
              Reach out. Sign up. Tell a friend. Pull up a chair. This only works if we do it
              together, which is the whole point.
            </p>
          </Reveal>
        </div>

        {/* CTA at bottom of manifesto: plain link, not a glow button */}
        <div className="mt-14 pt-8 border-t border-border">
          <Link
            href="/#join"
            className="btn-primary focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 focus-visible:ring-offset-background"
          >
            Download Potluck
          </Link>
        </div>
      </div>
    </article>
  );
}
