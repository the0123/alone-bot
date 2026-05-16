# // alone-bot.log — project doc

**Status** v1 / MVP planning
**Host** Proxmox VE (theodros-ThinkPad-T14)
**Target LXC** MainLXC (192.168.1.10) — bundled with existing services
**Runtime** Docker container, managed via Portainer

---

## 01 What this is

A self-hosted Telegram bot that suggests things to do when alone. On a schedule, asks if I'm alone; if yes, returns a randomly selected activity from a curated list. Inline buttons let me accept, reject, ask for another, or mark "not alone." Logs every interaction for later tuning.

The activity list is seeded from my own list plus the YouTube video "34 Things to Do When You Are Alone." Items stay in the pool permanently — completing one doesn't remove it. A short-term blackout prevents the same activity from being re-suggested within a few days.

v1 is intentionally LLM-free. The LLM layer comes in v2 once the base loop is proven, and will get its own LXC.

## 02 How it works (v1 flow)

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

Triggers:
- **Weekday schedule** — single ping at 5:15pm
- **Weekend schedule** — 2–3 pings across the day (configurable times)
- **On-demand** — `/suggest` command, anytime, skips the "are you alone?" gate
- **Followup** — 1.5 hours after an accepted suggestion, ask if I completed it

Commands:
- `/start` — bind chat ID, confirm bot is alive
- `/suggest` — get a suggestion now
- `/add <text>` — add a new activity to the list
- `/list` — show all activities (paginated if needed)
- `/stats` — show acceptance/completion counts

Inline buttons:
- On "Are you alone?": **Yes** · **No**
- On a suggestion: **✅ Accept** · **🔁 Another** · **❌ Reject**
- On followup: **Yes, did it** · **No, didn't**

The `callback_data` for each button maps 1:1 to the `response` column in the `suggestions` table — see Data Model below. The one value that isn't a button is `no_response`, which a background job writes if a suggestion sits unanswered past a timeout.

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

**Why MainLXC instead of a dedicated LXC:** Resource footprint is trivial (Python + SQLite + bot library, idle most of the time). MainLXC has 7.4 GB / 100 GB disk and CPU load 0.04 — plenty of room. Splitting into its own LXC would be overhead with no benefit at this scale.

**Why Docker instead of systemd:** MainLXC already uses Docker as its management plane (Portainer for everything else). Running the bot the same way keeps it visible in Portainer, lets Homepage auto-discover it, and matches the established pattern.

**Why Ollama in its own LXC later:** Inference is CPU-heavy on the Iris Xe. Isolating it prevents it from starving Homepage/Redlib/etc. during generation. Already in the planned-services list in the home-server log.

## 04 Networking

**Telegram (outbound):** Bot polls `api.telegram.org` via long-polling. No inbound ports, no webhook, no Tailscale. Standard egress from MainLXC is sufficient.

**Bot ↔ Ollama (v2):** Direct LAN over the Proxmox bridge — `http://192.168.1.<ollama-ip>:11434/api/generate`. No Tailscale between LXCs on the same host. Ollama needs `OLLAMA_HOST=0.0.0.0` to accept non-localhost connections.

**Container access:** Bot runs without exposed ports — it only makes outbound calls. Logs viewable via `docker logs alone-bot` or through Portainer.

## 05 Tech stack

| Layer | Choice | Why |
|---|---|---|
| Language | Python 3.11+ | Best Telegram bot ecosystem, async, easy SQLite |
| Bot library | `python-telegram-bot` v21+ | Mature, native inline keyboard + callback query support |
| Scheduler | APScheduler | In-process, persists jobs in SQLite |
| Database | SQLite | Right-sized; no separate DB server |
| Container | Python slim base image | Small, just enough |
| Mgmt | Portainer | Same as everything else on MainLXC |
| Config | `config.toml` + env vars | Bot token via env, rest in config file |
| LLM (v2) | Ollama + llama3.2:3b | Already planned in the home-server stack |

## 06 Data model

```sql
-- Curated list of activities. Items are never removed once added.
-- Completing or rejecting an activity does NOT take it out of the pool.
-- `active = 0` is a soft-delete option if I ever want to retire one
-- without breaking historical suggestion records.
CREATE TABLE activities (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    text        TEXT NOT NULL UNIQUE,
    source      TEXT,                -- 'seed:youtube-34', 'seed:personal', 'user', 'llm' (v2)
    created_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    active      BOOLEAN DEFAULT 1
);

-- Every suggestion the bot makes + the response.
-- response values map 1:1 to inline button callback_data,
-- plus 'no_response' written by the timeout sweeper.
-- activity_id is nullable because 'not_alone' rows have no activity.
CREATE TABLE suggestions (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    activity_id     INTEGER REFERENCES activities(id),
    suggested_at    TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    trigger         TEXT,            -- 'scheduled' | 'on_demand' | 'rerolled'
    response        TEXT,            -- 'accepted' | 'rejected' | 'another' | 'no_response' | 'not_alone'
    response_at     TIMESTAMP,
    completed       BOOLEAN,         -- set by 1.5h followup
    completed_at    TIMESTAMP
);

-- Bot config / state (chat ID binding, last ping time, etc.)
CREATE TABLE state (
    key    TEXT PRIMARY KEY,
    value  TEXT
);
```

