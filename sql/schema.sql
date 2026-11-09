-- AROI — African Research Opportunity Intelligence
-- Database schema (PostgreSQL 15+ / Supabase). Safe to re-run: every object uses IF NOT EXISTS.
-- Run in the Supabase SQL Editor, or `python -m aroi init-db`.

create extension if not exists pgcrypto;
create extension if not exists pg_trgm;

-- ---------------------------------------------------------------------------
-- Key/value state for the pipeline (e.g. last successful sweep date)
-- ---------------------------------------------------------------------------
create table if not exists pipeline_state (
  key text primary key,
  value text not null,
  updated_at timestamptz not null default now()
);

-- ---------------------------------------------------------------------------
-- Research organisations (auto-populated from OpenAlex authorships)
-- ---------------------------------------------------------------------------
create table if not exists institutions (
  id uuid primary key default gen_random_uuid(),
  openalex_id text unique,
  ror text,
  name text not null,
  country_code char(2),
  type text,
  tto_url text,                 -- technology transfer office page, fill in manually
  repository_url text,
  notes text,
  created_at timestamptz not null default now()
);

-- ---------------------------------------------------------------------------
-- Keyword searches that run alongside the country+field sweep
-- ---------------------------------------------------------------------------
create table if not exists search_queries (
  id uuid primary key default gen_random_uuid(),
  sector text not null default 'agriculture',
  theme text,
  query_text text not null,
  extra_filter text,            -- appended to the OpenAlex filter, e.g. institutions.country_code:za|bw
  active boolean not null default true,
  last_run_at timestamptz,
  results_last_run integer,
  unique (sector, query_text)
);

-- ---------------------------------------------------------------------------
-- Papers: one row per work, deduplicated on OpenAlex id, DOI and title fingerprint
-- ---------------------------------------------------------------------------
create table if not exists papers (
  id uuid primary key default gen_random_uuid(),
  openalex_id text unique,
  doi text unique,
  title text not null,
  title_fingerprint text not null,
  abstract text,
  publication_date date,
  work_type text,
  language text,
  source_name text,
  landing_url text,
  oa_status text,
  oa_url text,
  has_grobid boolean not null default false,
  primary_field text,
  primary_subfield text,
  primary_topic text,
  countries text[] not null default '{}',
  authors jsonb not null default '[]'::jsonb,
  cited_by_count integer,
  sector text,                                   -- sector whose search found it first
  found_by text[] not null default '{}',        -- 'sweep:<sector>', or the query that found it
  status text not null default 'new'
    check (status in ('new', 'triaging', 'triaged_out', 'queued', 'analyzing', 'analyzed', 'failed')),
  triage_score smallint,
  triage_sector text,
  triage_reason text,
  metadata jsonb not null default '{}'::jsonb,
  discovered_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  analyzed_at timestamptz,
  opportunities_generated_at timestamptz
);
-- upgrades for databases created by v1 (no-ops on a fresh install)
alter table papers add column if not exists opportunities_generated_at timestamptz;
alter table papers add column if not exists sector text;
-- v3: a paper can be found by several sectors; `sector` is the primary (set by triage), `sectors` all candidates
alter table papers add column if not exists sectors text[] not null default '{}';
update papers set sectors = array[sector] where sectors = '{}' and sector is not null;
alter table papers drop constraint if exists papers_status_check;
alter table papers add constraint papers_status_check
  check (status in ('new', 'triaging', 'triaged_out', 'queued', 'analyzing', 'analyzed', 'failed'));
update papers set sector = 'agriculture' where sector is null;

create unique index if not exists papers_title_fp_year_idx
  on papers (title_fingerprint, (extract(year from publication_date)));
create index if not exists papers_status_idx on papers (status, triage_score desc);
create index if not exists papers_sector_status_idx on papers (sector, status);
create index if not exists papers_publication_date_idx on papers (publication_date desc);

