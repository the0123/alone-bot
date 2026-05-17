# alone-bot.log — project doc

**Status** v1 / BUILT
**Host** MainLXC (192.168.1.10) on Proxmox VE
**Runtime** Docker container, managed via Portainer
**Container path** `/opt/alone-bot/`
**Built** May 2026

---

## 01 What this is

A self-hosted Telegram bot that suggests things to do when alone. On a schedule, asks if I'm alone; if yes, returns a randomly selected activity from a curated list. Inline buttons let me accept, reject, ask for another, or mark "not alone." Logs every interaction for later tuning. Followup 1.5h after accept asks "did you actually do it?"

Items stay in the activity pool permanently — completing one doesn't remove it. A 3-day blackout prevents the same activity from being re-suggested within that window.

v1 is intentionally LLM-free. The LLM layer (v2) will get its own LXC.

## 02 How it works

```
[scheduler] ─── timed trigger ──→ [bot] ──→ "Are you alone?" + [Yes] [No]
                                                       │
                                  ┌──── No ────────────┴──── Yes ──────┐
                                  ▼                                    ▼
                          log 'not_alone'                 pick random activity
                          + sleep until next                (respecting blackout)
                                                                       │
                                                                       ▼
                                                       send suggestion +
                                                       [Accept] [Another] [Reject]
                                                                       │
                                          ┌────────────┬───────────────┤
                                          ▼            ▼               ▼
                                     'accepted'    'another'       'rejected'
                                          │            │               │
                                          ▼            ▼               ▼
                                    schedule       pick new        log + done
                                    followup       activity
                                    (+1.5h)        (loop)
                                          │
                                          ▼
                              "Did you do it?" + [Yes] [No]
                                          │
                                          ▼
                                  set completed = true/false
```

**Triggers**
- Weekday: 5:15pm Mon–Fri (`15 17 * * 1-5`)
- Weekend: 11am, 3:30pm, 8pm Sat/Sun
- On-demand: `/suggest` command, anytime, skips the "Are you alone?" gate
- Followup: 1.5h after an accepted suggestion
- Heartbeat: daily 9am "I'm alive" message
- Stale sweep: hourly cron marks unanswered suggestions older than 12h as `no_response`

**Commands**
- `/start` — bind chat ID, confirm bot is alive
- `/suggest` — get a suggestion now
- `/add <text>` — add a new activity to the list
- `/list` — show all activities (numbered)
- `/stats` — show acceptance/completion counts and top accepted/rejected

**Inline buttons**
- Gate: **Yes** / **No** (callback: `gate_yes` / `gate_no`)
- Suggestion: **✅ Accept** / **🔁 Another** / **❌ Reject** (callback: `accepted` / `another` / `rejected`)
- Followup: **Yes, did it** / **No, didn't** (callback: `followup_yes` / `followup_no`)

Callback data is `<response>:<suggestion_id>`. Buttons are stripped from old messages on every tap so only the newest suggestion has live buttons.

## 03 Architecture

```
┌─ Proxmox host (192.168.1.2) ──────────────────────────────────┐
│                                                               │
│  ┌─ MainLXC (192.168.1.10) ───────────────────────────────┐   │
│  │                                                        │   │
│  │  Docker:                                               │   │
│  │   - Homepage  :3000                                    │   │
│  │   - Portainer :9000                                    │   │
│  │   - Redlib    :8085                                    │   │
│  │   - Nginx     :8086                                    │   │
│  │   - alone-bot  (no exposed port — outbound only)       │   │
│  │                                                        │   │
│  │  Bot volume: /opt/alone-bot/data → /data in container  │   │
│  │  Holds SQLite DB                                       │   │
│  │                                                        │   │
│  └────────────────────────────────────────────────────────┘   │
│                                                               │
│  ┌─ Future OllamaLXC (v2) ───┐                                │
│  │ Ollama :11434             │◀── HTTP (v2 only)              │
│  │ llama3.2:3b               │                                │
│  └───────────────────────────┘                                │
│                                                               │
└───────────────────────────────────────────────────────────────┘
                          │
                          ▼ outbound only
                   api.telegram.org
```

