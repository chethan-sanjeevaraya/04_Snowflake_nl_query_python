# Snowflake NL Query — React chat UI

A React + TypeScript chat UI for the natural-language → Snowflake text-to-SQL
Lambda, alongside the existing Streamlit app in `../frontend/`. Functionally
equivalent to that app (same connection/target/table-hint sidebar, same
result rendering, same conversation-memory and clarification-question
support) but calls API Gateway **directly from the browser** instead of
through a Python server process.

Static-buildable: `npm run build` produces plain files in `dist/` that can be
hosted anywhere (S3+CloudFront, Netlify, Vercel, nginx, ...) — there is no
backend of its own, no server-side process to keep running.

## Run it locally

```bash
npm install
npm run dev
```

Opens on `http://localhost:5173`. Paste your API Gateway invoke URL into the
sidebar, or set it once via `.env.local` (copy `.env.example`):

```bash
cp .env.example .env.local
# edit .env.local: VITE_API_URL=https://abc123.execute-api.eu-central-1.amazonaws.com/dev/query
```

## Build for production / static hosting

```bash
npm run build      # -> dist/
npm run preview    # serve dist/ locally to sanity-check the build
```

`dist/` is a fully static site. A few ways to host it:

- **S3 + CloudFront**: `aws s3 sync dist/ s3://your-bucket/ --delete`, then
  invalidate the CloudFront distribution's cache. Enable S3 static website
  hosting or front the bucket with CloudFront (recommended, for HTTPS).
- **Netlify / Vercel**: drag-and-drop `dist/`, or connect the repo and set
  the build command to `npm run build` and the publish directory to `dist`.
- **Any static file server / nginx**: copy `dist/` to the document root.

Because the app calls API Gateway directly at runtime (not through a server
you control), there's no environment-specific server config to manage after
deploy — just the CORS setup below, done once on the API Gateway side.

## Required one-time setup: CORS on API Gateway

This is the one piece of infrastructure this UI needs that the Streamlit app
didn't, because Streamlit called the Lambda from a Python server process
(no browser involved, so no CORS). A browser calling API Gateway directly
sends a **preflight `OPTIONS` request** before the real `POST`, and API
Gateway has to answer that preflight itself — it never reaches the Lambda.

The Lambda's own POST responses already send
`Access-Control-Allow-Origin: *` (see `backend/lambda_function.py`'s
`_respond()`), so once the preflight is handled, the real requests already
carry the right header. You only need to make the preflight work:

**REST API** (API Gateway v1): open the `/query` resource in the console →
**Actions → Enable CORS** → accept the defaults (or list `Content-Type,
x-api-key` under allowed headers if you use an API key) → **Deploy API** to
your stage. This creates a mock `OPTIONS` method that answers the preflight
without invoking the Lambda.

**HTTP API** (API Gateway v2): API → **CORS** in the left nav → add your
origin (or `*` for dev testing) under Access-Control-Allow-Origin, `POST`
under Allow-Methods, and `Content-Type, x-api-key` under Allow-Headers. HTTP
APIs handle the preflight automatically once CORS is configured — no
separate OPTIONS method to create.

If you skip this, requests fail with no useful detail — a CORS-blocked
request never reaches this code with a status code at all; the browser
just refuses to expose anything about the response, and `fetch()` throws a
generic error. The **browser console** (not this app's UI) is what actually
tells you it was CORS: it logs an explicit CORS error there. The app's own
"Could not reach the API" message names CORS as one of the possible causes,
but can't distinguish it from the network being down or the URL being wrong.

## ⚠️ Same no-auth caveat as the Streamlit app

This ships with no authentication, same as `../frontend/`. Two differences
from the Streamlit setup worth knowing before this goes anywhere near other
people:

1. **The API URL (and API key, if set) end up in the browser bundle and
   network tab.** Unlike the Streamlit app — where the API call happens
   server-side and nothing about the endpoint reaches the user's browser —
   this app calls API Gateway directly from client-side JavaScript, so
   anyone who opens dev tools can see the URL and any `x-api-key` you
   configured. Don't put a production secret in `.env.local` or the
   sidebar's API key field.
2. **The API Gateway endpoint is presumably unauthenticated too**, same as
   documented in `../README.md`. Add a Cognito JWT authorizer or an API key
   + usage plan on the API Gateway side before sharing this with anyone —
   the sidebar already supports sending `x-api-key`, but a key sent from the
   browser is only a speed bump, not real access control (it's visible to
   the same dev-tools inspection as the URL).

## What's implemented

Same feature set as `../frontend/app.py`, described in the project's main
`README.md`:

- Connection settings (API URL, API key, client timeout) and target
  discovery (probes the Lambda's trusted DATABASE/SCHEMA pairs).
- Table hint field, and manual DATABASE/SCHEMA entry when discovery hasn't
  been run.
- Conversation memory: the `session_id` the Lambda returns is tracked and
  sent on every subsequent question, so follow-ups ("now filter that to
  last quarter") and result-referencing questions ("what was the second
  one about?") work the same as in the Streamlit UI.
- Clarification questions: when the Lambda responds with
  `needs_clarification`, the candidate tables render as clickable buttons
  (only on the latest such message) — click one, or just type a table name,
  and it resolves via the session without repeating the original question.
- Result rendering: answer text, truncation warning, narrowing note, a
  collapsible SQL block, a scrollable results table with CSV download, and
  full error rendering (detail, reason, generated SQL, available tables,
  valid targets).
- The chat transcript, session id, and API URL persist in `localStorage`
  across page refreshes. The API key deliberately does **not** persist —
  see `src/useLocalStorage.ts`.

## Project layout

```
src/
  api.ts                    Fetch-based client (TS port of ../frontend/api_client.py)
  types.ts                  Response/message shapes
  useLocalStorage.ts        Small persisted-state hook
  App.tsx                   Top-level state + layout
  components/
    Sidebar.tsx              Connection / target / behaviour controls
    ChatMessage.tsx          Dispatches to the three message kinds below
    SuccessMessage.tsx       Answer, SQL, results table
    ClarificationMessage.tsx Candidate-table buttons
    ErrorMessage.tsx         Error/detail/reason/available-tables rendering
    ResultTable.tsx          Scrollable table + CSV export
```

## Known gaps vs. a production app

This was built to match the Streamlit app's feature set with a nicer UI, not
to be a production-hardened frontend. Worth knowing about:

- No automated tests (the Streamlit app doesn't have any either). It *was*
  exercised end-to-end against mocked API responses (clarification → pick →
  session-resolved answer → table render → SQL expand → error render →
  clear chat) during development, but nothing is checked into the repo to
  re-run that.
- No retry/backoff on transient network failures — one attempt per question,
  same as the Streamlit client.
- `npm audit` currently flags a moderate advisory in `esbuild` (bundled by
  Vite 5's dev server) that only matters while running `npm run dev` on a
  network reachable by others — it lets a malicious webpage read the dev
  server's responses. It does not affect `npm run build` output. Fixing it
  means moving to Vite 6+, a breaking change not made here; worth doing
  before this is used in a shared/always-on dev environment.
