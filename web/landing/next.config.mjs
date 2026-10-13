/** @type {import('next').NextConfig} */
const nextConfig = {
  pageExtensions: ['ts', 'tsx'],
  images: { qualities: [75, 85] },
  // No external analytics scripts — privacy is the brand.
  // Use Vercel's built-in Edge Analytics (no third-party JS) if you want stats.
  //
  // MDX is intentionally not wired — the manifesto is rendered as a typed
  // TSX page (src/app/manifesto/page.tsx) which is easier to style and
  // maintain than an MDX pipeline. If you want to add MDX later, install
  // @next/mdx + remark-gfm and re-enable pageExtensions: ['ts','tsx','md','mdx'].

  // One origin. The account API only accepts https://trypotluck.ai, so sign-in
  // and terminal approval break on www.
  async redirects() {
    return [
      {
        source: '/:path*',
        has: [{ type: 'host', value: 'www.trypotluck.ai' }],
        destination: 'https://trypotluck.ai/:path*',
        permanent: true,
      },
    ];
  },
};

export default nextConfig;
