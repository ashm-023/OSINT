# Signal Console — How It Works

A single-file FastAPI app (`app.py`) that does competitive-intelligence OSINT:
pulls people/posts from LinkedIn and X, runs a multi-source "Company
Intelligence" pipeline (LinkedIn + X + website + Google Trends + any custom
sources you add), sends it all to Gemini for analysis, and serves an embedded
HTML/JS frontend — no separate frontend build, no database, just JSON files
on disk for caching.

---

## 1. Requirements

```bash
pip install fastapi uvicorn requests python-dotenv pydantic python-multipart
# optional, only used as a Trends fallback if you don't have a ScrapeBadger key:
pip install pytrends
```

### Environment variables (`.env` in the same folder as `app.py`)

| Variable | Required for | Notes |
|---|---|---|
| `APIFY_API_TOKEN` | LinkedIn people/posts (both tabs, and Company Intelligence exec discovery) | Apify account token. The actors used are paid — `harvestapi~linkedin-company-employees`, `harvestapi~linkedin-profile-posts`, `harvestapi~linkedin-company-posts`, plus Instagram/Facebook scraper actors if used |
| `SCRAPEBADGER_API_KEY` | X/Twitter posts, Google Trends | scrapebadger.com key. If unset, Trends silently falls back to the unofficial `pytrends` library, and X features fail |
| `GEMINI_API_KEY` | AI Insights synthesis, custom-source config extraction/debugging | Google AI Studio key |
| `GEMINI_MODEL` | optional | defaults to `gemini-2.5-flash` |
| `INSTAGRAM_ACTOR_ID` | optional | defaults to `apify~instagram-scraper` |
| `FACEBOOK_ACTOR_ID` | optional | defaults to `apify~facebook-posts-scraper` |

None of these are hard-required to boot the server — every source call is
wrapped in try/except, so missing keys just mean that specific source returns
an error in the `errors` panel instead of crashing the run. `GET /api/health`
reports which keys are actually present.

### Run it

```bash
uvicorn app:app --reload --port 8000
# open http://localhost:8000
```

**Restart the server after editing `.env`** — `load_dotenv()` only runs once
at import time; changing the file while the process is already running does
nothing until you restart.

### Files it writes to disk (auto-created, all JSON, all gitignore-worthy)

| File | What it holds |
|---|---|
| `search_history.json` | Recent searches across all tabs (capped at 100) |
| `company_cache.json` | Every company you've searched on the LinkedIn tab, with cached people + logo |
| `intelligence_cache.json` | Every completed Company Intelligence report, keyed by company slug |
| `sources/*.json` | One file per custom pluggable data source (see §6) — **may contain plaintext API keys**, don't commit this folder |

---

## 2. Two ways to look people up

### LinkedIn tab
Type a company name or LinkedIn URL → `/api/profiles` → Apify's
`linkedin-company-employees` actor → normalized into a common person shape
(name, headline, position, seniority tier, photo, email, location, LinkedIn
URL) → auto-cached to `company_cache.json`. Click a person → `/api/posts` →
their recent posts in a coverflow viewer.

**Seniority tiers** (`C-Suite`, `VP`, `Director`, `Manager`, `Individual
Contributor`) are inferred from title/headline text via keyword matching —
see `_seniority_tier()`.

### False-positive filtering (`filter_profiles_by_company`)
The underlying LinkedIn actor sometimes returns people who aren't actually
current employees — group members, loose name matches, someone who just
mentions the company. Every profile's *reported employer* is checked against
what you searched:

- Employer matches (exact, substring, or fuzzy ≥72% similarity, suffix-
  normalized so "Shopify" == "Shopify Inc.") → kept, `company_match_verified: true`
