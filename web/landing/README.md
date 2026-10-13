# Potluck AI: landing site

The website for [trypotluck.ai](https://trypotluck.ai). Next.js 15 (App Router) + TypeScript + Tailwind CSS.

## Stack

- **Framework:** Next.js 15 (App Router)
- **Language:** TypeScript (strict mode, `noUncheckedIndexedAccess`)
- **Styling:** Tailwind CSS with Hearth theme tokens matching `apps/desktop/frontend/`
- **Fonts:** self-hosted in `public/fonts/` and declared in `src/styles/fonts.css`: Newsreader (headings and serif text), Familjen Grotesk (body), Spline Sans Mono (code). See `public/fonts/README.md` for provenance and licenses.
- **Tests:** vitest
- **Hosting:** Vercel (`vercel.json` sets the framework, build commands and security headers)

## Run locally

```bash
cd web/landing
pnpm install
pnpm dev       # localhost:3000
```

## Build and test

```bash
pnpm build       # production build
pnpm start       # preview production build locally
pnpm test        # vitest
pnpm typecheck   # tsc --noEmit
pnpm lint        # eslint
```

From the repo root, `make test-landing` runs the vitest suite, the typecheck and a production build (`LANDING_BUILD=0` skips the build).

## Blog

Articles live in `src/content/blog/` as Markdown with JSON front matter. Set
`draft: false` to publish. Each article gets a canonical URL, BlogPosting schema,
a share image and a sitemap entry. Use `diagram: "open-source"` for the source
announcement artwork and GitHub action. Source links live in `src/lib/openSource.ts`.

Run `pnpm seo:check https://trypotluck.ai` after a production deployment to check
page status, canonical URLs, headings, indexing directives, metadata and share
images. Production deployments are manual.

## Pages

| Route | File | Purpose |
|---|---|---|
| `/` | `src/app/page.tsx` | Home page: model splitting, measured configurations, product film, blog, open-source announcement and downloads |
| `/how-it-works` | `src/app/how-it-works/page.tsx` | How Potluck runs local models, connects your machines and gives agents memory, with current limits |
| `/install` | `src/app/install/page.tsx` | Downloads, checksums and install steps |
| `/benchmarks` | `src/app/benchmarks/page.tsx` | Memory retrieval and local inference results |
| `/roadmap` | `src/app/roadmap/page.tsx` | What is available today, what is being tested and what is planned |
| `/governance` | `src/app/governance/page.tsx` | Who builds Potluck and who is responsible for what |
| `/manifesto` | `src/app/manifesto/page.tsx` | The founding document, as a typed TSX page |
| `/careers` | `src/app/careers/page.tsx` | Open roles |
| `/privacy`, `/terms` | `src/app/privacy/page.tsx`, `src/app/terms/page.tsx` | Legal pages, rendered from `src/content/legal/*.md` |
| `/auth/verify` | `src/app/auth/verify/page.tsx` | Hands a sign-in link token to the desktop app through the `potluck://` deep link (not indexed) |
| `/auth/device` | `src/app/auth/device/page.tsx` | Approves a CLI terminal login (not indexed) |
| `/circles/join` | `src/app/circles/join/page.tsx` | Accepts a trusted-circle invite (not indexed) |
| `/dl/[platform]` | `src/app/dl/[platform]/route.ts` | Counted download redirect to the release file |

`src/app/sitemap.ts`, `opengraph-image.tsx`, `icon.tsx` and `apple-icon.tsx` generate the sitemap and the icons.

## Components

| File | What it does |
|---|---|
| `src/components/Header.tsx` | Sticky nav: logo, nav links, mobile menu |
| `src/components/Footer.tsx` | Footer: tagline, nav, links |
| `src/components/DownloadButtons.tsx` | Per-platform download buttons, linked through `/dl/<platform>` |
| `src/components/Neighborhood.tsx` | Interactive neighborhood demo on the home page |
| `src/components/CommercialFilm.tsx` | Video player for the home page film |
| `src/components/LegalDocument.tsx` | Renders the privacy and terms markdown |
| `src/components/motion.tsx` | Shared reveal and motion primitives that respect `prefers-reduced-motion` |

## Downloads and release constants

`src/lib/downloads.ts` holds the release versions and download URLs and is the single place to update when a new build ships. The download buttons link to `/dl/<platform>`, which redirects to the file. For each real browser request (known crawlers and link-preview fetchers are skipped) the route records the platform and a timestamp, nothing else, through a Supabase RPC. Logging fails open: the redirect works if logging is unset or down.

## Configuration

All variables are optional.

| Variable | Used for | Default |
|---|---|---|
| `NEXT_PUBLIC_SITE_URL` | Site URL in metadata and the sitemap | `https://trypotluck.ai` |
| `NEXT_PUBLIC_API_URL` | Account API used by `/auth/device` | `https://api.trypotluck.ai` |
| `DOWNLOADS_SUPABASE_URL`, `DOWNLOADS_SUPABASE_ANON_KEY` | Download counter; with either unset nothing is logged | unset |

The root layout includes Vercel Analytics (`@vercel/analytics`). The Content-Security-Policy in `vercel.json` restricts script sources to the site's own origin (plus inline scripts), and the fonts load from the site's own origin.

## Theme

The tokens live in `src/styles/globals.css` and are mapped to Tailwind colors in `tailwind.config.ts`. The `:root` block holds the light "Common Room" paper palette and the `.dark` block holds the Hearth palette used by the desktop app. The root layout sets `className="dark"` on the `<html>` element, so the site renders dark. Dark-theme values:

- Background `hsl(28 12% 9%)`, the warm charcoal of the desktop app (`#1A1714`)
- Foreground `hsl(36 30% 94%)`
- Ember accent `hsl(16 60% 66%)` for CTAs and key interactive elements
- Sage `hsl(138 14% 56%)` for live and positive-state signals
- Newsreader for `h1` to `h4`, Spline Sans Mono for code and technical strings

To switch the site to light mode, change `className="dark"` to `className="light"` on the `<html>` element in `src/app/layout.tsx`.
