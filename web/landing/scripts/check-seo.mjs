/** Read-only audit of rendered pages. Run against a production build or live site. */
import { JSDOM } from 'jsdom';
import { writeFile } from 'node:fs/promises';

const base = new URL(process.argv[2] || 'http://127.0.0.1:3100');
const canonicalOrigin = process.argv[3] || 'https://trypotluck.ai';
const output = process.argv[4];
const report = { checkedAt: new Date().toISOString(), base: base.origin, canonicalOrigin, pages: [], errors: [], warnings: [] };
const headers = { 'user-agent': 'PotluckSEOAuditBot/1.0' };
const request = (path, method = 'GET') => fetch(new URL(path, base), { method, headers, signal: AbortSignal.timeout(20000) });
function check(condition, message) { if (!condition) report.errors.push(message); }

try {
  const robots = await request('/robots.txt');
  const rules = await robots.text();
  check(robots.ok, 'robots.txt is unavailable');
  check(!/^Disallow:\s*\/\s*$/im.test(rules), 'robots.txt blocks the site');
  check(rules.includes(`Sitemap: ${canonicalOrigin}/sitemap.xml`), 'robots.txt must advertise the canonical sitemap');
  const sitemap = await request('/sitemap.xml');
  check(sitemap.ok, 'sitemap.xml is unavailable');
  const sitemapDom = new JSDOM(await sitemap.text(), { contentType: 'text/xml' });
  const entries = Array.from(sitemapDom.window.document.querySelectorAll('url > loc'), (node) => node.textContent);
  sitemapDom.window.close();
  check(entries.length > 0, 'Sitemap is empty');
  check(new Set(entries).size === entries.length, 'Sitemap contains duplicate URLs');
  check(entries.includes(`${canonicalOrigin}/blog`), 'Blog is absent from sitemap');
  const titles = new Map();
  const descriptions = new Map();
  for (const canonical of entries) {
    const url = new URL(canonical);
    check(url.origin === canonicalOrigin && !url.search && !url.hash, `Unexpected sitemap URL: ${canonical}`);
    const response = await request(url.pathname);
    const dom = new JSDOM(await response.text());
    const doc = dom.window.document;
    const title = doc.querySelector('title')?.textContent?.trim();
    const description = doc.querySelector('meta[name="description"]')?.content?.trim();
    const declared = Array.from(doc.querySelectorAll('link[rel="canonical"]'), (node) => node.href);
    const directives = `${response.headers.get('x-robots-tag') ?? ''} ${doc.querySelector('meta[name="robots"]')?.content ?? ''}`;
    const row = { path: url.pathname, status: response.status, title, description, canonical: declared, h1s: doc.querySelectorAll('h1').length };
    report.pages.push(row);
    check(response.status === 200 && !response.redirected, `${url.pathname}: expected 200 without redirect`);
    check(Boolean(title), `${url.pathname}: missing title`);
    check(Boolean(description), `${url.pathname}: missing description`);
    check(declared.length === 1 && new URL(declared[0]).href === url.href, `${url.pathname}: canonical missing or mismatched`);
    check(!/noindex|\bnone\b/i.test(directives), `${url.pathname}: indexing is blocked`);
    check(row.h1s === 1, `${url.pathname}: expected one main heading`);
    if (titles.has(title)) report.warnings.push(`${url.pathname}: same title as ${titles.get(title)}`);
    if (descriptions.has(description)) report.warnings.push(`${url.pathname}: same description as ${descriptions.get(description)}`);
    titles.set(title, url.pathname); descriptions.set(description, url.pathname);
    if (url.pathname.startsWith('/blog/')) {
      const schemas = Array.from(doc.querySelectorAll('script[type="application/ld+json"]'), (node) => JSON.parse(node.textContent));
      const article = schemas.find((item) => item['@type'] === 'BlogPosting');
      check(Boolean(article), `${url.pathname}: missing BlogPosting structured data`);
      if (article) {
        check(article['@context'] === 'https://schema.org' && article.mainEntityOfPage === url.href, `${url.pathname}: invalid schema identity`);
        for (const field of ['headline', 'description', 'image', 'datePublished', 'dateModified']) check(typeof article[field] === 'string' && article[field].length > 0, `${url.pathname}: missing schema ${field}`);
        check(article.headline === doc.querySelector('h1')?.textContent, `${url.pathname}: schema headline differs from visible title`);
        check(Boolean(article.author?.name), `${url.pathname}: missing schema author`);
        const image = new URL(article.image);
        check(image.origin === canonicalOrigin, `${url.pathname}: share image has unexpected origin`);
        const imageResponse = await request(image.pathname, 'HEAD');
        check(imageResponse.ok && imageResponse.headers.get('content-type')?.startsWith('image/'), `${url.pathname}: share image failed`);
      }
    }
    dom.window.close();
  }
  for (const path of ['/auth/device', '/auth/verify']) {
    const response = await request(path); const dom = new JSDOM(await response.text());
    check(/noindex/.test(dom.window.document.querySelector('meta[name="robots"]')?.content ?? ''), `${path}: account page must be noindex`);
    dom.window.close();
  }
  const missing = await request('/blog/seo-audit-unpublished-post');
  check(missing.status === 404, 'Unknown article must return 404');
} catch (error) { report.errors.push(String(error)); }

if (output) await writeFile(output, `${JSON.stringify(report, null, 2)}\n`);
console.log(JSON.stringify(report, null, 2));
process.exitCode = report.errors.length ? 1 : 0;
