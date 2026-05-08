import type { Config } from 'tailwindcss';
// eslint-disable-next-line @typescript-eslint/no-require-imports
const typography = require('@tailwindcss/typography');

const config: Config = {
  darkMode: 'class',
  content: ['./index.html', './src/**/*.{ts,tsx}'],
  theme: {
    extend: {
      colors: {
        // shadcn/ui-compatible CSS variable tokens.
        // Each color maps to an HSL CSS variable so shadcn components
        // can be dropped in without any config changes.
        background: 'hsl(var(--background))',
        foreground: 'hsl(var(--foreground))',
        card: {
          DEFAULT: 'hsl(var(--card))',
          foreground: 'hsl(var(--card-foreground))',
        },
        popover: {
          DEFAULT: 'hsl(var(--popover))',
          foreground: 'hsl(var(--popover-foreground))',
        },
        primary: {
          DEFAULT: 'hsl(var(--primary))',
          foreground: 'hsl(var(--primary-foreground))',
        },
        secondary: {
          DEFAULT: 'hsl(var(--secondary))',
          foreground: 'hsl(var(--secondary-foreground))',
        },
        muted: {
          DEFAULT: 'hsl(var(--muted))',
          foreground: 'hsl(var(--muted-foreground))',
        },
        accent: {
          DEFAULT: 'hsl(var(--accent))',
          foreground: 'hsl(var(--accent-foreground))',
        },
        destructive: {
          DEFAULT: 'hsl(var(--destructive))',
          foreground: 'hsl(var(--destructive-foreground))',
        },
        border: 'hsl(var(--border))',
        input: 'hsl(var(--input))',
        ring: 'hsl(var(--ring))',
        // Hearth custom tokens (not shadcn defaults). Use sparingly:
        // sage for "alive/online" signal, paper for cross-theme bridges.
        sage: {
          DEFAULT: 'hsl(var(--sage))',
          foreground: 'hsl(var(--sage-foreground))',
        },
        paper: 'hsl(var(--paper))',
      },
      borderRadius: {
        // Slightly larger default than shadcn's 0.5rem — a small move
        // that pushes Direction-B "softer corners, more breath."
        lg: 'var(--radius)',
        md: 'calc(var(--radius) - 2px)',
        sm: 'calc(var(--radius) - 4px)',
      },
      fontFamily: {
        // 'Inter Variable' is the @fontsource-variable export; falls back
        // to the static cut + system stack if the variable face fails.
        sans: ['"Inter Variable"', 'Inter', 'system-ui', 'sans-serif'],
        // Display / editorial face for headlines + the manifesto layer.
        // Same family the design brief identifies as the single biggest
        // character lever versus the default-shadcn cold-violet vibe.
        serif: ['"Instrument Serif"', 'Iowan Old Style', 'Georgia', 'serif'],
        // Mono variable face for code, MCP tool args, model slugs.
        mono: ['"JetBrains Mono Variable"', '"JetBrains Mono"', 'ui-monospace', 'monospace'],
      },
      letterSpacing: {
        // Tight tracking on display headlines (per Things 3 / Oxide style).
        'display': '-0.015em',
      },
    },
  },
  plugins: [typography],
};

export default config;
