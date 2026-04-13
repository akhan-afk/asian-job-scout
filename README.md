# JobAgent

> Automated multilingual job aggregator and AI assistant for the Asian job market — Japan, Korea, Thailand.

The system scrapes native-language job boards, translates every field to English, generates AI summaries with Mistral, stores results in MongoDB Atlas, and delivers them through a stateful Telegram bot powered by a LangGraph ReAct agent. Subscribed users receive proactive push notifications whenever fresh listings are scraped.

---

## Business Value

Job seekers targeting Japan, Korea, and Thailand face three compounding problems:

1. **Language barrier** — listings are in Japanese, Korean, or Thai; English postings are rare and often filtered out.
2. **Fragmented boards** — Rikunabi, Wanted, and JobsDB each have different UIs with no cross-platform aggregation.
3. **High noise** — raw listings are verbose; comparing dozens of jobs across markets is time-consuming.

JobAgent eliminates all three:

| Problem | Solution |
|---|---|
| Language barrier | Every field (title, company, location, salary, description) is machine-translated at scrape time; Mistral AI produces clean English summaries |
| Fragmented boards | Single Telegram interface surfaces jobs from all three markets simultaneously |
| High noise | AI summaries condense each listing to 5 key fields; natural language search filters by role, skill, or location in plain English |
| Manual polling | Push notifications alert subscribed users the moment new listings land — no daily checking required |

---

## How It Works

```
┌─── DATA PIPELINE ────────────────────────────────────────────────────┐
│                                                                      │
│  Playwright + BS4          Google Translate             Mistral AI   │
│  ┌─────────────┐           ┌─────────────┐           ┌────────────┐  │
│  │  Scrapers   │ ─────────►│  Translate  │ ─────────►│ Summarize  │  │
│  │  3 regions  │           │  all fields │           │ 5-field EN │  │
│  └──────┬──────┘           └─────────────┘           └─────┬──────┘  │
│         │                                                   │         │
│    MongoDB: jobs                                  MongoDB: summaries  │
│    + data/*.txt                                   + data/*.txt        │
└──────────────────────────────────────────────────────────────────────┘
                                          │
                                          ▼
┌─── TELEGRAM BOT (live on Render) ────────────────────────────────────┐
│                                                                      │
│  User  ──►  Guardrails  ──►  LangGraph Agent (StateGraph)            │
│                                    │                                 │
│                           _should_summarize?                         │
│                           /               \                          │
│                 summarize_node          agent_node                   │
│                 (llm_plain)           (llm + tools)                  │
│                           \               │                          │
│                             ──────────────►                          │
│                                           │                          │
│                                   tools_condition                    │
│                                   /             \                    │
│                               ToolNode          END                  │
│                           (5 tools)      (reply sent)                │
│                               │                                      │
│                     ┌─────────┴──────────┐                           │
│                  MongoDB              Formatting                      │
│                 (checkpoints)          gateway                        │
└──────────────────────────────────────────────────────────────────────┘
```

---

## Architecture Overview

The system has three independent runtime contexts:

| Context | Trigger | Key components |
|---|---|---|
| **Bot (persistent)** | `uv run python -m bot.main` / Render | LangGraph agent, Telegram polling, MongoDB, health server |
| **Pipeline (background)** | Auto-scheduled inside bot process, or `uv run run_pipeline.py` manually | Pipeline StateGraph, Playwright, Mistral AI, MongoDB |
| **CLI stages** | `uv run run_scraper.py` / `uv run run_summarizer.py` | Playwright, BS4, deep-translator, Mistral AI |

---

## Project Structure

```
jobAgent/
│
├── bot/                          # Telegram bot + LangGraph agent
│   ├── agent.py                  # StateGraph: _should_summarize → summarize_node → agent_node → ToolNode
│   ├── formatting.py             # send_reply() — single formatting + sending gateway for all outbound messages
│   ├── guardrails.py             # Input guardrails (injection, length) + output guardrails (CLI leak, truncation)
│   ├── handlers.py               # Telegram command and free-text message handlers
│   ├── health.py                 # Threading HTTP health server + self-ping keep-alive loop (Render)
│   ├── main.py                   # Bot init, polling, background task orchestration
│   ├── observability.py          # Optional Langfuse CallbackHandler integration
│   └── tools.py                  # LangGraph tool definitions (MongoDB-first, txt fallback)
│
├── pipeline/
│   └── orchestrator.py           # Pipeline StateGraph: check_freshness → scrape_summarize → notify_users
│
├── db/
│   ├── client.py                 # MongoClient singleton, collection accessors, ensure_indexes()
│   └── models.py                 # Pydantic models: UserDocument, JobDocument, SummaryDocument
│
├── scrapers/
│   ├── base.py                   # BaseScraper ABC, JobListing dataclass, translate(), save_jobs()
│   ├── japan/rikunabi.py         # Rikunabi new-grad listings (Playwright, Japanese → EN)
│   ├── korea/wanted.py           # Wanted.co.kr (React SPA, JS rendering, Korean → EN)
│   └── thailand/jobsdb.py        # JobsDB Thailand (Thai → EN)
│
├── summarizers/
│   └── summarizer.py             # parse_jobs_file(), summarise_jobs(), save_summaries()
│
├── data/                         # Flat-file output (gitignored *.txt)
│   ├── japan/
│   ├── korea/
│   └── thailand/
│
├── run_bot.py                    # Entry point: start the bot
├── run_pipeline.py               # Entry point: manual pipeline trigger (--force, --region, --no-notify)
├── run_scraper.py                # Entry point: scrape one region
├── run_summarizer.py             # Entry point: summarize one region
│
├── architecture.html             # System architecture diagram
├── langgraph-diagram.html        # LangGraph StateGraph workflow diagram
├── render.yaml                   # Render deployment configuration
├── pyproject.toml                # Python dependencies (managed with uv)
└── .env.example                  # Required environment variables template
```

