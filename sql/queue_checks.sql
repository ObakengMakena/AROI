-- AROI queue stage checks (v2): the cheap semantic checks from each stage's Python check(), run at
-- submit time so a bad result is rejected while the worker still holds the lease instead of failing
-- later at ingest. Mirrors aroi/stages/*.check(); tests/test_units.py keeps the category list in sync
-- with aroi/sectors.py. Quote verification stays advisory (an unverified quote lowers confidence at
-- ingest, it is not an error), so quotes not found verbatim come back as warnings.
-- Run AS postgres after queue_functions.sql. Safe to re-run.

create or replace function public.aroi_check_result(p_stage text, p_payload jsonb, p_result jsonb)
returns jsonb
language plpgsql immutable set search_path = public, pg_temp
as $$
declare
  errs text[] := '{}'; warns text[] := '{}';
  cats constant jsonb := jsonb_build_object(
      'agriculture', jsonb_build_array('crops_and_soil', 'food_processing_and_preservation', 'livestock_and_aquaculture', 'agri_waste_and_bioproducts', 'farm_technology'),
      'energy', jsonb_build_array('solar_and_wind', 'batteries_and_storage', 'mini_grids_and_grid_tech', 'bioenergy_and_clean_cooking', 'efficiency_and_heating', 'hydrogen_and_fuels'),
      'ai', jsonb_build_array('african_language_ai', 'computer_vision', 'decision_support_and_forecasting', 'low_resource_and_edge_ai', 'fraud_risk_and_finance_ai', 'health_and_public_sector_ai'),
      'construction', jsonb_build_array('low_cost_materials', 'energy_efficient_buildings', 'informal_and_affordable_housing', 'construction_methods_and_automation', 'property_valuation_and_proptech'),
      'quant_finance', jsonb_build_array('volatility_and_options', 'market_microstructure', 'portfolio_and_risk', 'return_predictability_and_factors', 'alternative_data'));
  r jsonb; o jsonb; s jsonb; i int; ref text; sec text; cands jsonb; seen text[] := '{}'; missing text[];
  bad text[]; host text; q text; hay text;
begin
  if p_stage = 'triage' then
    for r, i in select e, n - 1 from jsonb_array_elements(coalesce(p_result->'results', '[]')) with ordinality x(e, n) loop
      ref := r->>'ref';
      if not coalesce(p_payload->'refs' ? ref, false) then
        errs := errs || format('results/%s/ref: %s is not one of the task refs', i, coalesce(ref, 'null'));
        continue;
      end if;
      if ref = any(seen) then errs := errs || format('results/%s/ref: %s appears twice', i, ref); end if;
      seen := seen || ref;
      cands := coalesce(p_payload->'candidates'->ref, '[]');
      sec := coalesce(r->>'sector', cands->>0);
      if not cands ? sec then
        errs := errs || format('results/%s/sector: %s is not a candidate sector for %s (%s)', i, sec, ref, cands);
      elsif r->>'category' <> 'not_relevant' and not coalesce(cats->sec ? (r->>'category'), false) then
        errs := errs || format('results/%s/category: %s is not a %s category', i, r->>'category', sec);
      end if;
    end loop;
    select array_agg(k order by k) into missing from jsonb_object_keys(coalesce(p_payload->'refs', '{}')) k
     where not k = any(seen);
    if missing is not null then
      errs := errs || format('results: missing verdicts for %s (every paper needs one)', missing);
    end if;

  elsif p_stage = 'analyze' then
    if jsonb_array_length(coalesce(p_result->'findings', '[]')) = 0 then
      errs := errs || 'findings: at least one finding is required (record what the paper does report)'::text;
    end if;
    hay := lower(regexp_replace(coalesce(p_payload->>'abstract', '') || ' ' || coalesce(p_payload->>'full_text', ''), '\s+', ' ', 'g'));
    for r, i in select e, n - 1 from jsonb_array_elements(coalesce(p_result->'findings', '[]')) with ordinality x(e, n) loop
      q := lower(btrim(regexp_replace(coalesce(r->>'evidence_quote', ''), '\s+', ' ', 'g')));
      if q <> '' and length(hay) > 1 and strpos(hay, q) = 0 then
        warns := warns || format('findings/%s/evidence_quote: not found verbatim in the abstract/full text; it will count as unverified', i);
      end if;
    end loop;

  elsif p_stage = 'generate' then
    if jsonb_array_length(coalesce(p_result->'opportunities', '[]')) > 3 then
      errs := errs || format('opportunities: %s given, at most 3 allowed', jsonb_array_length(p_result->'opportunities'));
    end if;
    for o, i in select e, n - 1 from jsonb_array_elements(coalesce(p_result->'opportunities', '[]')) with ordinality x(e, n) loop
      if btrim(coalesce(o->>'title', '')) = '' then errs := errs || format('opportunities/%s/title: empty', i); end if;
      select array_agg(v) into bad from jsonb_array_elements_text(coalesce(o->'enabling_finding_refs', '[]')) v
       where not coalesce(p_payload->'refs' ? v, false);
      if bad is not null then
        errs := errs || format('opportunities/%s/enabling_finding_refs: unknown refs %s', i, bad);
      end if;
    end loop;

  elsif p_stage = 'validate' then
    seen := '{}';
    for s, i in select e, n - 1 from jsonb_array_elements(coalesce(p_result->'sources', '[]')) with ordinality x(e, n) loop
      if (s->>'id') = any(seen) then errs := errs || format('sources/%s/id: duplicate id %s', i, s->>'id'); end if;
      seen := seen || (s->>'id');
      host := lower(substring(s->>'url' from '^[a-zA-Z]+://(?:[^/@]*@)?(\[[^\]]*\]|[^/:?#]*)'));
      if host is null or host = '' or position('.' in host) = 0 or host = 'localhost'
         or host like '%.local' or host like '%.internal'
         or host ~ '^(10\.|127\.|0\.|169\.254\.|192\.168\.|172\.(1[6-9]|2[0-9]|3[01])\.)' then
        errs := errs || format('sources/%s/url: %s is not a public web address', i, s->>'url');
      end if;
    end loop;
    if p_result->>'recommendation' = 'investigate' and not exists (
         select 1 from jsonb_array_elements(coalesce(p_result->'sources', '[]')) e where (e->>'fetched')::boolean) then
      errs := errs || 'sources: ''investigate'' needs at least one fetched source; otherwise recommend ''park'''::text;
    end if;
  end if;
  return jsonb_build_object('errors', to_jsonb(errs), 'warnings', to_jsonb(warns));
end $$;

create or replace function public.aroi_submit_task(p_task_id uuid, p_worker text, p_result jsonb)
returns jsonb
language plpgsql security definer set search_path = public, pg_temp
as $$
declare t agent_tasks%rowtype; errs text[]; chk jsonb;
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
    raise exception 'aroi_submit_task: result rejected (% errors): %', array_length(errs, 1),
      left(array_to_string(errs[1:10], ' | '), 1500)
      using hint = 'fix these and submit again before the lease expires';
  end if;
  chk := public.aroi_check_result(t.stage, t.payload, p_result);
  if jsonb_array_length(chk->'errors') > 0 then
    -- not persisted: the raise rolls back, so the task stays claimed and unchanged
    raise exception 'aroi_submit_task: result rejected by % checks (% errors): %', t.stage,
      jsonb_array_length(chk->'errors'),
      left((select string_agg(e, ' | ') from (select jsonb_array_elements_text(chk->'errors') e limit 10) z), 1500)
      using hint = 'fix these and submit again before the lease expires';
  end if;
  update agent_tasks set status = 'submitted', result = p_result, submitted_at = now(),
         last_error = null, lease_expires_at = null
   where agent_tasks.id = p_task_id;
  return jsonb_build_object('ok', true, 'task_id', p_task_id, 'status', 'submitted', 'warnings', chk->'warnings');
end $$;

grant create on schema public to aroi_queue_owner;
alter function public.aroi_submit_task(uuid, text, jsonb) owner to aroi_queue_owner;
alter function public.aroi_check_result(text, jsonb, jsonb) owner to aroi_queue_owner;
revoke create on schema public from aroi_queue_owner;
revoke execute on function public.aroi_check_result(text, jsonb, jsonb) from public, anon, authenticated;
revoke execute on function public.aroi_submit_task(uuid, text, jsonb) from public, anon, authenticated;
grant execute on function public.aroi_check_result(text, jsonb, jsonb) to aroi_agent, service_role;
grant execute on function public.aroi_submit_task(uuid, text, jsonb) to aroi_agent, service_role;
