import type { Metadata } from "next";
import Link from "next/link";
import "@/styles/careers.css";

export const metadata: Metadata = {
  title: "Careers",
  description:
    "Help shape a more personal future for AI. Explore roles in engineering, AI, sales, product, and leadership at Potluck.",
  alternates: { canonical: "/careers" },
  openGraph: {
    title: "Good work. Better together. | Potluck careers",
    description:
      "Explore the team we’re building and career opportunities at Potluck.",
    url: "/careers",
  },
  twitter: {
    title: "Good work. Better together. | Potluck careers",
    description:
      "Explore the team we’re building and career opportunities at Potluck.",
  },
};

const TEAMS = [
  {
    id: "engineering",
    name: "Engineering",
    description:
      "Make a network of everyday machines feel like something anyone can use.",
    roles: [
      {
        title: "Distributed Systems Engineer",
        focus: "Networking · Reliability · Infrastructure",
        work: "Build the connections that let work move reliably between machines. Think peer discovery, routing, recovery, and the awkward moments when a computer goes offline.",
        experience:
          "Experience with distributed systems, networking, or backend infrastructure. A habit of testing what happens when things break.",
      },
      {
        title: "Desktop Software Engineer",
        focus: "Desktop · Developer tools · Product",
        work: "Make local AI feel approachable, from installing a model to connecting another computer. Shape the desktop app, CLI, and the shared services underneath them.",
        experience:
          "Experience shipping desktop applications or developer tools, with care for performance, accessibility, and the details of a good setup experience.",
      },
    ],
  },
  {
    id: "ai",
    name: "AI & inference",
    description:
      "Get useful intelligence out of the hardware people already have.",
    roles: [
      {
        title: "AI Inference Engineer",
        focus: "Inference · Performance · Hardware",
        work: "Help models run well on real laptops, gaming PCs, and home servers. Improve model loading, memory use, hardware compatibility, and performance measurement.",
        experience:
          "Practical experience with model inference, GPU or CPU optimization, quantization, or benchmarking. Curiosity about the limits as well as the possibilities.",
      },
      {
        title: "Applied AI Engineer",
        focus: "Models · Evaluation · AI experiences",
        work: "Turn model capabilities into dependable everyday tools. Explore local reasoning, retrieval, tool use, and evaluations grounded in what people actually need.",
        experience:
          "Experience building with language models and evaluating their behavior. Good judgment about quality, latency, privacy, and when a simpler approach works.",
      },
    ],
  },
  {
    id: "growth",
    name: "Sales & partnerships",
    description:
      "Find the people this should exist for, and understand what they need.",
    roles: [
      {
        title: "Founding Sales Lead",
        focus: "Customer discovery · Sales · Growth",
        work: "Explore where Potluck can help individuals and teams. Build early customer relationships, understand real buying needs, and bring those lessons back into the product.",
        experience:
          "Experience introducing technical products to customers, listening carefully, and building a sales process from first principles.",
      },
      {
        title: "Partnerships & Community Lead",
        focus: "Ecosystem · Partnerships · Community",
        work: "Build relationships with developers, hardware communities, and people working on open models. Help turn shared interests into useful collaborations.",
        experience:
          "Experience nurturing technical communities or partnerships. Clear writing, thoughtful follow-through, and a feel for what makes participation worthwhile.",
      },
    ],
  },
  {
    id: "leadership",
    name: "Product & leadership",
    description:
      "Give ambitious work a clear direction, and help people do it well.",
    roles: [
      {
        title: "Product Manager",
        focus: "Product direction · Research · Delivery",
        work: "Connect user needs to the choices behind the product. Help decide what to build, make tradeoffs explicit, and keep delivery focused on useful outcomes.",
        experience:
          "Experience taking technical products from a problem to a working release. Comfort with user research, uncertain answers, and small teams.",
      },
      {
        title: "Engineering Manager",
        focus: "People · Technical direction · Delivery",
        work: "Help shape an engineering team that can own difficult problems together. Support clear decisions, sustainable delivery, and room for people to grow.",
        experience:
          "Experience supporting engineers and staying close to technical work. A thoughtful approach to feedback, planning, and building trust.",
      },
    ],
  },
] as const;

function introductionHref(role?: string) {
  const subject = role
    ? `Careers: ${role}`
    : "Careers: Introduction to Potluck";
  return `mailto:hello@trypotluck.ai?subject=${encodeURIComponent(subject)}`;
}

