# ClaudeTrade frontend — design system

Phase 1 (ADR-0008 Decision 2) rebuilds the UI as a React + TypeScript SPA,
replacing Streamlit's default widget chrome with a dark, purpose-built
"research terminal" aesthetic. This document is the reference for the next
phase (paper-trading detail screens, backtesting, settings) so the app keeps
reading as one system rather than five independently styled pages.

## Why this stack

- **Vite + React + TypeScript** — the fastest path to a polished, testable
  SPA; the owner's complaints ("ugly", "icons aren't smooth", "can't click
  through") are aesthetic/interactional, exactly where a hand-styled web
  stack beats a widget framework's defaults.
- **Tailwind CSS v4** (`@tailwindcss/vite`, CSS-first `@theme` config) — every
  design token is a CSS custom property, which both Tailwind's utilities and
  hand-written CSS (AG Grid's Theming API, Plotly's layout objects) read from
  the same source of truth.
- **AG Grid Community** (`ag-grid-community` + `ag-grid-react`, Theming API,
  no enterprise modules) — the Screener's interactive grid.
- **Plotly.js** (`plotly.js-dist-min` via `react-plotly.js`'s factory entry
  point, not the full `plotly.js` bundle) — candlestick/volume/RSI/sentiment
  and the equity sparkline.
- **lucide-react** — every icon in the app. This is the direct answer to
  "icons aren't smooth": crisp SVGs at any DPI, never an emoji standing in
  for an icon.
- **react-router-dom** — client-side routing only (`createBrowserRouter`,
  library mode). No SSR, no data loaders/actions, no RSC — see "A note on
  `npm audit`" below for why that distinction matters.

## Design tokens

Defined once in `src/index.css`'s `@theme` block and exposed as Tailwind
utilities (`bg-surface`, `text-ink-secondary`, `border-gridline`, ...). AG
Grid's theme (`src/grid/theme.ts`) and the Plotly chart builders
(`src/charts/*.tsx`) both hard-code the *same* hex values as a second source
— Tailwind's CSS variables aren't reachable from a JS chart layout object at
render time, so the values are kept in sync by comment rather than import.
If a token here changes, grep for the hex value in `src/charts/` and
`src/grid/theme.ts` too.

| Token | Value | Use |
|---|---|---|
| `--color-page` | `#0d0d0d` | App background, outside cards |
| `--color-surface` | `#1a1a19` | Card/panel background |
| `--color-surface-2` | `#202020` | Nested surface (table header, grid header) |
| `--color-gridline` | `#2c2c2a` | Borders, dividers, chart gridlines |
| `--color-ink` | `#ffffff` | Primary text |
| `--color-ink-secondary` | `#c3c2b7` | Secondary text, table body |
| `--color-ink-muted` | `#898781` | Captions, placeholders, disabled |
| `--color-accent` / `-strong` / `-soft` | `#3987e5` / `#184f95` / 15% alpha | The one interactive accent — buttons, links, active nav, RSI line, chart accents. Carried over verbatim from `claudetrade.ui.theme.ACCENT` for continuity with the retiring Streamlit UI. |
| `--color-long` / `--color-short` / `--color-neutral` | `#3987e5` / `#e66767` / `#898781` | The diverging blue/red pair for **direction and polarity only** (long/short, bullish/bearish). Never reused for unrelated categorical data. |
| `--color-good` / `-warning` / `-serious` / `-critical` / `-muted` | `#0ca30c` / `#fab219` / `#ec835a` / `#d03b3b` / `#898781` | Signal/order lifecycle state (`StatusChip`) and severity messaging. Fixed; never themed, never reused for series identity. |

**Spacing.** Tailwind's default scale (4px steps) is used throughout, but
every value actually chosen in this app is a multiple of 8px (`gap-2`=8px,
`p-4`=16px, `gap-4`=16px, `py-2`=8px, ...) — an 8px grid expressed through
Tailwind's existing scale rather than a custom one, so the vocabulary stays
familiar to anyone who already knows Tailwind.

**Typography.** System font stack (`-apple-system, BlinkMacSystemFont,
"Segoe UI", Inter, Roboto, ...`) rather than a bundled Inter font file — this
ships as a native desktop app via pywebview with no guaranteed network
access at runtime, so a self-hosted or CDN font was avoided. `Inter` is
listed in the stack for machines that do have it installed. Numbers use
`font-variant-numeric: tabular-nums` app-wide (set on `body`) so score/price
columns don't jitter as digits change width.

## States every screen must handle

- **Loading**: skeletons (`Skeleton`, `SkeletonRows`, `SkeletonCard`), never a
  spinner. A pulsing placeholder shaped like the content that's coming beats
  a spinner that tells the user nothing about what's about to appear.
