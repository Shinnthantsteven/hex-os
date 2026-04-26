# HEX OS

A personal dashboard with a dark cyberpunk aesthetic, built as a single `index.html` file with zero dependencies beyond Google Fonts.

## Features

- **Year Progress** — live bar showing % of 2026 completed, days elapsed/remaining, week and quarter
- **Interactive Calendar** — browse months, add/delete events, persisted to localStorage
- **To-Do List** — High / Mid / Low priority tasks, filter by priority or done state, persisted to localStorage
- **Monthly Spending Tracker** — category bars (Food, Transport, Shopping, Bills, Health, Fun) with per-entry log, navigate between months, persisted to localStorage
- **Subscriptions Tracker** — days-until-renewal countdown, color coded red ≤3 days / amber ≤7 days / green otherwise, persisted to localStorage
- **WhatsApp Hex Bot Commands** — click-to-copy command panel for your WhatsApp automation bot
- **Live Clock** in the header
- **Fully responsive** — works on mobile and desktop

## Stack

- Vanilla HTML / CSS / JavaScript — no frameworks, no build step
- Google Fonts: Bebas Neue + DM Mono
- All persistence via `localStorage`

## Usage

Open `index.html` in any modern browser, or visit the GitHub Pages URL.

## Theme

| Token | Value |
|---|---|
| Background | `#07070f` |
| Surface | `#0d0d1a` |
| Accent | `#00ffcc` |
| Danger | `#ff4d6d` |
| Warning | `#ffb830` |

## Deploying OpenClaw to Railway

OpenClaw is the WhatsApp AI bot that powers the HEX OS bot commands. The repo includes a `Dockerfile` and `railway.json` so you can host it 24/7 on [Railway.app](https://railway.app).

### Prerequisites

- A Railway account (railway.app)
- Railway CLI: `npm install -g @railway/cli` and `railway login`
- An Anthropic API key (console.anthropic.com) — OAuth auth doesn't work headlessly

### Steps

**1. Push this repo to GitHub** (if not already done)

**2. Create a Railway project**

In the Railway dashboard → New Project → Deploy from GitHub Repo → select `hex-os`.

**3. Add a persistent volume**

Railway Dashboard → your service → Volumes → Add Volume → mount path `/data`.
This stores OpenClaw state, WhatsApp session, and workspace across redeploys.

**4. Set environment variables**

In your Railway service settings → Variables, add (copy from `.env.example`):

| Variable | Value |
|---|---|
| `OPENCLAW_GATEWAY_TOKEN` | any long random secret |
| `OPENCLAW_GATEWAY_PORT` | `8080` |
| `OPENCLAW_STATE_DIR` | `/data/.openclaw` |
| `OPENCLAW_WORKSPACE_DIR` | `/data/workspace` |
| `ANTHROPIC_API_KEY` | your Anthropic API key |
| `TAVILY_API_KEY` | your Tavily key (optional) |

**5. Enable HTTP proxy**

Railway Dashboard → your service → Settings → Networking → Generate Domain. Railway will route HTTPS traffic to port 8080 automatically.

**6. Deploy**

```bash
railway up
```

Or it deploys automatically on every push to `main`.

**7. Connect WhatsApp**

Once deployed, open the Control UI:

```
https://<your-railway-domain>/openclaw
```

Log in with your `OPENCLAW_GATEWAY_TOKEN`, go to Channels → WhatsApp → scan the QR code with your phone.

> **Note:** WhatsApp session state is saved to `/data/.openclaw` (the persistent volume), so it survives redeploys.

### Health checks

Railway pings `/healthz` every 30 seconds. `/readyz` confirms WhatsApp is connected and the agent is ready.

### Updating OpenClaw

The Dockerfile installs `openclaw@latest` at build time. To update, just trigger a redeploy:

```bash
railway redeploy
```

---

## License

MIT
