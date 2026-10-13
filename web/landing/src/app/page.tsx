import type { Metadata } from 'next';
import Link from 'next/link';
import Image from 'next/image';
import { Arrow } from '@/components/Arrow';
import { BlogArtwork } from '@/components/BlogArtwork';
import { SOURCE_URL, LICENSE_URL, OPEN_SOURCE_ARTICLE } from '@/lib/openSource';
import { ModelSplit } from '@/components/ModelSplit';
import { CommercialFilm } from '@/components/CommercialFilm';
import { DownloadButtons } from '@/components/DownloadButtons';
import { getAllPosts } from '@/lib/blog';
import { RELEASE_VERSION, LINUX_RELEASE_VERSION, WINDOWS_RELEASE_VERSION, DISCORD_INVITE } from '@/lib/downloads';
import '@/styles/landing.css';

export const metadata: Metadata = {
  title: { absolute: 'Potluck | Bigger models. Same computers.' },
  description: 'Open-source AI for the computers you already own. Explore Potluck’s model-splitting work, tested configurations, and local AI downloads. MIT licensed.',
  alternates: { canonical: '/' },
  openGraph: { title: 'Bigger models. Same computers. | Potluck', description: 'Open-source AI on your own machines. Explore model splitting, see what we’ve tested, and build with us on GitHub.', url: '/' },
  twitter: { title: 'Bigger models. Same computers. | Potluck', description: 'Open-source AI on your own machines. Explore model splitting, see what we’ve tested, and build with us on GitHub.' },
};

const QUESTIONS = [
  { question: 'Is Potluck open source?', answer: <>Yes. Potluck’s source is public on <a href={SOURCE_URL}>GitHub</a> under the <a href={LICENSE_URL}>MIT license</a>. You can inspect it, build on it, and contribute. Model weights and third-party components keep their own licenses.</> },
  { question: 'Is this one model running on several computers?', answer: 'Yes. In a split setup, selected computers hold different parts of the same model and execute them together. That is different from sending the whole request to another computer that holds the entire model.' },
  { question: 'Can I try model splitting today?', answer: 'Managed model splitting is experimental. Linux CLI 0.1.7 includes a CPU split engine. The tested Mac + Linux GPU setup uses development builds; it is not yet an end-to-end feature in the public desktop app. Every participant needs a compatible split runtime.' },
  { question: 'Can I just add up the RAM in my computers?', answer: 'That is a starting point, not a fit guarantee. Each machine needs room for its operating system and working buffers. Model format, context length, GPU memory, and backend support all affect what can be loaded. Potluck checks the selected machines and reports where model weights actually land.' },
  { question: 'Will more computers make it faster?', answer: 'Not necessarily. Splitting is a way to get more capacity. Speed also depends on how the model is divided, which parts run on CPUs or GPUs, and the connection between computers. More machines can add communication overhead.' },
  { question: 'Can I still use just one computer?', answer: 'Yes. The desktop app can run a model locally on one computer. It is free to download and run locally, and requires an account. Connecting your own machines does not enroll them in a public contribution pool.' },
];

function RouteMark() {
  return <svg className="room-route-mark" viewBox="0 0 210 110" fill="none" aria-hidden="true"><rect x="8" y="18" width="66" height="46" rx="5"/><path d="M8 64 1 75h80l-7-11M30 75h24"/><rect x="151" y="9" width="46" height="74" rx="6"/><path d="M162 22h24m-24 9h24m-24 9h24M94 43h41m-8-7 8 7-8 7M135 63H94m7-7-7 7 7 7"/><circle cx="174" cy="69" r="4"/></svg>;
}

