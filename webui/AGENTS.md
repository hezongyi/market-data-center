# WebUI implementation rules

Applies to all files under `webui/`. Read the root AGENTS first, then the current
[product/UI spec](../docs/specs/2026-09-15-eurusd-first-product-baseline.md).

## Required design foundation

- Use **satnaing/shadcn-admin**, at the commit recorded in
  [reference provenance](../docs/references/shadcn-admin.md), as the complete
  shell/theme/interaction foundation. Read the local reference first.
- Reuse its actual shadcn/Radix primitives, Tailwind styles and animation
  dependencies for the selected components. A handwritten component with the
  same name is not evidence of adopting shadcn.
- Preserve the reference's sidebar, spacing, form, table toolbar, sheet and
  dialog design. Do not introduce another global CSS design system or rebuild
  existing primitives with ad-hoc CSS. Scoped business layout/chart CSS is fine.
- Inventory imported/customized files with upstream paths and commit, retaining
  MIT notices. Existing UI is a compatibility surface, not the visual baseline
  for new work. Scope old global CSS so it cannot override the new foundation.
- Keep one final webui build. Temporary legacy routes/entries must have an exit
  condition. Do not copy upstream package.json wholesale or maintain permanent
  duplicate frontends. Document genuine component limitations and the smallest
  adaptation; a different design direction requires a spec change.

## Preserve working contracts

- Reuse typed API/services, cursor and snapshot semantics, recipe identity,
  optimistic versions, idempotency and asynchronous execution states. Demo Faker
  tasks, fake totals and mock progress must not become production behavior.
- Source provider/instrument/timeframe choices from the capabilities API and
  surface unavailable choices honestly; do not copy template demo options.
- Use existing backend `/auth/*` cookie sessions. Do not ship mock tokens,
  template registration/OTP/forgot-password/Clerk controls without backend scope.
- Default Chinese, existing time-zone behavior, raw IDs remain copyable.
  Loading/empty/error/pending, field validation and retained drafts are required.
- Use navigable task URLs, preserving filters and back behavior; test direct
  refresh and SPA fallback without masking API/static 404s.
- Respect reduced motion, keyboard/focus, desktop and mobile operation.

## Evidence before calling a slice complete

- State which upstream layouts/components were used and which were adapted.
  Check actual package/lockfile imports and rendered pages, not just the spec.
- Provide a working isolated preview URL, commit, data mode, operation steps and
  limitations before merge; let the maintainer assess the actual task flow.
- Build and relevant browser checks must pass. Review verifies component
  provenance and business contracts; the maintainer verifies appearance and use.
- Record remaining legacy pages separately. Do not mark “shadcn migration done”
  because one button was imported or the old CSS build still passes.
- Current fixed Vite proxy to 18380 is not an isolated preview. Follow the
  preview spec and P0 tooling once delivered; never use production for trial writes.
