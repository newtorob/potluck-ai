import type { Config } from 'tailwindcss';
// eslint-disable-next-line @typescript-eslint/no-require-imports
const typography = require('@tailwindcss/typography');

const config: Config = {
  darkMode: 'class',
  content: ['./index.html', './src/**/*.{ts,tsx}'],
  theme: {
    extend: {
      // Text needs different contrast from a filled button. Keep the ember
      // fill identical across themes while making existing semantic text
      // utilities readable on paper, charcoal, and lightly tinted chips.
      textColor: {
        primary: {
          DEFAULT: 'hsl(var(--primary-text) / <alpha-value>)',
          foreground: 'hsl(var(--primary-foreground) / <alpha-value>)',
        },
        destructive: {
          DEFAULT: 'hsl(var(--destructive-text) / <alpha-value>)',
          foreground: 'hsl(var(--destructive-foreground) / <alpha-value>)',
        },
      },
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
        // Layered surface aliases - semantic elevation
        'surface-1': 'hsl(var(--surface-1))',
        'surface-2': 'hsl(var(--surface-2))',
        'surface-3': 'hsl(var(--surface-3))',
      },
      borderRadius: {
        // Slightly larger default than shadcn's 0.5rem, for softer corners
        // and more breathing room.
        lg: 'var(--radius)',
        md: 'calc(var(--radius) - 2px)',
        sm: 'calc(var(--radius) - 4px)',
      },
      fontFamily: {
        // 'Inter Variable' is the @fontsource-variable export; falls back
        // to the static cut + system stack if the variable face fails.
        sans: ['"Inter Variable"', 'Inter', 'system-ui', 'sans-serif'],
        // Display / editorial face for headlines and editorial text.
        // The single biggest lever on the app's character.
        serif: ['"Instrument Serif"', 'Iowan Old Style', 'Georgia', 'serif'],
        // Mono variable face for code, MCP tool args, model slugs.
        mono: ['"JetBrains Mono Variable"', '"JetBrains Mono"', 'ui-monospace', 'monospace'],
      },
      letterSpacing: {
        // Tight tracking on display headlines (per Things 3 / Oxide style).
        display: '-0.015em',
        // Open tracking on the Hearth label pattern - font-serif text-[11px] italic
        // used for section sub-headers throughout the app.
        'hearth-label': '0.04em',
      },
      // Hearth elevation - surface tinting + hairline, not stacked shadows.
      // These shadows are deliberately quiet; if you reach for them, ask
      // whether a surface tint would do the same job.
      boxShadow: {
        'hearth-sm': '0 1px 2px 0 hsl(28 30% 4% / 0.08)',
        'hearth':    '0 4px 16px -4px hsl(28 30% 4% / 0.18), 0 1px 0 0 hsl(var(--border))',
        'hearth-lg': '0 12px 32px -10px hsl(28 30% 4% / 0.32), 0 1px 0 0 hsl(var(--border))',
        // Inner ring for selected cards - softer than ring-2
        'ember-inset':  'inset 0 0 0 1px hsl(var(--primary) / 0.45)',
        'ember-glow':   '0 0 0 1px hsl(var(--primary) / 0.40), 0 6px 24px -6px hsl(var(--primary) / 0.32)',
      },
      transitionTimingFunction: {
        'out-soft': 'cubic-bezier(0.22, 0.61, 0.36, 1)',
        'out-expo': 'cubic-bezier(0.19, 1, 0.22, 1)',
      },
      transitionDuration: {
        fast: '120ms',
        base: '180ms',
        slow: '280ms',
      },
      keyframes: {
        fadeIn: {
          '0%': { opacity: '0' },
          '100%': { opacity: '1' },
        },
        fadeInUp: {
          '0%': { opacity: '0', transform: 'translateY(4px)' },
          '100%': { opacity: '1', transform: 'translateY(0)' },
        },
        hearthPulse: {
          '0%, 100%': { opacity: '1' },
          '50%': { opacity: '0.55' },
        },
        caretBlink: {
          '0%, 49%':   { opacity: '1' },
          '50%, 100%': { opacity: '0.2' },
        },
      },
      animation: {
        'fade-in':       'fadeIn 180ms cubic-bezier(0.22, 0.61, 0.36, 1) both',
        'fade-in-up':    'fadeInUp 180ms cubic-bezier(0.22, 0.61, 0.36, 1) both',
        'hearth-pulse':  'hearthPulse 2s cubic-bezier(0.22, 0.61, 0.36, 1) infinite',
        'caret-blink':   'caretBlink 1.05s steps(2, end) infinite',
      },
    },
  },
  plugins: [typography],
};

export default config;