export default function HomePage() {
  const latest = getAllPosts()[0];
  return <div className="vivid-home">
    <aside className="home-source-announcement" aria-label="Open-source announcement"><div className="home-wrap"><p><strong>Potluck is now open source.</strong> MIT licensed.</p><Link href={OPEN_SOURCE_ARTICLE}>Why we opened the code <Arrow /></Link></div></aside>
    <section className="vivid-hero" aria-labelledby="home-title">
      <div className="vivid-hero-copy home-wrap">
        <h1 id="home-title">Bigger models.<br /><span>Same computers.</span></h1>
        <div className="vivid-hero-intro"><p>Your laptop. Your gaming PC. That spare desktop. Potluck splits one AI model across your machines, making room for something bigger.</p>
          <div className="room-hero-actions"><a href="#sharing" className="btn-primary">See how it works <Arrow /></a><a href="#the-film" className="room-watch-link"><svg width="12" height="14" viewBox="0 0 12 14" fill="currentColor" aria-hidden="true"><path d="M1 1v12l10-6z" /></svg>Watch the film</a></div>
        </div>
      </div>
      <div className="vivid-hardware home-wrap"><Image src="/images/potluck-computers-vivid.webp" width={2057} height={765} alt="An illustrated group of everyday computers: two laptops, a gaming PC, and a desktop." sizes="(max-width: 700px) 100vw, 1320px" priority /><div className="vivid-hardware-caption"><span>Individually, your computers.<br /><strong>Together, more room for AI.</strong></span><a href="#sharing" aria-label="Explore how the model is shared"><Arrow /></a></div></div>
      <p className="vivid-availability home-wrap">Model splitting is experimental. The tested Mac + Linux GPU setup uses development builds.</p>
    </section>

    <section id="sharing" className="vivid-sharing room-section" aria-labelledby="sharing-heading">
      <div className="home-wrap vivid-sharing-heading"><h2 id="sharing-heading">Too big for one?<br /><span>Bring another.</span></h2><p>A model doesn’t have to live on a single computer. Give each machine a part, and let them work on the same answer.</p></div>
      <ModelSplit />
    </section>

    <section className="room-get-started home-wrap room-section" aria-labelledby="start-heading">
      <h2 id="start-heading" className="sr-only">How the work is shared</h2>
      <ol className="room-steps">
        <li><span className="room-step-number" aria-hidden="true">1</span><div><h3>Bring the machines you have.</h3><p>Connect computers you own through Potluck’s private mesh. Choose which ones take part in the split and opt them in.</p></div></li>
        <li><span className="room-step-number" aria-hidden="true">2</span><div><h3>Give each one a share.</h3><p>One computer keeps the model file. Potluck assigns parts of its weights to the participating machines, using the memory they have available.</p></div></li>
        <li><span className="room-step-number" aria-hidden="true">3</span><div><h3>Run one model together.</h3><p>The machines execute their parts of the same inference. You get one answer from one model, with the work spread across your setup.</p></div></li>
      </ol>
      <div className="room-tools-note"><p>More capacity is the point. Speed depends on the model, free memory, hardware, and the connection between machines.</p><Link href="/how-it-works#model-splitting" className="text-action">How the split works <Arrow /></Link></div>
    </section>

    <section className="vivid-proof" aria-labelledby="split-proof-title"><div className="home-wrap room-section">
      <div className="vivid-proof-heading"><h2 id="split-proof-title">One 32B model.<br /><span>A PC and a Mac.</span></h2><div><p>We’ve run Qwen2.5-Coder 32B across an RTX 2080 Ti Linux PC and an M2 Pro Mac. Both held model weights. Both helped generate the answer.</p><Link href="/benchmarks#model-splitting" className="text-action">See the configuration and results <Arrow /></Link></div></div>
      <div className="vivid-placement"><h3>Where the model actually ran.</h3><div className="vivid-placement-bar" aria-hidden="true"><span style={{ flex: 6.23 }} /><span style={{ flex: 7.58 }} /><span style={{ flex: 1.03 }} /></div><dl><div><dt><i />Linux PC · CPU</dt><dd>6.23 GiB</dd></div><div><dt><i />Linux PC · GPU</dt><dd>7.58 GiB</dd></div><div><dt><i />Mac worker</dt><dd>1.03 GiB</dd></div></dl><p>Measured model-weight placement · Q3_K_M · September 29, 2026</p></div>
      <p className="vivid-proof-note">Development builds. Some weights remained on the PC’s CPU. This proves a model ran across both machines; it does not establish a speedup or that neither could run it alone.</p>
    </div></section>

    <section id="the-film" className="room-film-section" aria-labelledby="film-heading">
      <div className="home-wrap room-section">
        <div className="room-film-heading"><h2 id="film-heading">A bigger idea.<br /><span>A little film.</span></h2><div><p>Start with the computers you already own. Then imagine what becomes possible when more people can bring their machines to the same network.</p><Link href="/manifesto" className="text-action">Why we’re building Potluck <Arrow /></Link></div></div>
        <CommercialFilm />
        <div className="room-film-caption"><p>A 30-second concept film about where we’re headed.</p><details className="home-film-transcript"><summary>Read the transcript</summary><p>It starts with an idea. Connect the computers around you. At home. At work. With friends. Across the country. Together, power larger AI models. Bring your ideas to life. Potluck. Bring what you’ve got. Build something bigger.</p></details></div>
        <div className="room-network-stages"><div><h3>Your machines</h3><p><span className="room-availability">Start here</span>Local AI today. Managed model splitting in development.</p></div><div><h3>A trusted circle</h3><p><span className="room-availability">In development</span>An invited group sharing capacity.</p></div><div><h3>An open pool</h3><p><span className="room-availability">In development</span>A wider network of contributing members.</p></div></div>
        <div className="room-future-note"><p>Trusted circles and the open pool are the next step beyond your own machines.</p><Link href="/roadmap" className="text-action">Follow the roadmap <Arrow /></Link></div>
      </div>
    </section>

    <section className="room-resources home-wrap room-section" aria-labelledby="setup-heading">
      <div className="room-resources-heading"><h2 id="setup-heading">Start with what<br />you already have.</h2><p>Get Potluck, connect your machines, and follow the work on larger models. The desktop app also runs models locally on one computer.</p></div>
      <div className="room-resource-columns">
        <div className="room-platform-list"><h3>Download the desktop app.</h3><Link href="/install"><span>Mac<small>Apple Silicon · Metal</small></span><span>{RELEASE_VERSION}<Arrow /></span></Link><Link href="/install"><span>Windows<small>Windows 10+ · unsigned installer</small></span><span>{WINDOWS_RELEASE_VERSION}<Arrow /></span></Link><Link href="/install"><span>Linux<small>x86-64 · Vulkan + CPU fallback</small></span><span>{LINUX_RELEASE_VERSION}<Arrow /></span></Link><p className="room-platform-note">Linux GPU support validated on NVIDIA RTX 2080 Ti and Intel integrated graphics. AMD validation is pending. Intel Mac downloads are paused.</p><Link href="/benchmarks" className="text-action">Read the measurements <Arrow /></Link></div>
        {latest && <Link href={'/blog/' + latest.slug} className="room-guide">{latest.diagram === 'open-source' ? <BlogArtwork kind={latest.diagram} /> : <div className="room-guide-art"><RouteMark /><span>From the Potluck blog.</span></div>}<div className="room-guide-copy"><h3>{latest.title}</h3><p>{latest.description}</p><span className="text-action">Read the article <Arrow /></span></div></Link>}
      </div>
      <div className="room-blog-link"><p>Notes from building and using Potluck.</p><Link href="/blog" className="text-action">More from the blog <Arrow /></Link></div>
    </section>

    <section className="room-faq-section" aria-labelledby="faq-heading"><div className="home-wrap room-section room-faq"><div><h2 id="faq-heading">A few things<br />worth knowing.</h2><p>Something else on your mind?</p><a href={DISCORD_INVITE} className="text-action" target="_blank" rel="noopener noreferrer">Ask us on Discord <Arrow /></a></div><div className="home-questions">{QUESTIONS.map(({ question, answer }) => <details key={question}><summary>{question}<svg className="faq-toggle" width="18" height="18" viewBox="0 0 18 18" fill="none" aria-hidden="true"><path d="M3 9h12M9 3v12" stroke="currentColor" strokeWidth="1.4" /></svg></summary><p>{answer}</p></details>)}</div></div></section>

    <section id="join" className="home-join" aria-labelledby="join-heading"><div className="home-wrap"><h2 id="join-heading">Bring your computers.<br /><span>Think bigger.</span></h2><p>Start with local AI. Follow the work on running larger models together.</p><DownloadButtons /><Link href="/install" className="text-action">Requirements and install instructions <Arrow /></Link><p className="room-account-note">Free to download and run locally. Account required.</p></div></section>
  </div>;
}
