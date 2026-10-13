# Bundled landing-page fonts

All fonts are served from this site's own origin. No browser or build-time Google Fonts request is required.

## Other Room typography (October 7, 2026)

Bricolage Grotesque is the display face (`--font-display`, weights 200–800, optical sizes 12–96). Instrument Sans is the body/UI face (`--font-body`, weights 400–700). The font-face declarations retain Google's Unicode subsets and use `font-display: swap`.

The Latin WOFF2 files are the only global font preloads: Bricolage Grotesque 76,868 bytes and Instrument Sans 29,904 bytes, 106,772 bytes combined. Latin extended and Bricolage Vietnamese subsets load only when needed. Newsreader, Familjen Grotesk and Spline Sans Mono remain available for editorial and older references without global preloads.

The following official Google Fonts CSS response supplied the pinned WOFF2 files on October 7, 2026, using a modern Chrome User-Agent:

https://fonts.googleapis.com/css2?family=Bricolage+Grotesque:opsz,wght@12..96,200..800&family=Instrument+Sans:wght@400..700&display=swap

| Local file | Official source |
| --- | --- |
| `bricolage-grotesque-normal-vietnamese.woff2` | https://fonts.gstatic.com/s/bricolagegrotesque/v9/3y9K6as8bTXq_nANBjzKo3IeZx8z6up5BeSl9D4dj_x9PpZBMlGHInHWVyNJtvI.woff2 |
| `bricolage-grotesque-normal-latin-ext.woff2` | https://fonts.gstatic.com/s/bricolagegrotesque/v9/3y9K6as8bTXq_nANBjzKo3IeZx8z6up5BeSl9D4dj_x9PpZBMlGGInHWVyNJtvI.woff2 |
| `bricolage-grotesque-normal-latin.woff2` | https://fonts.gstatic.com/s/bricolagegrotesque/v9/3y9K6as8bTXq_nANBjzKo3IeZx8z6up5BeSl9D4dj_x9PpZBMlGIInHWVyNJ.woff2 |
| `instrument-sans-normal-latin-ext.woff2` | https://fonts.gstatic.com/s/instrumentsans/v4/pxiTypc9vsFDm051Uf6KVwgkfoSxQ0GsQv8ToedPibnr0She1ZuWi3hKpA.woff2 |
| `instrument-sans-normal-latin.woff2` | https://fonts.gstatic.com/s/instrumentsans/v4/pxiTypc9vsFDm051Uf6KVwgkfoSxQ0GsQv8ToedPibnr0SZe1ZuWi3g.woff2 |
| `bricolage-share.ttf` | https://fonts.gstatic.com/s/bricolagegrotesque/v9/3y9U6as8bTXq_nANBjzKo3IeZx8z6up5BeSl5jBNz_19PpbpMXuECpwUxJBOm_OJWiaaD30YfKfjZZoLvcXlyM0.ttf |
| `instrument-share.ttf` | https://fonts.gstatic.com/s/instrumentsans/v4/pximypc9vsFDm051Uf6KVwgkfoSxQ0GsQv8ToedPibnr-yp2JGEJOH9npSTF-Qf1.ttf |

The `*-share.ttf` files are official static TTFs returned by the same CSS URL without a modern browser User-Agent: Bricolage weight 600 and Instrument weight 400. They have no variable `fvar` table and are used only by Next.js/Satori share-image rendering, which cannot read WOFF2. No conversion, package installation or fallback typeface was needed.

The original full variable sources, `bricolage-grotesque.ttf` (408,496 bytes) and `instrument-sans.ttf` (194,336 bytes), remain alongside the optimized files for provenance. They were downloaded from the official Google Fonts repository:

- https://github.com/google/fonts/tree/main/ofl/bricolagegrotesque
- https://github.com/google/fonts/tree/main/ofl/instrumentsans

Both families use the SIL Open Font License 1.1. The original license texts are included as `bricolage-OFL.txt` and `instrument-OFL.txt`.

## Retained editorial and earlier typography

`newsreader-share.ttf` and `familjen-share.ttf` are static, regular-weight Latin copies of the matching bundled WOFF2 files. They were generated with FontTools 4.66.1: load the source with `TTFont`, instantiate all variable axes at their defaults (weight 400), set `font.flavor = None`, and save as TTF. Their original SIL Open Font Licenses still apply. Newsreader remains the article title face.

The older WOFF2 files are the exact font assets emitted by the successful local Next.js 15.5.18 build before the September 22, 2026 deployment. The source font-face declarations, Unicode subset ranges, weights and fallback metrics remain in `src/styles/fonts.css`.

The initial Vercel build failed in `next/font/google` while extracting a file extension from a Google Fonts response. Bundling these files removed the external build-time font dependency.

Retained families: Newsreader (normal and italic, variable 200–800 with optical size), Familjen Grotesk (normal, weights 400/500/600/700), and Spline Sans Mono (normal, weights 400/500). Latin, Latin extended and Vietnamese subsets are retained where provided. Original SIL Open Font License 1.1 files accompany the assets, retrieved from:

- https://github.com/google/fonts/tree/main/ofl/newsreader
- https://github.com/google/fonts/tree/main/ofl/familjengrotesk
- https://github.com/google/fonts/tree/main/ofl/splinesansmono
