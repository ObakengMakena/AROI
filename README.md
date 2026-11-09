# AROI — African Research Opportunity Intelligence

Finds new South African research every day across five sectors, works out which findings could become
a business, checks them against the real market with live web research, scores them, and emails you the
best few per sector each Monday.

**v3: no model API.** The judgement stages (triage, analyse, generate, validate, score) are done by
Grok Bot agents working an `agent_tasks` queue in Postgres. Python and GitHub Actions still do
discovery, dedup, quote verification, scoring arithmetic, grade caps and the digest. The agents'
playbook is [`docs/BOT_RUNBOOK.md`](docs/BOT_RUNBOOK.md).

```
OpenAlex ──► 1 Discover ──► 2 Triage ──► 3 Analyse ──► 4 Generate ──► 5 Validate ──► 6 Score ──► Digest
             (filters,      (agent,      (agent;        (agent,        (agent web     (agent       (Monday
              dedup)         0-10)        quotes         ≤3 ideas,      research,      criteria,     email +
                                          checked        merged in      fetched URLs   total in      health)
                                          in code)       code)          only)          code)
   stages 2-6: Actions enqueue tasks → agents `pull` / `submit` JSON → next `tick` validates + ingests
```

Scope: South Africa · default hard cap $30/month on OpenAlex spend (agents cost nothing per call here).

| Sector | What it sweeps (OpenAlex, SA-affiliated authors) | Output |
|---|---|---|
| Agriculture & food | field 11 Agricultural & Biological Sciences | business opportunities |
| Renewable energy & storage | field 21 Energy | business opportunities |
| AI & data products | subfields 1702 AI, 1707 Computer Vision | business opportunities |
| Real estate, construction & materials | subfields 2205 Civil Eng., 2215 Building & Construction, 3322 Urban Studies | business opportunities |
| Quant finance & options | subfield 2003 Finance | **testable hypotheses only** — never trading signals |

Each sector also runs 5–12 keyword searches across Southern/East Africa (or the whole continent for
African-language AI, proptech and finance), and has its own screening guide, readiness scale and
South African market/regulatory notes (e.g. NERSA and NRS 097 for energy, POPIA for AI, Agrément SA
and SANS 10400 for building systems). All of it lives in `aroi/sectors.py`.

**The quant finance track is research-only, by design.** It reads finance papers, extracts the claimed
edge (signal, instruments, data, whether it was tested out-of-sample with costs), lists red flags such
as look-ahead or survivorship bias, and ranks hypotheses by *testability*, not profit. It stops there.
Backtesting needs historical option data (paid for JSE) and a proper testing harness, which is a
separate project to build once a hypothesis earns it.

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
| Market validator "uses retrieved sources" | Agent web research; it must list every source with `fetched: true/false`, and only fetched, public http(s) URLs tied to a claim are stored | Limits invented links. Unfetched or unresolved claims are moved to "unknowns". Caveat: "fetched" is the agent's own report, not independently verified. |
| Evidence quotes trusted | Every quote checked verbatim against the source text | Unverifiable quotes are flagged and cap the paper's confidence. |
| IP: "who owns it?" | IPR-PFRD Act pathway generated per opportunity | SA universities own most publicly funded IP; the route is a licence via the tech transfer office. |
| Crossref enrichment | Dropped for v1 | OpenAlex already ingests Crossref; it added calls without new information. |
| Full text | GROBID full text from OpenAlex ($0.01/paper) for analysed papers only | Better evidence quotes for a cent; abstracts used otherwise. |
| Five independent agents | One pipeline, sector-specific prompts and context, per-sector daily quotas, sector order rotated daily | Same quality bar everywhere; no sector starves another when the run budget is hit. |
| Options agent finds "edges" | Separate hypothesis track scored on testability | A paper's significant result is not evidence of a tradable edge. |
| Supabase table view | Plus RLS on all tables, an `opportunity_board` view, and a phone-friendly "Record a decision" workflow | Supabase exposes `public` via its REST API; RLS blocks the anon key. |