export default function CareersPage() {
  return (
    <div className="careers-page">
      <div className="careers-shell">
        <section className="careers-hero" aria-labelledby="careers-title">
          <div>
            <h1 id="careers-title">
              Good work.
              <br />
              <em>Better together.</em>
            </h1>
            <p className="careers-lead">
              Join us at Potluck. AI should feel closer to the people who use it.
              Help us build what comes next.
            </p>
            <div className="careers-actions">
              <a className="careers-button" href="#roles">
                Explore roles <span aria-hidden="true">↓</span>
              </a>
              <a className="careers-text-link" href="#say-hello">
                Introduce yourself <span aria-hidden="true">↗</span>
              </a>
            </div>
          </div>
          <aside className="careers-note" aria-label="The idea behind Potluck">
            <span className="careers-note-star" aria-hidden="true">
              ✳
            </span>
            <p className="careers-note-title">
              Everyone brings
              <br />
              what they can.
            </p>
            <p>
              A different skill. A useful question. A stubborn belief that
              things could work better.
            </p>
            <span className="careers-note-signature">
              There’s a place for that here.
            </span>
          </aside>
        </section>

        <section
          className="careers-mission"
          aria-labelledby="careers-mission-title"
        >
          <div>
            <h2 id="careers-mission-title">
              Small machines.
              <br />
              <em>A bigger idea.</em>
            </h2>
          </div>
          <div>
            <p>
              Potluck runs open models on the hardware people own. We’re
              building toward a member-operated compute network, where more
              people have a part in powering AI.
            </p>
            <p>
              That takes careful engineering, useful products, and people who
              can connect the work to the world outside it. We’re looking for
              people who want to help build it.
            </p>
            <Link className="careers-text-link" href="/manifesto">
              Read the idea behind Potluck <span aria-hidden="true">↗</span>
            </Link>
          </div>
        </section>

        <section
          className="careers-opportunities"
          id="roles"
          aria-labelledby="careers-roles-title"
        >
          <div className="careers-section-heading">
            <div>
              <h2 id="careers-roles-title">The team we’re building.</h2>
            </div>
            <p>
              Explore the work. Find your corner.
              <br />
              Tell us where you’d like to contribute.
            </p>
          </div>
          <nav className="careers-teams" aria-label="Explore career areas">
            {TEAMS.map((team) => (
              <a key={team.id} href={`#${team.id}`}>
                {team.name} <span aria-hidden="true">↓</span>
              </a>
            ))}
          </nav>
          {TEAMS.map((team) => (
            <section
              className="careers-team"
              key={team.id}
              id={team.id}
              aria-labelledby={`${team.id}-title`}
            >
              <div className="careers-team-intro">
                <h3 id={`${team.id}-title`}>{team.name}</h3>
                <p>{team.description}</p>
              </div>
              <div className="careers-role-list">
                {team.roles.map((role) => (
                  <details className="careers-role" key={role.title}>
                    <summary>
                      <span className="careers-role-summary">
                        <span className="careers-role-title">{role.title}</span>
                        <span className="careers-role-focus">{role.focus}</span>
                      </span>
                      <span className="careers-role-toggle" aria-hidden="true">
                        +
                      </span>
                    </summary>
                    <div className="careers-role-body">
                      <h4>The work</h4>
                      <p>{role.work}</p>
                      <h4>What you might bring</h4>
                      <p>{role.experience}</p>
                      <a
                        className="careers-text-link"
                        href={introductionHref(role.title)}
                        aria-label={`Introduce yourself for the ${role.title} role`}
                      >
                        Introduce yourself <span aria-hidden="true">↗</span>
                      </a>
                    </div>
                  </details>
                ))}
              </div>
            </section>
          ))}
        </section>

        <section className="careers-principles" aria-label="What matters to us">
          <div>
            <h2>Useful beats impressive.</h2>
            <p>
              Start with something that makes a person’s day better. Make the
              complicated parts feel simple.
            </p>
          </div>
          <div>
            <h2>Leave room for a better idea.</h2>
            <p>
              Ask good questions. Test assumptions. Let what you learn change
              what you build.
            </p>
          </div>
          <div>
            <h2>Keep people in control.</h2>
            <p>
              Respect the person, their hardware, and their choices. Build trust
              into the details.
            </p>
          </div>
        </section>

        <section
          className="careers-contact"
          id="say-hello"
          aria-labelledby="careers-contact-title"
        >
          <h2 id="careers-contact-title">
            Your kind of thing?
            <br />
            <em>Pull up a chair.</em>
          </h2>
          <p>
            Tell us what you care about, where you could help, and something
            you’re proud of. A short note or a link to your work is a good place
            to start.
          </p>
          <a className="careers-button" href={introductionHref()}>
            Say hello <span aria-hidden="true">↗</span>
          </a>
          <p className="careers-contact-address">hello@trypotluck.ai</p>
        </section>
      </div>
    </div>
  );
}
