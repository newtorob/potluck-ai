# components/ui/

This directory is the target for shadcn/ui component generation.

When adding a shadcn component later, run from `apps/desktop/frontend/`:

```
pnpm dlx shadcn@latest add button
pnpm dlx shadcn@latest add dialog
# etc.
```

shadcn will place generated files here. The `cn.ts` helper is already present
and matches the path shadcn expects (`@/components/ui/cn`).

Do not hand-write files in this directory. Let shadcn generate them so
they stay in sync with the shadcn release you are targeting.