Notes:
- **Items stay in the pool forever.** Acceptance/completion is logged, but doesn't remove the activity from future selection.
- **Blackout rule** prevents an activity from being re-suggested within N days (default 3). Implemented as a query filter:
  ```sql
  WHERE id NOT IN (
    SELECT activity_id FROM suggestions
    WHERE suggested_at > datetime('now', '-3 days')
      AND activity_id IS NOT NULL
  )
  ```
- **`trigger = 'rerolled'`** distinguishes a re-pick (after tapping Another) from a fresh scheduled suggestion. Without this, acceptance-rate math gets polluted — re-rolling 4x before accepting would look like 80% rejection rate across different activities.
- **`response = 'not_alone'`** is logged with `activity_id = NULL` (the gate question fires before any activity is picked).

## 07 Config (config.toml sketch)

```toml
[telegram]
# bot token loaded from env var TELEGRAM_BOT_TOKEN, not stored here

[schedule]
# cron expressions — APScheduler reads these
weekday_pings = ["15 17 * * 1-5"]            # 5:15pm Mon-Fri
weekend_pings = ["0 11 * * 6,0",             # 11am Sat/Sun
                 "30 15 * * 6,0",            # 3:30pm Sat/Sun
                 "0 20 * * 6,0"]             # 8pm Sat/Sun
followup_hours = 1.5
heartbeat = "0 9 * * *"                      # daily 9am "I'm alive"

[behavior]
suggestions_per_session_max = 5              # cap re-rolls per session
recent_repeat_blackout_days = 3              # don't repeat within N days
suggestion_timeout_hours = 12                # mark 'no_response' after this

[database]
path = "/data/alone-bot.db"                  # mounted volume in container
```

## 08 Project layout

```
/opt/alone-bot/                              # on MainLXC host
├── docker-compose.yml
├── Dockerfile
├── requirements.txt
├── config.toml
└── data/                                    # mounted to /data in container
    └── alone-bot.db                         # SQLite, persists across rebuilds

# Inside the container:
/app/
├── alone_bot/
│   ├── __init__.py
│   ├── main.py              # entrypoint
│   ├── bot.py               # handlers: /start, /suggest, callback queries
│   ├── scheduler.py         # APScheduler job definitions
│   ├── db.py                # SQLite access layer
│   ├── selector.py          # activity-picking (random + blackout)
│   ├── heartbeat.py         # daily "I'm alive" job
│   └── seed_data.py         # seed activities
```

## 09 v1 build order

1. Register bot with @BotFather, get token
2. Scaffold project (`docker-compose.yml`, Dockerfile, Python venv for local dev)
3. SQLite schema + seed activities (your list + the YouTube 34)
4. `/start` handler — binds chat ID to `state` table, confirms bot is alive
5. `/suggest` handler — picks an activity, sends with inline buttons (Accept / Another / Reject)
6. Callback query handler — writes the response, handles "Another" re-pick loop
7. APScheduler — weekday 5:15pm ping + weekend pings, with the "Are you alone?" gate question first
8. Followup job — 1.5h after accept, ask if completed
9. Timeout sweeper — mark stale suggestions as `no_response`
10. Daily heartbeat job — "I'm alive" message at 9am
11. `/add`, `/list`, `/stats` commands
12. Dockerize, deploy to MainLXC, verify in Portainer + Homepage discovery

Steps 1–6 are the real MVP. 7+ layers on the automation, monitoring, and quality-of-life pieces.

## 10 Bot health monitoring (v1, lightweight)

The bot sends a Telegram message every morning at 9am saying "I'm alive." If I don't get it, I know it's down. Zero extra infrastructure.

The bigger health-dashboard project (replacement for the dead CasaOS-era Telegram cron) is deferred — when that's built, it can absorb this responsibility.

## 11 v2 — LLM layer (later)

Once v1 is stable, layer Ollama into its own LXC for:
- **Generate new suggestions** when the list is exhausted or all recent items got rejected
- **Context-aware suggestions** — accept optional context with `/suggest <context>` (e.g. `/suggest low energy, 20 min`)
- **Propose list additions** — periodically pitch new activities for me to approve via inline button, which then writes through to the DB

The LLM never writes directly to the DB. It proposes; deterministic code commits on approval.

## 12 Open / deferred

- **Multi-user** — single-user (me) for v1. Chat ID binding via `/start` is the gate.
- **Backup** — SQLite file lives in `/opt/alone-bot/data/` on MainLXC, covered by whatever LXC-level Proxmox snapshot strategy gets put in place.
- **Smarter triggers** — Home Assistant presence, calendar busy/free, etc. v3 problem.
- **Full health dashboard** — separate future project. Will absorb the bot's heartbeat once it exists.

---

## Decisions locked for v1

All open questions resolved. Ready to build when you are.