---

## Tech Stack

### Core AI & Agent Framework

| Package | Version | Purpose |
|---|---|---|
| `langgraph` | `≥ 1.1.4` | ReAct StateGraph for the bot agent and pipeline orchestrator |
| `langchain-mistralai` | `≥ 1.1.2` | LangChain wrapper for ChatMistralAI with tool-calling support |
| `langgraph-checkpoint-mongodb` | `≥ 0.3.1` | `MongoDBSaver` — durable per-user conversation state in Atlas |
| `mistralai` | `≥ 1.12.4` | Direct Mistral API client for job summarisation |

### Telegram Bot

| Package | Version | Purpose |
|---|---|---|
| `python-telegram-bot` | `≥ 22.6` | Telegram Bot API client — long polling, command routing, async |

### Data & Storage

| Package | Version | Purpose |
|---|---|---|
| `pymongo` | `≥ 4.15.5` | MongoDB Atlas client — jobs, summaries, users, checkpoints |

### Scraping & Translation

| Package | Version | Purpose |
|---|---|---|
| `playwright` | `≥ 1.58.0` | Headless Chromium for JS-heavy job boards |
| `beautifulsoup4` | `≥ 4.14.3` | HTML parsing of individual listing pages |
| `deep-translator` | `≥ 1.11.4` | Google Translate — all raw fields translated to English at scrape time |

### Observability & Utilities

| Package | Version | Purpose |
|---|---|---|
| `langfuse` | `≥ 2.0.0` | LLM observability: traces, token usage, latency, tool calls per conversation |
| `python-dotenv` | `≥ 1.2.2` | `.env` file loading |
| `schedule` | `≥ 1.2.2` | Pipeline scheduling support |

### Deployment

| Tool | Purpose |
|---|---|
| **Render** (free tier) | Hosts the bot as a persistent web service |
| **MongoDB Atlas** (free tier M0) | Cloud-hosted MongoDB — all data and conversation state |
| **UptimeRobot** | External health monitoring — pings `/health` every 5 minutes |
| **uv** | Fast Python package manager and script runner |

---

## Setup