## 04 Networking

- **Telegram (outbound):** Bot polls `api.telegram.org` via long-polling. No inbound ports, no webhook, no Tailscale.
- **Bot ↔ Ollama (v2):** Direct LAN over the Proxmox bridge — `http://192.168.1.<ollama-ip>:11434/api/generate`. Ollama needs `OLLAMA_HOST=0.0.0.0` to accept non-localhost connections.
- **Container access:** Bot runs without exposed ports. Logs via `docker logs alone-bot` or Portainer.

## 05 Tech stack

| Layer | Choice |
|---|---|
| Language | Python 3.12 |
| Bot library | `python-telegram-bot[job-queue]` v21.6 |
| Scheduler | APScheduler 3.10.4 (AsyncIO) |
| Database | SQLite (built-in, via `sqlite3`) |
| Config | `config.toml` (read via stdlib `tomllib`) + env vars |
| Container | `python:3.12-slim` |
| Process mgmt | Docker, managed via Portainer |
| LLM (v2) | Ollama + llama3.2:3b |

## 06 Data model

```sql
-- Activities pool. Items are never removed; soft-delete via active = 0.
CREATE TABLE activities (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    text        TEXT NOT NULL UNIQUE,
    source      TEXT,                -- 'seed:personal' | 'user' | 'llm' (v2)
    created_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    active      BOOLEAN DEFAULT 1
);

-- Every suggestion the bot makes + the response.
-- activity_id is nullable: 'not_alone' and 'gate_yes' rows have no activity.
CREATE TABLE suggestions (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    activity_id         INTEGER REFERENCES activities(id),
    suggested_at        TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    trigger             TEXT,           -- 'scheduled' | 'on_demand' | 'rerolled'
    response            TEXT,           -- see response values below
    response_at         TIMESTAMP,
    completed           BOOLEAN,        -- set by 1.5h followup
    completed_at        TIMESTAMP,
    followup_sent_at    TIMESTAMP       -- stamped when followup message sent
);

-- Bot config / state (chat ID binding).
CREATE TABLE state (
    key    TEXT PRIMARY KEY,
    value  TEXT
);
```

**Response values**
- `gate_yes` — gate row, user answered "yes I'm alone" (suggestion follows in a separate row)
- `not_alone` — gate row, user answered "no I'm not alone"
- `accepted` — user accepted a suggestion
- `rejected` — user rejected a suggestion
- `another` — user tapped "Another" (logged before the next suggestion fires)
- `no_response` — written by hourly sweep after `suggestion_timeout_hours`

**Key behaviors**
- **Blackout filter** prevents an activity from being re-suggested within 3 days. Implemented as `WHERE id NOT IN (SELECT activity_id FROM suggestions WHERE suggested_at > -3 days AND activity_id IS NOT NULL)`.
- **`trigger = 'rerolled'`** distinguishes a re-pick from a fresh scheduled suggestion. Critical for accurate acceptance-rate math.
- **`followup_sent_at`** prevents restart-induced duplicate followups. Only `accepted` rows where this field is NULL get re-scheduled on bot restart.
- **In-session exclusion** for the "Another" button uses a 30-minute rolling window on the `suggestions` table — no separate session state needed.

## 07 Config (config.toml)

```toml
[telegram]
# bot token loaded from env var TELEGRAM_BOT_TOKEN

[schedule]
weekday_pings = ["15 17 * * 1-5"]
weekend_pings = ["0 11 * * 6,0", "30 15 * * 6,0", "0 20 * * 6,0"]
followup_hours = 1.5
heartbeat = "0 9 * * *"

[behavior]
suggestions_per_session_max = 5
recent_repeat_blackout_days = 3
suggestion_timeout_hours = 12
session_window_minutes = 30

[database]
path = "/data/alone-bot.db"
```

Timezone is `America/New_York`, set in `scheduler.py` (not config).

## 08 Project layout

