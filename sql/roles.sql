-- AROI least-privilege database roles. Run once in the Supabase SQL editor AS the postgres user,
-- after schema.sql. Replace the two passwords first (use long random strings), then put the
-- resulting connection strings in GitHub secrets / the bots' environment:
--   DATABASE_URL        -> aroi_pipeline   (GitHub Actions: discover, enqueue, ingest, digest, review)
--   AGENT_DATABASE_URL  -> aroi_agent      (Grok Bot agents: pull/submit tasks only)
-- Neither role is a superuser or owns the tables, so neither bypasses RLS; each gets explicit
-- policies instead. Keep the postgres owner credentials out of CI entirely (use them only to
-- run schema.sql / this file by hand).

do $$ begin
  if not exists (select 1 from pg_roles where rolname = 'aroi_pipeline') then
    create role aroi_pipeline login password 'CHANGE_ME_pipeline' noinherit;
  end if;
  if not exists (select 1 from pg_roles where rolname = 'aroi_agent') then
    create role aroi_agent login password 'CHANGE_ME_agent' noinherit;
  end if;
end $$;

grant usage on schema public to aroi_pipeline, aroi_agent;

-- Pipeline: read/write data tables, no DDL, no delete except via cascades it doesn't need.
grant select, insert, update on all tables in schema public to aroi_pipeline;
grant select on opportunity_board to aroi_pipeline;
-- pg_trgm similarity() and gen_random_uuid() are needed by the pipeline
grant execute on all functions in schema public to aroi_pipeline;

-- Agents: may only see and update the work queue. They never touch papers, scores or digests
-- directly; the pipeline validates and ingests what they submit.
revoke all on all tables in schema public from aroi_agent;
grant select on agent_tasks to aroi_agent;
grant update (status, claimed_by, claimed_at, lease_expires_at, attempts, submitted_at, result,
              last_error, error_log) on agent_tasks to aroi_agent;

-- RLS policies (RLS is enabled on every table by schema.sql)
do $$
declare t text;
begin
  foreach t in array array['pipeline_state','institutions','search_queries','papers','paper_institutions',
    'paper_analyses','findings','opportunities','opportunity_papers','market_validations','evidence_items',
    'opportunity_scores','reviews','agent_runs','digests','finance_hypotheses','agent_tasks','pipeline_runs']
  loop
    execute format('drop policy if exists aroi_pipeline_all on %I', t);
    execute format('create policy aroi_pipeline_all on %I for all to aroi_pipeline using (true) with check (true)', t);
  end loop;
end $$;

drop policy if exists aroi_agent_read on agent_tasks;
create policy aroi_agent_read on agent_tasks for select to aroi_agent using (true);
drop policy if exists aroi_agent_work on agent_tasks;
-- agents may only move tasks that are open or that are claimed by themselves (claimed_by is free text,
-- so this is a guard against accidents, not against a malicious agent)
create policy aroi_agent_work on agent_tasks for update to aroi_agent
  using (status in ('pending', 'claimed'))
  with check (status in ('pending', 'claimed', 'submitted'));
