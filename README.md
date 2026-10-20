# AROI — African Research Opportunity Intelligence

Finds new South African agricultural research every day, works out which findings could become a
business, checks them against the real market with live web search, scores them, and emails you the
best few each Monday.

```
OpenAlex ──► 1 Discover ──► 2 Triage ──► 3 Analyse ──► 4 Generate ──► 5 Validate ──► 6 Score ──► Digest
             (filters,      (Haiku,      (Sonnet,       (≤3 ideas,     (Sonnet +      (Sonnet      (Monday
              dedup)         0-10)        quotes         merged         web search,    criteria,     email)
                                          checked)       across         cited URLs     total in
                                                         papers)        only)          code)
                         all stages read/write one Supabase Postgres database
```

Pilot scope: South Africa · agriculture & food · lean budget (default hard cap $20/month).

---

## How this differs from the blueprint, and why

The blueprint's design is sound and is kept: one shared database, structured agent outputs, the
seven-criterion rubric with the same weights, the score computed in code, evidence grade kept
separate from the score, and a human gate before anything costs real money. These changes fix
things that were outdated or would have caused problems:

| Blueprint | Built | Why |
|---|---|---|
| n8n workflows | Python package run on GitHub Actions | n8n needs an always-on server; Actions is free for this volume, versioned and testable. Same four workflows. |
| OpenAlex key "optional" | Key required | Since Feb 2026 OpenAlex requires a free key ($1/day free usage). |
| Keyword `search=` as the main query | Country + field **filter sweep** first, keywords second | Filters cost 10× less than search, and the sweep catches every new SA agri paper, not just ones using your words. |
| Query template had no country filter | `institutions.country_code:za` | Without it you get global results. |
| Market validator "uses retrieved sources" | Claude web search; only URLs from the API's own citation blocks are stored | Stops invented links. Model-claimed sources that don't match a retrieved page are moved to "unknowns". |
| Evidence quotes trusted | Every quote checked verbatim against the source text | Unverifiable quotes are flagged and cap the paper's confidence. |
| IP: "who owns it?" | IPR-PFRD Act pathway generated per opportunity | SA universities own most publicly funded IP; the route is a licence via the tech transfer office. |
| Crossref enrichment | Dropped for v1 | OpenAlex already ingests Crossref; it added calls without new information. |
| Full text | GROBID full text from OpenAlex ($0.01/paper) for analysed papers only | Better evidence quotes for a cent; abstracts used otherwise. |
| Supabase table view | Plus RLS on all tables, an `opportunity_board` view, and a phone-friendly "Record a decision" workflow | Supabase exposes `public` via its REST API; RLS blocks the anon key. |

---

## Setup (about 30 minutes)

### 1. Accounts and keys
1. **OpenAlex** — create an account at openalex.org, copy the key from Settings → API.
2. **Claude API** — platform.claude.com → API keys. Add ~$10 credit and **set a monthly spend limit
   in the Console** as a second safety net beside the code's own budget guard.
3. **Supabase** — new project (free tier). Choose a region near you. Save the database password.
4. **Gmail app password** (for the digest) — Google Account → Security → 2-Step Verification →
   App passwords. Use it as `SMTP_PASSWORD`.

### 2. Create the database
Supabase → SQL Editor → paste and run `sql/schema.sql`, then `sql/seed.sql`.
(Or locally: `pip install . && python -m aroi init-db`.)

### 3. Put the code on GitHub
Create a **private** repository and push this folder. Private repos get 2,000 free Actions
minutes a month; this uses roughly 150–300. (Public repos also work, but GitHub disables
scheduled workflows on public repos after 60 days without commits.)

### 4. Add secrets
Repository → Settings → Secrets and variables → Actions → New repository secret:

