# Bundled landing-page fonts

These WOFF2 files are the exact font assets emitted by the successful local Next.js 15.5.18 build of the approved landing design. They were fetched by next/font/google before the September 22, 2026 deployment. The source font-face declarations, Unicode subset ranges, weights, and fallback metrics are preserved in src/styles/fonts.css.

The initial Vercel build failed in next/font/google while extracting a file extension from a Google Fonts response. Bundling these files removes the external build-time font dependency and preserves the reviewed typography. Browsers continue to load fonts from the website's own origin, as before.

Families: Newsreader (normal and italic, variable 200–800 including optical size), Familjen Grotesk (normal, weights 400/500/600/700), and Spline Sans Mono (normal, weights 400/500). Latin, Latin extended and Vietnamese subsets are retained where provided. The four Latin files are preloaded in src/app/layout.tsx.

The fonts are licensed under the SIL Open Font License 1.1. Original license files are included beside the assets, retrieved from the Google Fonts repository:
- https://github.com/google/fonts/tree/main/ofl/newsreader
- https://github.com/google/fonts/tree/main/ofl/familjengrotesk
- https://github.com/google/fonts/tree/main/ofl/splinesansmono
