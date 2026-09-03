# AP2 Audit Dashboard (frontend)

Vite + React + Tailwind v4 dashboard for the Phase 6 append-only audit ledger.

- Polls `GET /api/audit/latest` and `GET /api/transact` (all transactions) every 2s.
- Polls `GET /api/audit/{intent_id}` every 2s for whichever flow is selected —
  defaults to "replay latest" (auto-follows the most recently active flow),
  or click any row in the transactions table to pin to that flow.
- Each event in the vertical timeline is expandable to show its full
  `payload_snapshot` JSON (e.g. the `alternatives` array on `FAILURE_DIVERTED`).

## Run

Backend must be running first (from the repo root):

```bash
uvicorn app.main:app --reload --port 8000
```

Then, from this directory:

```bash
npm install
npm run dev
```

Opens on http://localhost:5173 — `vite.config.js` proxies `/api/*` to
`http://localhost:8000`, so no CORS setup or `VITE_API_BASE_URL` is needed
in dev. For a production build (`npm run build`), serve `dist/` behind the
same origin as the API, or add a reverse-proxy rule for `/api`.