```
/opt/alone-bot/                              # on MainLXC host
├── .env                                     # TELEGRAM_BOT_TOKEN (mode 600, gitignored)
├── .gitignore
├── .git/                                    # local repo
├── .venv/                                   # local Python venv for Pylance (gitignored)
├── Dockerfile
├── docker-compose.yml
├── requirements.txt
├── config.toml
├── data/                                    # mounted to /data in container
│   └── alone-bot.db                         # SQLite, persists across rebuilds
└── alone_bot/                               # Python package
    ├── __init__.py
    ├── main.py                              # entrypoint, drives bot + scheduler
    ├── bot.py                               # handlers: /start, /suggest, /add, /list, /stats, callbacks
    ├── scheduler.py                         # APScheduler jobs (pings, followups, sweep, heartbeat)
    ├── db.py                                # SQLite access layer
    ├── selector.py                          # activity-picking logic
    └── seed_data.py                         # initial 25 activities
```

## 09 Operating notes

**Start / stop / rebuild**
```bash
cd /opt/alone-bot
docker compose up -d --build      # rebuild + start
docker compose down               # stop + remove container
docker compose restart alone-bot  # restart in place
docker logs -f alone-bot          # follow logs
```

**Reset database**
```bash
docker compose down && rm /opt/alone-bot/data/alone-bot.db && docker compose up -d --build
```
Schema gets recreated + activities reseeded. Need to send `/start` again to re-bind chat ID.

**Inspect DB directly**
```bash
sqlite3 /opt/alone-bot/data/alone-bot.db "SELECT id, text FROM activities ORDER BY id;"
sqlite3 /opt/alone-bot/data/alone-bot.db "SELECT id, activity_id, response, completed FROM suggestions ORDER BY id DESC LIMIT 10;"
```

**Backup**
SQLite file lives at `/opt/alone-bot/data/alone-bot.db`. Proxmox-level LXC snapshot covers it; no separate backup story needed for v1.

## 10 Bot health monitoring

The bot sends a Telegram message every morning at 9am saying "alone-bot heartbeat ✓". If I don't get it, the bot is down. Zero extra infrastructure.

The bigger health-dashboard project (replacement for the dead CasaOS-era Telegram cron, covering all of MainLXC's services) is deferred. When that's built, it can absorb this responsibility.

## 11 v2 — LLM layer (planned)

Once v1 has run for a few weeks, layer Ollama into its own LXC for:
- **Generate new suggestions** when the list is exhausted or all recent items got rejected
- **Context-aware suggestions** — accept optional context with `/suggest <context>` (e.g. `/suggest low energy, 20 min`)
- **Propose list additions** — periodically pitch new activities for approval via inline button

The LLM never writes directly to the DB. It proposes; deterministic code commits on approval.

Pre-requisites for v2:
- Create OllamaLXC (Debian 12, dedicated LXC for inference isolation)
- Install Ollama with `OLLAMA_HOST=0.0.0.0`
- Pull `llama3.2:3b` (or `llama3.2:1b` if 3b is too slow on CPU)
- Add LLM-related code to alone-bot

## 12 Lessons / decisions worth remembering

- **SQLite footgun: `id NOT IN (NULL)`** returns NULL (not TRUE) for every row, silently filtering everything out. When building dynamic `IN` clauses, omit the clause entirely when the exclude list is empty rather than passing `NULL`.
- **AsyncIOScheduler over JobQueue** — APScheduler's standalone AsyncIO scheduler accepts cron strings directly via `CronTrigger.from_crontab()`, no translation needed from config.
- **DB as job state** — instead of using a persistent APScheduler job store, query the DB on startup to restore pending one-shot jobs. Simpler, fewer moving parts. Added `followup_sent_at` column so restart never re-fires already-sent followups.
- **Inline buttons map 1:1 to data model** — `callback_data` strings match the `response` column values exactly. No translation layer needed.
- **Items never leave the pool** — completion is tracked, but every activity stays eligible forever (blackout window aside). The behavior is "the bot has a list it cycles through with cooldowns," not "the bot has a list it works through."
