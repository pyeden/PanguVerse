# PanguVerse Workbench UI

Vite + React 19 + TypeScript + React Flow + TanStack Query.

## Build

```bash
cd webui
npm ci
npm run build
```

`vite.config.ts` writes directly to `../panguverse/web/static/`:

- `index.html`
- `app.js`
- `style.css`

Fixed entry filenames are intentional: the current `http.server` static whitelist only
serves `index.html`, `app.js`, `style.css`, `favicon.svg`. This keeps
`python -m panguverse serve` a zero-build experience for pip users. `base: './'` keeps
all API and asset URLs relative so the app also works behind `/panguverse/`.

## Develop

```bash
cd webui
npm install
npm run dev
```

The dev server proxies `/api` to `http://127.0.0.1:7788`; start
`python -m panguverse serve --port 7788` first.

## Routes

- `#/projects`, `#/p/:pid/{overview,run,history,settings}`, `#/p/:pid/runs/:rid`
- `#/studio/new`, `#/studio/:wid`, `#/studio/:wid/review`
- `#/atoms`, `#/tasks`, `#/executions`, `#/history`, `#/reports`

The Studio UI has a local fixture fallback only when `/api/studio/*` returns 404, so
frontend work can continue before the backend lands. Once the backend exists, the
same page reloads against the real API automatically.

> Note: `content-visibility: auto` (used for long debug timelines) is ignored by
> Safari < 18; on those versions long lists fall back to normal rendering and may
> scroll/paint less smoothly.