- Employer present but genuinely different → **dropped**
- No employer field at all → falls back to checking if the person's own
  headline/position *names* the company (word-boundary matched, so "Apple"
  doesn't match "Pineapple Consulting") → kept if it does
- Still unverifiable:
  - **Lenient mode** (regular LinkedIn tab): kept but flagged `verified: false`
  - **Strict mode** ("Execs only" toggle, and always inside Company
    Intelligence): **dropped** — an unverifiable stranger shouldn't become a
    named persona whose posts get attributed to the company

Company Intelligence adds a second gate on top: since it explicitly asks the
actor for C-level/VP job titles, anyone whose own title tier isn't
`C-Suite`/`VP` is filtered out even if their employer matches — a real
employee who isn't actually an executive doesn't belong in "Tier 2: named
executives."

### X / Twitter tab
Handle → `/api/x` → ScrapeBadger's Twitter API → recent tweets in the same
coverflow viewer.

### Batch Import
Upload an Apollo-style people-export CSV (First Name, Last Name, Title,
Company, LinkedIn URL, Seniority, ...) → parsed client-side-free on the
server (`parse_batch_csv`) → grouped into companies → each company
auto-cached → posts pulled live per person → one combined CSV download.
Preview first (`/api/batch/preview`) to sanity-check the parse before
spending API calls.

---

## 3. Company Intelligence (the core feature)

One input — company name + time filter (1 day / 1 week / 1 month) — 
orchestrates everything via `run_company_intelligence()`:

### Collection order (Tier 1 → Tier 2, with failsafes)

1. **Tier 1 — official/company-level sources**, each in its own try/except
   so one failing never blocks the rest:
   - Company website (best-effort scrape + dependency-light HTML stripping)
   - Official LinkedIn company page posts
   - X: handle-guess + a direct text search for the company name (deduped
     against each other)
   - **Any enabled custom sources** (`sources/*.json` — see §6)
   - Google Trends (search-interest data, not a "post")
2. **Tier 2 — named executives.** Discovers C-level/VP profiles via Apify,
   runs them through the strict two-gate filter above, then pulls each
   exec's LinkedIn + X posts (again, one exec failing doesn't block others)
3. **Time filter** — posts outside the selected window get dropped, except
   posts with an unparseable date (kept rather than silently lost)
4. **Gemini synthesis** — the compiled text (+ Trends context) goes to
   Gemini, which returns `summary`, `metrics` (capped 5–9, enforced in code
   even if the model ignores the prompt), `sentiment`, and
   `strategy_insights` — the prompt explicitly asks it to contrast internal
   company messaging against external Google Trends interest
5. Every completed run (partial or full) is cached by company slug, so
   revisiting it later costs zero API calls

### Frontend feed grouping (3 modes, same underlying data)

- **Newest First** — every persona's posts merged, sorted by date, in a
  coverflow slider
- **By Persona** — a labeled section per person (Official Page, CEO, CFO...)
  with their posts stacked underneath
- **By Tier** — two sections (Tier 1 official / Tier 2 executives), each
  broken down by persona inside

### Company logo
Pulled from whichever matched exec's employer field had one; falls back to
whatever `company_cache.json` already has for that company if exec discovery
came back empty. Shown in the intel header, the "Previously Analyzed" grid
(which lives on the **Company Profiles** page, not the Intelligence page),
and the plain Company Profiles cards.

---

## 4. Export

Every Company Intelligence result can be exported as CSV or JSON — either
from the UI (client-side, from the already-fetched result) or by re-running
via `POST /api/intelligence/export?fmt=csv|json` for direct API/script use.

---

## 5. API endpoint reference

| Method | Path | Purpose |
|---|---|---|
| GET | `/api/health` | Which API keys are configured |
| POST | `/api/profiles` | LinkedIn people search for a company |
| POST | `/api/posts` | Posts for one LinkedIn profile URL |
| GET/DELETE | `/api/history` | Recent-search log |
| GET | `/api/companies` | All cached companies (LinkedIn tab) |
| GET | `/api/companies/{slug}` | One cached company's people |
| POST | `/api/x` | Tweets for a handle |
| POST | `/api/automate` | Tier-hierarchy CSV report for one cached company |
| POST | `/api/batch/preview` | Parse a people CSV, no API calls made |
| POST | `/api/batch/automate` | Full batch run → combined CSV |
| POST | `/api/intelligence` | Run Company Intelligence fresh |
| GET | `/api/intelligence/cached` | List of previously run reports |
| GET | `/api/intelligence/cached/{slug}` | One cached report, instant |
| POST | `/api/intelligence/export` | Re-run + stream as CSV/JSON |
| GET | `/api/sources` | List custom sources (keys redacted) |
| POST | `/api/sources/extract` | Paste API docs → Gemini drafts a config |
| POST | `/api/sources/test` | Dry-run a draft config, see sample output |
| POST | `/api/sources/debug` | Diagnose why Test failed or returned 0 items |
| POST | `/api/sources` | Save a config to `sources/` |
| POST | `/api/sources/{name}/toggle` | Enable/disable a source |
| DELETE | `/api/sources/{name}` | Delete a source config |

---

## 6. Pluggable custom sources (the "modularity" layer)

Adding a new data source (Reddit, a news API, anything REST/JSON) doesn't
require touching `app.py`'s core logic. Each source is a JSON config file in
`sources/`, and one generic engine (`fetch_config_source` /
`normalize_config_item`) reads the config, builds the HTTP request, and maps
the response onto the standard post shape (`text`, `date`, `url`,
`reactions`, `comments`). Enabled configs run automatically as Tier 1
sources on every Company Intelligence search via `collect_config_sources()`.

