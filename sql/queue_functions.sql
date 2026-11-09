-- AROI queue functions: safe claim / submit / release for Grok Bot agents.
-- Run AS postgres after schema.sql and roles.sql. Agents call these instead of writing agent_tasks directly.
-- Functions are SECURITY DEFINER, owned by aroi_queue_owner (NOLOGIN; select/update on agent_tasks only).
-- Result validation uses pg_jsonschema against the task's own payload->'output_schema' (the same
-- schema the Python CLI uses). The stage-specific semantic checks (stage.check(), e.g. quote matching)
-- still run in the pipeline's ingest step, which re-validates every submitted result.

create extension if not exists pg_jsonschema with schema extensions;

do $$ begin
  if not exists (select 1 from pg_roles where rolname = 'aroi_queue_owner') then
    create role aroi_queue_owner nologin noinherit;
  end if;
end $$;
grant aroi_queue_owner to postgres;  -- needed so postgres can assign ownership
grant usage on schema public to aroi_queue_owner;
grant usage on schema extensions to aroi_queue_owner;
revoke all on all tables in schema public from aroi_queue_owner;
grant select, update on agent_tasks to aroi_queue_owner;
drop policy if exists aroi_queue_owner_all on agent_tasks;
create policy aroi_queue_owner_all on agent_tasks for all to aroi_queue_owner using (true) with check (true);

-- 1) claim ---------------------------------------------------------------------------------------
create or replace function public.aroi_claim_tasks(
  p_worker text, p_stage text default null, p_limit int default 1, p_lease_minutes int default 60)
returns table (id uuid, stage text, sector text, attempt int, max_attempts int,
               lease_expires_at timestamptz, previous_errors jsonb, payload jsonb)
language plpgsql security definer set search_path = public, pg_temp
as $$
begin
  if coalesce(btrim(p_worker), '') = '' then raise exception 'aroi_claim_tasks: p_worker is required'; end if;
  if p_stage is not null and p_stage not in ('triage','analyze','generate','validate','score') then
    raise exception 'aroi_claim_tasks: unknown stage %', p_stage;
  end if;
  if p_limit is null or p_limit < 1 or p_limit > 50 then raise exception 'aroi_claim_tasks: p_limit must be 1..50'; end if;
  if p_lease_minutes is null or p_lease_minutes < 1 or p_lease_minutes > 240 then
    raise exception 'aroi_claim_tasks: p_lease_minutes must be 1..240';
  end if;

  -- expire stale leases (same rule as agent_queue.expire_leases)
  update agent_tasks t set
    status = case when t.attempts >= t.max_attempts then 'failed' else 'pending' end,
    last_error = 'lease expired before a valid result was submitted',
    error_log = t.error_log || jsonb_build_array(jsonb_build_object(
        'attempt', t.attempts, 'error', 'lease expired', 'worker', t.claimed_by)),
    claimed_by = null, claimed_at = null, lease_expires_at = null
  where t.status = 'claimed' and t.lease_expires_at < now()
    and (p_stage is null or t.stage = p_stage);

  return query
  with c as (
    select a.id from agent_tasks a
     where a.status = 'pending' and a.attempts < a.max_attempts
       and (p_stage is null or a.stage = p_stage)
     order by a.priority desc, a.created_at
     limit p_limit
     for update skip locked)
  update agent_tasks t set status = 'claimed', claimed_by = p_worker, claimed_at = now(),
         lease_expires_at = now() + make_interval(mins => p_lease_minutes), attempts = t.attempts + 1
    from c where t.id = c.id
  returning t.id, t.stage, t.sector, t.attempts, t.max_attempts, t.lease_expires_at,
            (select coalesce(jsonb_agg(e->'error'), '[]'::jsonb)
               from (select e from jsonb_array_elements(t.error_log) with ordinality x(e, n)
                     order by n desc limit 3) z),
            t.payload;
end $$;

-- 2) submit --------------------------------------------------------------------------------------
create or replace function public.aroi_submit_task(p_task_id uuid, p_worker text, p_result jsonb)
returns jsonb
language plpgsql security definer set search_path = public, pg_temp
as $$
declare t agent_tasks%rowtype; errs text[];
begin
  select * into t from agent_tasks where agent_tasks.id = p_task_id for update;
  if not found then raise exception 'aroi_submit_task: no task with id %', p_task_id; end if;
  if t.status <> 'claimed' then
    raise exception 'aroi_submit_task: task is ''%'', not ''claimed'' - claim it first', t.status;
  end if;
  if t.claimed_by is distinct from p_worker then
    raise exception 'aroi_submit_task: task is claimed by %, not %', t.claimed_by, p_worker;
  end if;
  if t.lease_expires_at is null or t.lease_expires_at < now() then
    raise exception 'aroi_submit_task: lease expired at % - claim again', t.lease_expires_at;
  end if;
  if p_result is null then raise exception 'aroi_submit_task: p_result is null'; end if;
  if t.payload->'output_schema' is null then
    raise exception 'aroi_submit_task: task has no output_schema';
  end if;

  errs := extensions.jsonschema_validation_errors((t.payload->'output_schema')::json, p_result::json);
  if coalesce(array_length(errs, 1), 0) > 0 then
    -- not persisted: the raise rolls back, so the task stays claimed and unchanged
    raise exception 'aroi_submit_task: result rejected (% errors): %', array_length(errs, 1),
      left(array_to_string(errs[1:10], ' | '), 1500)
      using hint = 'fix these and submit again before the lease expires';
  end if;

  update agent_tasks set status = 'submitted', result = p_result, submitted_at = now(),
         last_error = null, lease_expires_at = null
   where agent_tasks.id = p_task_id;
  return jsonb_build_object('ok', true, 'task_id', p_task_id, 'status', 'submitted');
end $$;

-- 3) release -------------------------------------------------------------------------------------
create or replace function public.aroi_release_task(p_task_id uuid, p_worker text, p_reason text default null)
returns jsonb
language plpgsql security definer set search_path = public, pg_temp
as $$
declare n int;
begin
  update agent_tasks set status = 'pending', claimed_by = null, claimed_at = null, lease_expires_at = null,
         attempts = greatest(attempts - 1, 0), last_error = left('released: ' || coalesce(p_reason, 'no reason given'), 1000)
   where agent_tasks.id = p_task_id and status = 'claimed' and claimed_by = p_worker;
  get diagnostics n = row_count;
  if n = 0 then raise exception 'aroi_release_task: task % is not claimed by %', p_task_id, p_worker; end if;
  return jsonb_build_object('ok', true, 'task_id', p_task_id, 'status', 'pending');
end $$;

-- ALTER OWNER needs CREATE on the schema; grant it only for the ownership change
grant create on schema public to aroi_queue_owner;
alter function public.aroi_claim_tasks(text, text, int, int) owner to aroi_queue_owner;
alter function public.aroi_submit_task(uuid, text, jsonb) owner to aroi_queue_owner;
alter function public.aroi_release_task(uuid, text, text) owner to aroi_queue_owner;
revoke create on schema public from aroi_queue_owner;

revoke execute on function public.aroi_claim_tasks(text, text, int, int) from public, anon, authenticated;
revoke execute on function public.aroi_submit_task(uuid, text, jsonb) from public, anon, authenticated;
revoke execute on function public.aroi_release_task(uuid, text, text) from public, anon, authenticated;
grant execute on function public.aroi_claim_tasks(text, text, int, int) to aroi_agent, service_role;
grant execute on function public.aroi_submit_task(uuid, text, jsonb) to aroi_agent, service_role;
grant execute on function public.aroi_release_task(uuid, text, text) to aroi_agent, service_role;