**Prerequisites:** Python 3.10+, [uv](https://docs.astral.sh/uv/getting-started/installation/)

```bash
# 1. Install dependencies
uv sync

# 2. Install headless browser (required for scraping only)
uv run playwright install chromium

# 3. Configure secrets
cp .env.example .env
# Edit .env — fill in all required values
```

---

## Environment Variables

| Variable | Required | Description |
|---|---|---|
| `BOT_TOKEN` | **Yes** | Telegram bot token — get from [@BotFather](https://t.me/BotFather) |
| `MISTRAL_API_KEY` | **Yes** | Mistral AI key — get from [console.mistral.ai](https://console.mistral.ai) |
| `MONGODB_URI` | **Yes** | Atlas connection string — `mongodb+srv://<user>:<pass>@<cluster>.mongodb.net/jobagent` |
| `MISTRAL_MODEL` | No | Model name (default: `mistral-small-latest`) |
| `PIPELINE_INTERVAL_DAYS` | No | Days between automatic scrape runs (default: `3`; set to `9999` on Render to disable) |
| `LANGFUSE_PUBLIC_KEY` | No | Enables Langfuse LLM observability tracing (optional) |
| `LANGFUSE_SECRET_KEY` | No | Required if `LANGFUSE_PUBLIC_KEY` is set |
| `RENDER_EXTERNAL_URL` | Auto | Set by Render — activates the self-ping keep-alive loop |
| `PORT` | Auto | Set by Render — health server binds to this port |

---

## Running Locally

### Start the bot

```bash
uv run python -m bot.main
```

Starts Telegram long polling, an HTTP health server on port 8080, and the background pipeline scheduler — all in the same asyncio event loop.

### Run the data pipeline manually

```bash
# Scrape + summarize + notify all subscribed users (all regions)
uv run run_pipeline.py

# Force refresh even if data is recent
uv run run_pipeline.py --force

# Skip Telegram notifications
uv run run_pipeline.py --no-notify

# Single region only
uv run run_pipeline.py --region korea
```

### Run individual pipeline stages

```bash
# Stage 1 — scrape (takes a few minutes, one page per listing)
uv run run_scraper.py --region japan      # or korea / thailand

# Stage 2 — summarize with Mistral AI
uv run run_summarizer.py --region japan
```

---

## Telegram Commands

| Command | Description |
|---|---|
| `/start` | Show welcome message and usage guide |
| `/help` | Same as `/start` |
| `/jobs` | Latest job summaries from all regions |
| `/search <keyword>` | Filter jobs by keyword — e.g. `/search backend` |
| `/subscribe` | Opt in to push notifications when new listings are scraped |
| `/unsubscribe` | Stop job alert notifications |
| `/clear` | Reset your conversation history (deletes LangGraph checkpoints) |

**Natural language queries are also supported:**

- "Jobs in Japan"
- "Backend roles in Korea"
- "Any remote roles in Thailand?"
- "What salary should I expect as a software engineer in Tokyo?"
- "How do I write a Japanese-style CV?"
- "Am I subscribed to job alerts?"

---

## Deployment on Render

`render.yaml` defines the Render free-tier web service. Key settings:

| Setting | Value |
|---|---|
| Build command | `pip install uv && uv sync --no-dev` |
| Start command | `uv run python -m bot.main` |
| `PIPELINE_INTERVAL_DAYS` | `9999` (Playwright/Chromium is not installed on Render) |
| Keep-alive | Self-ping loop hits `RENDER_EXTERNAL_URL/health` every 4 minutes to prevent the free-tier 15-minute sleep |

Set `BOT_TOKEN`, `MISTRAL_API_KEY`, and `MONGODB_URI` as secret environment variables in the Render dashboard. Do not commit them to `render.yaml`.

---

## MongoDB Data Model

```
jobagent  (database)
├── jobs               ← raw scraped listings
│                        fields: region, scraped_date, source, url, title, company,
│                                location, salary, deadline, description (all in EN)
│
├── summaries          ← AI-generated summaries
│                        fields: region, summarized_date, source, url, summary,
│                                tags[], stack[], remote
│
├── users              ← /subscribe opt-ins
│                        fields: user_id, username, first_name, subscribed,
│                                regions[], subscribed_at, last_notified
│
├── checkpoints        ← LangGraph conversation state (msgpack binary)
│                        keyed by thread_id = str(telegram user_id)
│                        auto-created for every user who sends any message
│
└── checkpoint_writes  ← LangGraph write-ahead log (in-flight partial states)
```

> The `users` collection is opt-in only — a user appears here only after running `/subscribe`. The `checkpoints` collection is automatic — every conversation is persisted immediately.

---

## Key Design Decisions

### Single formatting gateway (`bot/formatting.py`)
All outbound messages pass through `send_reply()`. Markdown normalisation (converting LLM `**bold**` → Telegram `*bold*`), `ParseMode` decisions, and the plain-text fallback are handled in one place — never scattered across handlers.

### Intent routing (`bot/agent.py`)
`mistral-small-latest` sometimes responds with a greeting instead of calling `list_jobs` on a query like "Jobs in Japan". `_apply_routing_hint()` detects regional/job patterns and appends a direct tool-call instruction to the user message *before* the LLM call. The hint is never persisted to the checkpoint — the conversation history stays clean.

### Conversation summarisation (`bot/agent.py`)
When persisted message count exceeds 20, `summarize_node` fires before the agent. It condenses all but the last 6 messages into a 2–3 sentence summary using `llm_plain` (tools disabled), then removes the old messages via `RemoveMessage`. The agent always sees `[system, summary?, last-6]` — never unbounded history.

### Self-ping keep-alive (`bot/health.py`)
Render's free tier sleeps after 15 minutes of *inbound* HTTP inactivity. A Telegram bot uses outbound polling, so it appears inactive to Render's proxy. `self_ping_loop()` solves this by hitting `RENDER_EXTERNAL_URL/health` every 4 minutes, which registers as inbound traffic and resets Render's inactivity timer.

### Guardrails (`bot/guardrails.py`)
Lightweight regex-based protection with no external dependencies — safe within Render's 512 MB RAM limit. Input check blocks prompt injection patterns and messages over 2 000 chars. Output check strips any accidentally leaked CLI commands and truncates responses to Telegram's 4 096-char message limit.

---

## Adding a New Region

1. Create `scrapers/<region>/` with `__init__.py`
2. Implement a class extending `BaseScraper`:
   - `get_listing_urls()` — return a list of listing page URLs
   - `parse_listing(url)` — return a populated `JobListing`
3. Register it in `run_scraper.py` under `REGION_CONFIG`
4. Add `"<region>": "data/<region>"` to `DATA_REGIONS` in `bot/tools.py`
5. Create `data/<region>/` directory