---

## Setup (about 30 minutes)

### 1. Accounts and keys
1. **OpenAlex** — create an account at openalex.org, copy the key from Settings → API.
2. **Grok Bot agents** — no key needed in this repo. Give the agents `AGENT_DATABASE_URL` (the
   `aroi_agent` role) and the runbook; see `docs/BOT_RUNBOOK.md`.
3. **Supabase** — new project (free tier). Choose a region near you. Save the database password.
4. **Gmail app password** (for the digest) — Google Account → Security → 2-Step Verification →
   App passwords. Use it as `SMTP_PASSWORD`.

### 2. Create the database
Supabase → SQL Editor → paste and run `sql/schema.sql`, then `sql/seed.sql`, then `sql/roles.sql`
after replacing its two `CHANGE_ME_*` passwords (least-privilege roles for the pipeline and the agents).
(Or locally: `pip install . && python -m aroi init-db`.)

### 3. Put the code on GitHub
Create a **private** repository and push this folder. Private repos get 2,000 free Actions
minutes a month; this uses roughly 150–300. (Public repos also work, but GitHub disables
scheduled workflows on public repos after 60 days without commits.)

### 4. Add secrets
Repository → Settings → Secrets and variables → Actions → New repository secret:

| Secret | Value |
|---|---|
| `DATABASE_URL` | Supabase → **Connect** → **Session pooler** string, logged in as `aroi_pipeline` (the direct connection is IPv6-only and won't work from GitHub) |
| `OPENALEX_API_KEY` | from step 1 |
| `CONTACT_EMAIL` | your email (sent to OpenAlex as a courtesy) |
| `SMTP_USER`, `SMTP_PASSWORD`, `DIGEST_TO` | your Gmail address, the app password, where to send the digest |

Optional, under **Variables**: `MONTHLY_BUDGET_USD` (default 30), `SECTORS` (default all five),
`ANALYZE_PER_SECTOR` (default 5), `VALIDATE_PER_SECTOR` (default 2). Other tuning is in `.env.example`.

### 5. First run
Actions → **Daily research pipeline** → Run workflow. The first run looks back 90 days across all
five sectors, so it finds a backlog (often a few thousand papers); up to 60 per sector are queued for
agent triage per tick, so throughput depends on how often the agents work the queue. Then Actions → **Weekly digest** → Run workflow to get your first email.

From then on: discovery daily at 05:17, a queue `tick` every 2 hours, digest Mondays at 06:41 (SA time).
Each stage needs one tick to queue it and one to ingest the agents' answers, so a paper takes
about a day from discovery to score when the agents keep up. The digest shows a funnel per sector and
warns if runs stop, fail, or tasks pile up.

---

## Using it

**Read the digest.** Each lead shows the customer, product, score, evidence grade, blocking risks,
the next action, the cheapest test that could kill the idea, the IP route, the source papers and
the market pages the agent actually read.

**Record decisions — this is what makes it improve.** From your phone: GitHub app → Actions →
**Record a decision** → enter the 8-character id from the digest (works for opportunities and
finance hypotheses), pick `promising`/`rejected`/…,
and say why. Or on a laptop: `python -m aroi review 6c8f9b1c rejected "PICS bags already cheaper"`.
Your decisions appear in the next digest and build the dataset you'll calibrate the scoring on.

**Browse everything** in Supabase → Table editor → `opportunity_board` (view), `papers`,
`evidence_items`, `agent_tasks` (the queue, with every rejected submission in `error_log`),
`pipeline_runs` (heartbeat), `agent_runs` (OpenAlex spend).

**Local commands** (with a `.env` copied from `.env.example`):
```
python -m aroi status            # counts and month-to-date spend
python -m aroi list --min-score 55 --sector energy
python -m aroi tick              # ingest agent results, queue the next tasks
python -m aroi.agent_queue stats # queue depth per stage
SECTORS=ai,construction python -m aroi daily   # run just some sectors
python -m aroi digest --no-email # writes out/digest-*.html
```

**Edit what it searches:** the `search_queries` table (add rows with the right `sector`, or set
`active=false`). Don't use `*` wildcards — OpenAlex rejects them; a bad query is skipped and logged.
To change a sweep, edit `sweep_filter` for that sector in `aroi/sectors.py`.
**Turn sectors off:** set the `SECTORS` variable, e.g. `agriculture,energy`.

---

## What it costs

| Item | Typical month |
|---|---|
| OpenAlex (5 sweeps + 37 searches/day + GROBID full texts at $0.01, ≈5/sector/day max) | $0–15, inside or near the free $1/day |
| Agent work (triage, analysis, ideas, validation, scoring) | not billed through this repo |
| Supabase free tier, GitHub Actions (~12 short ticks/day) | $0 |

`MONTHLY_BUDGET_USD` (30) stops discovery and new analyses (which fetch full text) once month-to-date
OpenAlex spend reaches it. Supabase's free tier pauses after 7 days of inactivity; the daily run prevents that.

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
sql/seed.sql              37 starter keyword searches across the five sectors
aroi/sectors.py           sector registry: sweep filters, screening guides, SA market context
aroi/openalex.py          API client: cursor paging, retries, budget errors, GROBID full text
aroi/normalize.py         abstract rebuild, DOI/title dedup keys, TEI parsing, quote verification
aroi/agent_queue.py       task queue + agent CLI: pull, submit (schema-checked), release, stats, schema
aroi/budget.py            OpenAlex monthly budget guard and cost log
sql/roles.sql             least-privilege roles: aroi_pipeline, aroi_agent
docs/BOT_RUNBOOK.md       how agents work the queue: commands, inputs, output schemas, rules
aroi/prompts.py           all agent prompts incl. the finance analyst (versioned)
aroi/schemas.py           JSON schemas for every agent's output
aroi/scoring.py           weights, total score, evidence-grade cap, finance testability score
aroi/stages/              discover · triage · analyze (+finance) · opportunities · validate · score
aroi/digest.py            weekly HTML email
.github/workflows/        daily discovery · 2-hourly tick · weekly digest · record a decision · tests
tests/                    37 tests; the pipeline test runs every stage for every sector against real Postgres
```

Run tests: `TEST_DATABASE_URL=postgresql://… pytest` (uses a throwaway database — it drops the schema).

## Troubleshooting
- **Discover stage: OpenAlex 400** — a sweep or query was rejected. The log names the sector and query;
  try that URL in a browser with your key. One failing sector or query doesn't stop the others.
- **429 "budget"** — OpenAlex daily allowance used; it resets at 02:00 SA time. Lower `MAX_SWEEP_PAGES`.
- **"Monthly budget reached"** — working as designed; raise `MONTHLY_BUDGET_USD` or wait for the 1st.
- **Opportunity stuck at `failed`** — set `pipeline_status` back to `generated` in the table editor to retry.
- **Tasks piling up / digest warns "are the bots running?"** — `python -m aroi.agent_queue stats`; see
  `last_error` on `agent_tasks`. Failed tasks (after `TASK_MAX_ATTEMPTS`) mark their paper `failed`.
- **No email** — check the three SMTP secrets; the digest is always saved as a workflow artifact too.

## Expanding later
**New sector:** add a `Sector(...)` entry in `aroi/sectors.py` (sweep filter, categories, signal guide,
SA context) and keyword rows in `sql/seed.sql`; add its key to `SECTORS`. **New country:** add a
`COUNTRY_CONTEXT` entry in `prompts.py` and a `context` entry for that country in each sector, then
run a second scheduled job with `TARGET_COUNTRY` set. **Backtesting:** once a finance hypothesis is
worth it, build that as its own project with proper data, out-of-sample splits, costs and paper trading.
