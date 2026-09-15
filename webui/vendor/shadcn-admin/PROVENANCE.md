# shadcn-admin provenance

Upstream: `satnaing/shadcn-admin` at commit
`e16c87f213a5ba5e45964e9b67c792105ec74d26` (version 2.2.1, MIT).

The P1 UI copies and adapts these files:

| Upstream path | Local path | Adaptation |
| --- | --- | --- |
| `src/styles/index.css`, `src/styles/theme.css` | `src/p1.css` | Retained Tailwind v4 theme tokens and base behavior; reduced to the tokens used by this application and added business shell layout. |
| `src/components/ui/button.tsx` | `src/components/shadcn/button.tsx` | Relative import and selected variants only. |
| `src/components/ui/card.tsx` | `src/components/shadcn/card.tsx` | Relative import; selected exports only. |
| `src/components/ui/input.tsx` | `src/components/shadcn/input.tsx` | Relative import only. |
| `src/components/ui/label.tsx` | `src/components/shadcn/label.tsx` | Relative import only. |
| `src/components/ui/badge.tsx` | `src/components/shadcn/badge.tsx` | Relative import and selected variants only. |
| `src/components/ui/table.tsx` | `src/components/shadcn/table.tsx` | Relative import and selected exports only. |
| `src/components/ui/sheet.tsx` | `src/components/shadcn/sheet.tsx` | Relative import, Chinese accessible close label, right-side task form sizing. |
| `src/components/ui/select.tsx` | `src/components/shadcn/select.tsx` | Relative import and selected exports only. |
| `src/components/ui/sidebar.tsx` | `src/components/shadcn/sidebar.tsx` | Retained provider, slot structure, menu primitives and mobile Sheet; omitted collapse/tooltip variants not used by P1. |
| `src/lib/utils.ts` | `src/lib/utils.ts` | Retained `cn`; omitted unused helpers. |
| `src/components/layout/app-sidebar.tsx`, `src/features/tasks/index.tsx`, `src/features/tasks/components/tasks-table.tsx`, `src/features/tasks/components/tasks-mutate-drawer.tsx`, `src/features/auth/sign-in/*` | `src/p1-main.tsx`, `src/p1/tasks.tsx` | Shell, task toolbar/table/sheet and auth composition adapted to real MDC routes, API contracts and cookie session. No upstream demo data or mock authentication retained. |

The unmodified upstream license is stored in this directory. The legacy UI is
served through `legacy.html` in an iframe so its global CSS cannot override the
new Tailwind/shadcn foundation.

`legacy.html` is temporary compatibility infrastructure. Remove it after the
remaining data-view, run/problem, and settings routes migrate during P2–P4 and
their browser coverage targets the new routes; it must not survive P4 closure.