### Building one, via the UI ("Data Sources" tab)

1. **Paste API docs** (+ optional API key, + optional hints like "company
   name goes in the subreddit slot") → **Extract with Gemini** → returns a
   draft JSON config
2. **Test** against a sample company → calls the real API, shows normalized
   sample output, so you confirm the field mapping actually works
3. **Debug** (if Test fails, or succeeds with 0 items):
   - Fast, free, rule-based checks run first — catches the common stuff
     instantly with no API call: missing `auth.key_value`, RapidAPI's
     dual-header requirement (`X-RapidAPI-Host` + `x-rapidapi-key`),
     error-code-specific guidance (401/403/404/timeout/non-JSON-response)
   - If the call *succeeded* but returned 0 items, it fetches the **real raw
     API response** and shows it to you — usually reveals that
     `response_path` was guessed wrong, or that the endpoint you hit doesn't
     return post data at all (e.g. a `/profile` endpoint that only returns
     bio/follower-count metadata)
   - Gemini gets the raw response too, and can propose a corrected config
     directly ("Apply suggested fix" button)
   - **Debug tracks history per draft session** — pressing Debug again after
     applying a fix that didn't work explicitly tells Gemini "this diagnosis
     already failed, don't repeat it, explain why it didn't work and propose
     something different." The UI shows an "attempt #N" banner and a
     collapsible list of everything tried so far
4. **Save** → written to `sources/<name>.json`, runs live immediately.
   Enable/disable and delete from the list below the editor

### Config schema

```jsonc
{
  "name": "reddit",                 // unique id, also the filename
  "display_name": "Reddit",
  "tier": 1,                        // 1 = official/company, 2 = per-exec (rare for custom)
  "enabled": true,
  "target_type": "name",            // "name" -> company name, "handle" -> guessed @handle
  "method": "GET",
  "base_url": "https://oauth.reddit.com",
  "list_endpoint": "/r/{target}/new.json?limit={max_items}",  // {target}/{max_items} tokens
  "headers": { "User-Agent": "SignalConsole/1.0" },
  "query_params": {},
  "body": null,                     // POST only; supports the same tokens
  "auth": {
    "type": "bearer | header | query | none",
    "key_value": "sk-...",          // inline key — this IS the "paste a doc with your key" flow
    "key_env": "REDDIT_API_TOKEN",  // OR read from an env var instead of inlining
    "header_name": "x-api-key",     // for type=header
    "param_name": "api_key"         // for type=query
  },
  "response_path": "data.children[].data",  // dotted path; a segment ending in [] iterates a list;
                                             // "" means the response body itself is the list
  "url_prefix": "https://reddit.com",       // prepended if item urls are relative
  "date_is_unix": false,                     // true if the date field is a unix timestamp (seconds)
  "field_map": {
    "text": "selftext", "date": "created_utc", "url": "permalink",
    "reactions": "ups", "comments": "num_comments"
  },
  "unverified": true
}
```

`field_map.text` is the only required field mapping; everything else is
optional and defaults to empty/0.

### Design boundary — why this is config, not code-generation

Gemini extracts **declarative JSON**, never Python that gets executed. A
single fixed fetcher function reads that JSON and does the work. This covers
most REST/JSON APIs cleanly, and it means a malicious/broken "API doc" can
only produce a bad config (caught by `validate_source_config` / a failed
Test call) — never arbitrary code execution. APIs needing real auth
handshakes (OAuth flows, signed requests, cookie sessions) or non-JSON
responses are outside what this layer can do; those would need a
hand-written Python source instead.

### Security note

Inline `auth.key_value` keys are stored in **plaintext** in `sources/*.json`
so the "paste a doc with the key" flow works. They're redacted in every API
response and UI display (`_redact_source_config` masks them), but the file
on disk is not encrypted. Fine for local/personal use; if this folder is
ever version-controlled, gitignore it or switch that source to `key_env`
instead.

---

## 7. Frontend structure

Single embedded HTML/JS string (`INDEX_HTML`) served at `GET /`. No build
step, no separate frontend project. Views are plain `<section>` elements
toggled via a small `showView()`/`navigateTo()`/`goBack()` nav-stack system:

- **Company Intelligence** (home view) — search bar, time filter, results
- **LinkedIn** — company search, hierarchy view, direct profile lookup
- **X / Twitter** — handle search
- **Company Profiles** — gallery of every company you've looked up, plus
  "Previously Analyzed" intelligence reports
- **Batch Import** — CSV upload → preview → automate
- **Data Sources** — the pluggable-source builder described in §6

All state lives in plain JS variables (no framework), and all styling is
one embedded `<style>` block using CSS custom properties for the dark theme.