| Secret | Value |
|---|---|
| `DATABASE_URL` | Supabase → **Connect** → **Session pooler** string (the direct connection is IPv6-only and won't work from GitHub) |
| `OPENALEX_API_KEY` | from step 1 |
| `CONTACT_EMAIL` | your email (sent to OpenAlex as a courtesy) |
| `ANTHROPIC_API_KEY` | from step 1 |
| `SMTP_USER`, `SMTP_PASSWORD`, `DIGEST_TO` | your Gmail address, the app password, where to send the digest |

Optional: under **Variables**, set `MONTHLY_BUDGET_USD` (default 20).

### 5. First run
Actions → **Daily research pipeline** → Run workflow. The first run looks back 90 days, so it
finds a backlog (often 1,000+ papers); triage clears up to 300 a day, so the queue settles within
a week. Then Actions → **Weekly digest** → Run workflow to get your first email.

From then on it runs by itself: pipeline daily at 05:17, digest Mondays at 06:41 (SA time).

---

## Using it

**Read the digest.** Each lead shows the customer, product, score, evidence grade, blocking risks,
the next action, the cheapest test that could kill the idea, the IP route, the source papers and
the market pages the agent actually read.

**Record decisions — this is what makes it improve.** From your phone: GitHub app → Actions →
**Record a decision** → enter the 8-character id from the digest, pick `promising`/`rejected`/…,
and say why. Or on a laptop: `python -m aroi review 6c8f9b1c rejected "PICS bags already cheaper"`.
Your decisions appear in the next digest and build the dataset you'll calibrate the scoring on.

**Browse everything** in Supabase → Table editor → `opportunity_board` (view), `papers`,
`evidence_items`, `agent_runs` (every model call with its cost).

**Local commands** (with a `.env` copied from `.env.example`):
```
python -m aroi status            # counts and month-to-date spend
python -m aroi list --min-score 55
python -m aroi daily --only discover triage
python -m aroi digest --no-email # writes out/digest-*.html
```

**Edit what it searches:** the `search_queries` table (add rows, or set `active=false`).
To widen the sweep, set `SWEEP_FIELDS=11|23` (adds Environmental Science).

---

## What it costs

| Item | Typical month |
|---|---|
| OpenAlex (sweep + 12 searches/day + ~5 full texts/day) | $0 — inside the free $1/day |
| Triage, Haiku 5.5 (~20–60 papers/day) | under $0.20 |
| Analysis + idea generation, Sonnet 5.5 (≤5 papers/day) | $3–6 |
| Market validation incl. web search at $10/1,000 (≤2/day) | $4–9 |
| Scoring, Sonnet 5.5 | about $1 |
| Supabase free tier, GitHub Actions | $0 |
| **Total** | **≈ $8–16**, hard-capped by `MONTHLY_BUDGET_USD` |

Estimates from Oct 2026 list prices; check `agent_runs` for your real cost per paper after week one.
The guard stops all model calls once the month's logged spend reaches the cap, and each run stops at
`RUN_BUDGET_USD`. Supabase's free tier pauses after 7 days of inactivity; the daily run prevents that.

---

## Running the pilot well

1. **Week 1:** let the backlog clear. Skim 20 triaged-out papers in `papers` to check nothing good is
   being dropped; adjust `TRIAGE_THRESHOLD`.
2. **Week 2:** open 10 `findings` rows and check them against the papers. `quote_verified=false`
   rows are where to look first.
3. **Week 3:** build a gold set — 30–50 papers you've judged yourself, including ones that should
   produce nothing. When you change a prompt in `aroi/prompts.py`, bump its `*_VERSION` and compare.
4. **Week 4:** pick the two or three best leads and do the "cheapest disproving test" for real —
   five customer calls, or one email to the tech transfer office.

An empty digest is a valid result. The prompts are written to return nothing rather than invent
an opportunity.

---

## Project layout
```
sql/schema.sql            tables, indexes, RLS, opportunity_board view (idempotent)
sql/seed.sql              12 starter keyword searches from the blueprint's taxonomy
aroi/openalex.py          API client: cursor paging, retries, budget errors, GROBID full text
aroi/normalize.py         abstract rebuild, DOI/title dedup keys, TEI parsing, quote verification
aroi/llm.py               structured-output and web-search calls, cost log, budget guard
aroi/prompts.py           all agent prompts + SA country context (versioned)
aroi/schemas.py           JSON schemas for every agent's output
aroi/scoring.py           weights, total score, evidence-grade cap, recommended action
aroi/stages/              discover · triage · analyze · opportunities · validate · score
aroi/digest.py            weekly HTML email
.github/workflows/        daily pipeline · weekly digest · record a decision · tests
tests/                    20 tests; the pipeline test runs every stage against real Postgres
```

Run tests: `TEST_DATABASE_URL=postgresql://… pytest` (uses a throwaway database — it drops the schema).

## Troubleshooting
- **Discover stage: OpenAlex 400** — the filter syntax changed. Try the sweep URL from the log in a
  browser with your key; the `primary_topic.field.id` and `type` filters are the likely culprits.
- **429 "budget"** — OpenAlex daily allowance used; it resets at 02:00 SA time. Lower `MAX_SWEEP_PAGES`.
- **"Monthly budget reached"** — working as designed; raise `MONTHLY_BUDGET_USD` or wait for the 1st.
- **Opportunity stuck at `failed`** — set `pipeline_status` back to `generated` in the table editor to retry.
- **No email** — check the three SMTP secrets; the digest is always saved as a workflow artifact too.

## Expanding later
Countries: add a `COUNTRY_CONTEXT` entry in `prompts.py`, set `TARGET_COUNTRY`, and run a second
scheduled job. Sectors: add `search_queries` rows with a new `sector`, set `SECTOR` and the matching
`SWEEP_FIELDS` (e.g. 21 Energy, 22 Engineering, 17 Computer Science). Keep the options-trading idea
a separate project: it needs backtesting infrastructure, not this pipeline.