create table if not exists paper_institutions (
  paper_id uuid not null references papers(id) on delete cascade,
  institution_id uuid not null references institutions(id) on delete cascade,
  primary key (paper_id, institution_id)
);

-- ---------------------------------------------------------------------------
-- Scientific analysis (Agent: scientific analyst)
-- ---------------------------------------------------------------------------
create table if not exists paper_analyses (
  id uuid primary key default gen_random_uuid(),
  paper_id uuid not null references papers(id) on delete cascade,
  model text not null,
  prompt_version text not null,
  used_full_text boolean not null default false,
  problem text,
  methods text,
  novelty_claim text,
  readiness_level text,
  readiness_evidence jsonb not null default '[]'::jsonb,
  applications_to_investigate jsonb not null default '[]'::jsonb,
  missing_information jsonb not null default '[]'::jsonb,
  evidence_confidence numeric(4,3) check (evidence_confidence between 0 and 1),
  raw jsonb not null,
  created_at timestamptz not null default now()
);

create table if not exists findings (
  id uuid primary key default gen_random_uuid(),
  paper_id uuid not null references papers(id) on delete cascade,
  analysis_id uuid references paper_analyses(id) on delete cascade,
  claim text not null,
  evidence_quote text,
  evidence_location text,
  quote_verified boolean not null default false,   -- quote found verbatim in the source text
  quote_source text,                               -- 'abstract' | 'full_text' | null (not found)
  quote_check text,                                -- why a quote failed: too_short | not_found | negated
  measured_or_inferred text check (measured_or_inferred in ('measured', 'inferred')),
  limitations jsonb not null default '[]'::jsonb,
  created_at timestamptz not null default now()
);

-- ---------------------------------------------------------------------------
-- Opportunities: commercial hypotheses, merged across papers
-- ---------------------------------------------------------------------------
create table if not exists opportunities (
  id uuid primary key default gen_random_uuid(),
  title text not null,
  description text not null,
  sector text not null default 'agriculture',
  target_country char(2) not null default 'ZA',
  customer_problem text,
  target_customer text,
  proposed_solution text,
  revenue_model text,
  commercial_assumptions jsonb not null default '[]'::jsonb,
  key_risks jsonb not null default '[]'::jsonb,
  ip_pathway text,
  next_experiment text,
  -- machine pipeline progress
  pipeline_status text not null default 'generated'
    check (pipeline_status in ('generated', 'validating', 'validated', 'scored', 'failed')),
  -- human workflow stage (changed by your reviews)
  stage text not null default 'hypothesis'
    check (stage in ('hypothesis', 'researching', 'validation', 'promising', 'rejected', 'licensed', 'launched')),
  generator_confidence numeric(4,3),
  total_score numeric(5,2),
  evidence_grade text check (evidence_grade in ('high', 'medium', 'low')),
  blocking_risks jsonb not null default '[]'::jsonb,
  recommended_action text,
  next_action text,
  digest_sent_at timestamptz,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);
create index if not exists opportunities_score_idx on opportunities (total_score desc nulls last);
create index if not exists opportunities_title_trgm_idx on opportunities using gin (title gin_trgm_ops);

alter table findings add column if not exists quote_source text;
alter table findings add column if not exists quote_check text;

create table if not exists opportunity_papers (
  opportunity_id uuid not null references opportunities(id) on delete cascade,
  paper_id uuid not null references papers(id) on delete cascade,
  relationship text not null default 'origin',   -- origin | supporting
  supporting_finding_ids uuid[] not null default '{}',
  primary key (opportunity_id, paper_id)
);

-- Market validation memo (Agent: African market validator, with live web search)
create table if not exists market_validations (
  id uuid primary key default gen_random_uuid(),
  opportunity_id uuid not null references opportunities(id) on delete cascade,
  model text not null,
  prompt_version text not null,
  memo text not null,
  structured jsonb not null,
  searches_used integer not null default 0,
  created_at timestamptz not null default now()
);

