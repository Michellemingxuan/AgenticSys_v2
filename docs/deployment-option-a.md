# Deployment — Option A (Vite dev proxy, air-gapped server)

Run the backend (Flask) and the frontend (Vite) directly on an internal,
no-internet server. The browser only talks to Vite; Vite proxies `/api` to the
backend. LLM calls go through **safechain** to an internal model endpoint
(no public internet).

```
   ┌──────────────────── no-internet server (HOST=0.0.0.0) ────────────────────┐
   │                                                                            │
   │   Vite :5173  ──proxy /api──▶  Flask :3001  ──▶  safechain ──▶ INTERNAL    │
   │   (frontend)   (localhost)      (backend,                       model      │
   │                                  LLM_BACKEND=safechain)         endpoint   │
   │                                                                            │
   │   Trace viewer :3002   (hit directly in browser — not proxied)             │
   └────────────────────────────────────────────────────────────────────────────┘
```

## Ports to open (inbound, internal network)

| Port | Service        | Connects from                         |
|------|----------------|---------------------------------------|
| 5173 | Vite frontend  | Colleagues' browsers                  |
| 3002 | Trace viewer   | Your browser, directly (debug)        |
| 3001 | Flask backend  | Vite proxies via localhost; direct curl for debug |

## Gotchas

- The LLM switch is **`LLM_BACKEND=safechain`** — NOT `LLM_PROVIDER` (that var in
  `CaseReviewChat/.env.example` is legacy/misleading).
- `safechain` is **not** in `requirements.txt`; install it separately from the
  internal pip mirror.
- `HOST` defaults to `127.0.0.1`, which is unreachable from other machines —
  must be `0.0.0.0`. This covers both the backend (3001) and the trace viewer (3002).
- `vite.config.ts` proxy is under `server.proxy`, which only applies to
  `vite dev` (Option A). It does NOT apply to `vite build`/`vite preview` —
  that path needs a reverse proxy (Option B).

## Setup (once)

```bash
# Backend
cd AgenticSys_v2
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt          # internal mirror
pip install <safechain-package>          # internal mirror (already installed)

# Frontend
cd ../CaseReviewChat
npm install                              # internal mirror
```

## Run

Two processes. Use `tmux`, `nohup`, or `systemd` so they survive SSH logout.

```bash
# Terminal 1 — backend + trace viewer
cd AgenticSys_v2 && ./run.sh

# Terminal 2 — frontend
cd CaseReviewChat && ./run.sh
```

Then open `http://<server-internal-ip>:5173`.

Defaults baked into the scripts: `HOST=0.0.0.0`, `PORT=3001`,
`TRACE_VIEWER_PORT=3002`, `LLM_BACKEND=safechain`, `MODEL=gpt-4.1`,
`SAFECHAIN_MODEL=gpt-4.1`. Override any via env before calling `run.sh`.

## Verify

```bash
curl http://localhost:3001/api/cases          # backend up -> JSON
# browser: http://<server-ip>:5173            # app loads, a turn streams events
# browser: http://<server-ip>:3002            # trace viewer (optional)
```