- **Empty**: `EmptyState` always pairs its message with the exact command or
  action that fixes it (`claudetrade scan`, `claudetrade refresh`, "Run
  Scan"), mirroring `ui.components.tables.empty_state`'s rule verbatim —
  never a bare "no data".
- **Unavailable-with-reason**: numbers that can't be computed yet (win/loss
  ratio with zero trades, max drawdown with no equity history, near-miss
  candidates before any scan has run this session) render the *reason*, not
  a fake `0` or an empty table. See `PerformanceOut`/`RejectedResponse` on
  the API side and `Tile`/`EmptyState` on the frontend side.
- **Error**: every screen catches its own fetch failures and renders the
  server's `detail` message (via `ApiError`) rather than a blank screen.

## AG Grid theming

`src/grid/theme.ts` builds a theme via AG Grid's Theming API
(`themeQuartz.withPart(colorSchemeDark).withParams({...})`) — **not** the
legacy CSS-variable/`ag-theme-*` class approach, which AG Grid v33+
deprecated. No AG Grid CSS is imported anywhere; the Theming API generates
scoped styles at runtime from the params object, all pointed at this app's
own tokens (surface/ink/accent/gridline) rather than any stock AG Grid
palette. `.ct-grid-shell` in `index.css` adds only what the Theming API
doesn't cover: the wrapper's rounded corners/border to match the rest of the
app's card chrome, and `cursor: pointer` on every row — row-click navigation
is the Screener's entire reason for existing in this rebuild.

Only Community modules are registered (`src/grid/register.ts`,
`AllCommunityModule`) — no enterprise features anywhere.

## API client

`src/api/client.ts` is a thin `fetch` wrapper; `src/api/types.ts` mirrors
every pydantic response model in `claudetrade.webapi.schemas` field-for-field
(snake_case, matching the JSON wire shape exactly — one fewer translation
layer to keep in sync, at the cost of non-idiomatic-JS field names). If a
backend schema changes, update the matching interface here by hand; there is
no shared-types codegen step in phase 1.

## Testing

- `src/api/client.test.ts` — vitest smoke test for every API client method
  (query-string construction, POST bodies, error handling).
- `src/screens/Screener.test.tsx` — component test for the row-click
  navigation. AG Grid's real rendering is virtualised and leans on browser
  layout APIs (`ResizeObserver`, `getBoundingClientRect`) that jsdom only
  partially implements; `ag-grid-react` is stubbed with a minimal fake that
  renders one row per item and invokes the *real* `onRowClicked` handler
  `Screener.tsx` wires up, so the test exercises the actual navigation logic
  rather than AG Grid's internals.
- Run: `npm run test` (or `npx vitest run` for CI/non-watch mode).

## Build

`npm run build` runs `tsc -b && vite build`. Output goes straight to
`../src/claudetrade/webapi/static/` (see `vite.config.ts`) and is committed
to the repository so end users never need Node — `python -m
claudetrade.webapi` serves whatever is already built. Re-run the build and
commit the new `static/` contents whenever the frontend changes; there is no
build step in the shipped application itself.

Bundle size: the entry chunk is ~290 KB (~93 KB gzipped). AG Grid
(Screener) and Plotly (Dashboard's sparkline, Ticker Detail's chart) are each
several hundred KB to a few MB and are loaded via `React.lazy` per route
(`src/App.tsx`) rather than in the entry chunk — Plotly in particular is
inherently large (`plotly.js-dist-min`, ~4.6 MB / ~1.4 MB gzipped) because
the ADR specifies Plotly for chart quality; this is an accepted trade-off
for a locally-served desktop app, where "download" is a loopback disk read,
not a network fetch.

## A note on `npm audit`

`npm audit` flags `react-router-dom`/`react-router` regardless of which
recent version is installed — every advisory in range is about SSR,
framework-mode data loaders/actions, or React Server Components (open
redirects in server-rendered redirects, CSRF on server actions, RSC
hydration deserialization, ...). This app uses `react-router-dom` in plain
client-library mode (`createBrowserRouter` + `RouterProvider`, no loaders,
no actions, no SSR, no RSC) and is served as a static SPA by
`claudetrade.webapi`'s own dumb file server — none of the vulnerable code
paths are reachable. Pinned to the latest release (`7.18.2` at time of
writing) rather than chasing a downgrade that only trades one flagged range
for another.

## What's out of scope for phase 1

- **Light mode.** The design is dark-only for now — a genuine research
  terminal aesthetic, not a dark-mode reskin of a light app. `color-scheme:
  dark` is set globally; a light theme (if ever wanted) would need real
  token values added here, not just an inversion.
- **Backtesting and Settings screens** stay on the Streamlit `--classic` UI
  until a later phase.