-- Every URL that supports a claim. Web citations come straight from the search tool,
-- never from model-written text, so they always point at a page that was actually read.
create table if not exists evidence_items (
  id uuid primary key default gen_random_uuid(),
  opportunity_id uuid not null references opportunities(id) on delete cascade,
  source_url text not null,
  source_title text,
  evidence_type text not null,       -- web_citation | paper | patent | price | competitor | regulation
  claim_supported text not null,
  cited_text text,
  source_date text,
  retrieved_at timestamptz not null default now(),
  unique (opportunity_id, source_url, claim_supported)
);

create table if not exists opportunity_scores (
  id uuid primary key default gen_random_uuid(),
  opportunity_id uuid not null references opportunities(id) on delete cascade,
  rubric_version text not null,
  criteria_json jsonb not null,
  total_score numeric(5,2) not null,
  evidence_grade text not null,
  blocking_risks jsonb not null default '[]'::jsonb,
  model text,
  scored_at timestamptz not null default now()
);

-- ---------------------------------------------------------------------------
-- Quant finance track: research hypotheses to (maybe) backtest. Never trading signals.
-- ---------------------------------------------------------------------------
create table if not exists finance_hypotheses (
  id uuid primary key default gen_random_uuid(),
  paper_id uuid not null references papers(id) on delete cascade,
  analysis_id uuid references paper_analyses(id) on delete cascade,
  hypothesis text not null,
  signal_definition text,
  instruments_and_market text,
  data_period text,
  data_needed jsonb not null default '[]'::jsonb,
  out_of_sample_tested boolean not null default false,
  transaction_costs_modelled boolean not null default false,
  liquidity_considered boolean not null default false,
  reported_performance text,
  red_flags jsonb not null default '[]'::jsonb,
  replication_difficulty text,
  testability_score numeric(5,2),
  rubric_version text,
  recommended_action text,             -- backtest_candidate | watch | discard
  stage text not null default 'hypothesis'
    check (stage in ('hypothesis', 'researching', 'validation', 'promising', 'rejected', 'licensed', 'launched')),
  digest_sent_at timestamptz,
  created_at timestamptz not null default now()
);
create index if not exists finance_hypotheses_score_idx on finance_hypotheses (testability_score desc);

-- ---------------------------------------------------------------------------
-- Your decisions — the feedback loop the system learns from
-- ---------------------------------------------------------------------------
create table if not exists reviews (
  id uuid primary key default gen_random_uuid(),
  opportunity_id uuid references opportunities(id) on delete cascade,
  finance_hypothesis_id uuid references finance_hypotheses(id) on delete cascade,
  check (opportunity_id is not null or finance_hypothesis_id is not null),
  decision text not null
    check (decision in ('hypothesis', 'researching', 'validation', 'promising', 'rejected', 'licensed', 'launched')),
  notes text,
  reviewed_at timestamptz not null default now()
);
-- upgrades for databases created by v1
alter table reviews alter column opportunity_id drop not null;
alter table reviews add column if not exists finance_hypothesis_id uuid references finance_hypotheses(id) on delete cascade;

-- ---------------------------------------------------------------------------
-- Audit + cost log. Since v3 the AI stages are done by Grok Bot agents through agent_tasks, so the
-- only metered spend logged here is OpenAlex (stage 'openalex:<command>').
-- ---------------------------------------------------------------------------
create table if not exists agent_runs (
  id uuid primary key default gen_random_uuid(),
  stage text not null,
  model text,
  prompt_version text,
  paper_id uuid references papers(id) on delete set null,
  opportunity_id uuid references opportunities(id) on delete set null,
  status text not null default 'started',
  tokens_in integer not null default 0,
  tokens_out integer not null default 0,
  web_searches integer not null default 0,
  cost_usd numeric(12,6) not null default 0,
  error text,
  started_at timestamptz not null default now(),
  finished_at timestamptz
);
create index if not exists agent_runs_started_idx on agent_runs (started_at desc);

create table if not exists digests (
  id uuid primary key default gen_random_uuid(),
  sent_at timestamptz not null default now(),
  opportunity_ids uuid[] not null default '{}',
  finance_hypothesis_ids uuid[] not null default '{}',
  html text not null,
  emailed boolean not null default false
);
alter table digests add column if not exists finance_hypothesis_ids uuid[] not null default '{}';  -- v1 upgrade

-- ---------------------------------------------------------------------------
-- v3: work queue for the AI stages, done by Grok Bot agents instead of a paid model API.
-- Code enqueues tasks; a bot pulls (claims) them, submits JSON; code validates and ingests.
-- ---------------------------------------------------------------------------
create table if not exists agent_tasks (
  id uuid primary key default gen_random_uuid(),
  stage text not null check (stage in ('triage', 'analyze', 'generate', 'validate', 'score')),
  sector text,
  subject_key text not null,          -- e.g. 'paper:<uuid>' or 'opportunity:<uuid>' or 'triage:<uuid>,...'
  subject_ids uuid[] not null default '{}',
  payload jsonb not null,             -- {instructions, input, output_schema, prompt_version}
  status text not null default 'pending'
    check (status in ('pending', 'claimed', 'submitted', 'done', 'failed', 'cancelled')),
  priority numeric not null default 0,
  attempts integer not null default 0,
  max_attempts integer not null default 3,
  claimed_by text,
  claimed_at timestamptz,
  lease_expires_at timestamptz,
  submitted_at timestamptz,
  result jsonb,
  last_error text,
  error_log jsonb not null default '[]'::jsonb,
  created_at timestamptz not null default now(),
  completed_at timestamptz
);
create unique index if not exists agent_tasks_active_subject_idx
  on agent_tasks (stage, subject_key) where status in ('pending', 'claimed', 'submitted');
create index if not exists agent_tasks_queue_idx on agent_tasks (stage, status, priority desc, created_at);

-- One row per scheduled command: the digest's heartbeat and failure report
create table if not exists pipeline_runs (
  id uuid primary key default gen_random_uuid(),
  command text not null,
  ok boolean,
  results jsonb not null default '{}'::jsonb,
  started_at timestamptz not null default now(),
  finished_at timestamptz
);
create index if not exists pipeline_runs_started_idx on pipeline_runs (started_at desc);

-- Convenience view for the Supabase table editor
drop view if exists opportunity_board;
create view opportunity_board with (security_invoker = true) as
select
  o.id, o.sector, o.title, o.stage, o.pipeline_status, o.total_score, o.evidence_grade,
  o.target_customer, o.next_action, o.ip_pathway,
  (select string_agg(distinct i.name, '; ')
     from opportunity_papers op
     join paper_institutions pi on pi.paper_id = op.paper_id
     join institutions i on i.id = pi.institution_id
    where op.opportunity_id = o.id and i.country_code = o.target_country) as institutions,
  (select count(*) from evidence_items e where e.opportunity_id = o.id) as evidence_count,
  o.created_at
from opportunities o
order by o.total_score desc nulls last;

-- ---------------------------------------------------------------------------
-- Supabase exposes the public schema through its REST API. Enabling row level security with
-- no policies blocks the anon/authenticated API keys from reading or writing these tables;
-- the pipeline connects as the database owner, which bypasses RLS.
-- ---------------------------------------------------------------------------
do $$
declare t text;
begin
  foreach t in array array['pipeline_state','institutions','search_queries','papers','paper_institutions',
    'paper_analyses','findings','opportunities','opportunity_papers','market_validations','evidence_items',
    'opportunity_scores','reviews','agent_runs','digests','finance_hypotheses','agent_tasks','pipeline_runs']
  loop
    execute format('alter table %I enable row level security', t);
  end loop;
end $$;
