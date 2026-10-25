"""End-to-end run of every stage against a real Postgres, with OpenAlex faked and a fake Grok Bot agent
working the queue. Set TEST_DATABASE_URL to a throwaway database; the test drops and recreates the
public schema."""
import json
import os

import pytest

from aroi import agent_queue, db, digest
from aroi.agent_queue import ResultRejected, pull, submit
from aroi.budget import BudgetExceeded
from aroi.cli import cmd_review, run_pipeline, run_stage
from aroi.config import Config
from aroi.openalex import OpenAlexClient

from .fakes import FakeBot, openalex_transport

DB_URL = os.getenv("TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(not DB_URL, reason="TEST_DATABASE_URL not set")


@pytest.fixture()
def env(tmp_path):
    conn = db.connect(DB_URL)
    conn.execute("drop schema if exists public cascade; create schema public;")
    conn.commit()
    db.init_db(conn)
    cfg = Config()
    cfg.database_url = DB_URL
    cfg.output_dir = str(tmp_path)
    cfg.smtp_user = cfg.smtp_password = cfg.digest_to = ""
    cfg.analyze_per_sector = 2
    cfg.validate_per_sector = 1
    calls: list[str] = []
    oa = OpenAlexClient("test-key", "me@example.com", transport=openalex_transport(calls))
    yield conn, cfg, oa, FakeBot(), calls
    conn.close()


def work(conn, cfg, bot, stage, oa=None):
    """enqueue -> bot pulls and submits -> pipeline ingests. Returns (enqueue stats, ingest stats)."""
    e = run_stage(stage, conn, cfg, oa)
    tasks = pull(conn, stage, 100, "fake-bot", 30)
    for t in tasks:
        json.dumps(t)  # what the CLI prints must be JSON-serialisable
        submit(conn, t["task_id"], bot.answer(t), "fake-bot")
    i = agent_queue.ingest(conn, cfg, [stage])[stage]
    return e, i


def test_full_pipeline(env):
    conn, cfg, oa, bot, calls = env
    assert cfg.sectors == ["agriculture", "energy", "ai", "construction", "quant_finance"]

    # --- discover: every sector sweeps; one bad query is skipped, not fatal ---------------
    s = run_stage("discover", conn, cfg, oa)
    sweeps = [c for c in calls if "search=" not in c and "content.openalex" not in c]
    assert len(sweeps) == 5
    for frag in ("primary_topic.field.id%3A11", "primary_topic.field.id%3A21", "primary_topic.subfield.id%3A1702%7C1707",
                 "primary_topic.subfield.id%3A2205%7C2215%7C3322", "primary_topic.subfield.id%3A2003"):
        assert any(frag in c and "institutions.country_code%3Aza" in c for c in sweeps), frag
    assert all("from_created_date" in c and "sort=created_date%3Aasc" in c for c in sweeps)
    assert "api_key=test-key" in sweeps[0]
    assert s["queries"] == 36 and s["query_errors"] == 1  # 37 seeded queries, the TinyML one fails
    papers = conn.execute("select openalex_id, doi, status, found_by, sector, sectors from papers order by openalex_id").fetchall()
    assert {p["openalex_id"]: p["sector"] for p in papers} == {
        "E1": "energy", "Q1": "quant_finance", "W1": "agriculture", "W2": "agriculture", "W3": "agriculture",
        "W4": "agriculture"}, "W99 shares W1's DOI and must merge into it"
    w1 = next(p for p in papers if p["openalex_id"] == "W1")
    assert w1["doi"] == "10.1/hermetic" and "sweep:agriculture" in w1["found_by"] and len(w1["found_by"]) == 2
    assert next(p for p in papers if p["openalex_id"] == "W3")["status"] == "triaged_out"

    run_stage("discover", conn, cfg, oa)
    assert conn.execute("select count(*) n from papers").fetchone()["n"] == 6, "no duplicates on re-run"

    # --- triage: queued for the agent, then ingested ----------------------------------------
    e, t = work(conn, cfg, bot, "triage")
    assert e["papers"] == 5
    assert t["queued"] == 4 and t["dropped"] == 1
    assert t["queued_by_sector"] == {"agriculture": 2, "energy": 1, "quant_finance": 1}
    energy_task = next(x for x in bot.tasks if "renewable energy" in x["instructions"])
    energy_enum = energy_task["output_schema"]["properties"]["results"]["items"]["properties"]["category"]["enum"]
    assert "batteries_and_storage" in energy_enum and "crops_and_soil" not in energy_enum
    assert run_stage("triage", conn, cfg)["tasks"] == 0, "nothing left to triage"

    # --- analyze: per-sector quota; finance gets the hypothesis analyst --------------------
    e, a = work(conn, cfg, bot, "analyze", oa)
    assert e["full_text"] == 1  # only W1 has GROBID text, fetched by the pipeline not the bot
    assert a["analyzed"] == 4 and a["by_sector"] == {"agriculture": 2, "energy": 1, "quant_finance": 1}
    fs = conn.execute("select f.evidence_quote, f.quote_verified, f.quote_source, f.quote_check, p.sector "
                      "from findings f join papers p on p.id=f.paper_id").fetchall()
    fake = [f for f in fs if "aflatoxin" in f["evidence_quote"]]
    assert fake and not any(f["quote_verified"] for f in fake), "invented quote rejected"
    assert all(f["quote_check"] == "not_found" for f in fake)
    assert sum(f["quote_verified"] for f in fs if f["sector"] == "agriculture") == 2
    assert {f["quote_source"] for f in fs if f["quote_verified"]} == {"abstract"}
    assert any(f["quote_verified"] for f in fs if f["sector"] == "quant_finance")
    assert any("content.openalex.org/works/W1.grobid-xml" in c for c in calls)
    assert all("untrusted" in x["instructions"] for x in bot.tasks if "problem" in x["output_schema"]["properties"])
    assert all("grok" not in x["input"].lower() for x in bot.tasks)
    confs = [float(r["evidence_confidence"]) for r in conn.execute("select evidence_confidence from paper_analyses").fetchall()]
    assert max(confs) <= 0.65, "1 of 2 quotes verified caps at 0.65; abstract-only at 0.6"
    h = conn.execute("select * from finance_hypotheses").fetchone()
    # 25 OOS + 20 costs + 0 liquidity + 8 moderate + 10 verified quote + 12 (0.6 conf) - 5 (1 red flag) = 70
    assert float(h["testability_score"]) == 70.0 and h["recommended_action"] == "backtest_candidate"
    assert h["instruments_and_market"] == "JSE Top 40 index options"
    assert conn.execute("select model from paper_analyses limit 1").fetchone()["model"] == "fake-bot"

    # --- generate: product sectors only; merging stays within a sector -------------------
    e, g = work(conn, cfg, bot, "generate")
    assert e["tasks"] == 3
    assert g["created"] == 2 and g["merged"] == 1, "two agri papers -> one idea; energy -> its own"
    opps = {o["sector"]: o for o in conn.execute("select * from opportunities").fetchall()}
    assert set(opps) == {"agriculture", "energy"}, "finance papers never become product opportunities"
    agri = opps["agriculture"]
    assert float(agri["generator_confidence"]) == 0.9
    assert "IPR-PFRD" in agri["ip_pathway"] and "University of Pretoria" in agri["ip_pathway"]
    assert "University of Zimbabwe" in agri["ip_pathway"], "merged paper's IP route is kept too"
    assert "University of Johannesburg" in opps["energy"]["ip_pathway"]
    energy_gen = next(x for x in bot.tasks if "batteries" in x["input"] and "opportunities" in x["output_schema"]["properties"])
    assert "NERSA" in energy_gen["instructions"] and "Plant Breeders" not in energy_gen["instructions"]

    # --- validate: one per sector per run; only fetched sources become evidence ------------
    e, v = work(conn, cfg, bot, "validate")
    assert v["validated"] == 2 and v["by_sector"] == {"agriculture": 1, "energy": 1}
    assert v["searches"] == 10
    ev = conn.execute("select source_url, claim_supported, evidence_type from evidence_items").fetchall()
    assert {e["source_url"] for e in ev} == {"https://picsbags.example/za", "https://dalrrd.example/regs"}
    assert not any("ghostco" in e["source_url"] for e in ev), "unfetched source never stored"
    assert not any("Ghost Co" in e["claim_supported"] for e in ev)
    mv = conn.execute("select memo, structured from market_validations limit 1").fetchone()
    assert "[S1]" in mv["memo"] and "SOURCES" in mv["memo"]
    unknowns = " ".join(mv["structured"]["unknowns_needing_field_research"])
    assert "Ghost Co" in unknowns and "PICS bag: about R70" in unknowns
    assert mv["structured"]["unfetched_sources_ignored"] == 1

    # --- score: web sources = claim-linked pages only ---------------------------------------
    e, sc = work(conn, cfg, bot, "score")
    assert sc["scored"] == 2
    for o in conn.execute("select * from opportunities").fetchall():
        assert float(o["total_score"]) == 80.0  # all 4s
        assert o["evidence_grade"] == "medium"  # agent said high, but only 2 linked web sources -> capped
        assert o["recommended_action"] == "prioritize_validation"
    meta = conn.execute("select criteria_json from opportunity_scores limit 1").fetchone()["criteria_json"]["_meta"]
    assert meta["web_sources"] == 2

    # --- heartbeat + digest + review loop ---------------------------------------------
    ok, res = run_pipeline(conn, cfg, "tick", ["ingest", *agent_queue.AI_STAGES], oa)
    assert ok and conn.execute("select count(*) n from pipeline_runs where ok").fetchone()["n"] == 1
    d = digest.run(conn, cfg, send=True)
    assert d["count"] == 3 and d["emailed"] is False
    assert d["by_sector"] == {"agriculture": 1, "energy": 1, "ai": 0, "construction": 0, "quant_finance": 1}
    assert d["funnel"]["agriculture"]["found"] == 4 and d["funnel"]["agriculture"]["passed_triage"] == 2
    assert d["funnel"]["agriculture"]["validated"] == 1 and d["funnel"]["energy"]["scored"] == 1
    assert d["health"]["warnings"] == []
    html = open(d["path"], encoding="utf-8").read()
    for text in ("Hermetic maize storage bags", "Second-life battery backup packs", "picsbags.example",
                 "Quant finance — hypotheses to test", "JSE Top 40 index options", "not trading signals",
                 "Artificial intelligence and data products", "Passed triage", "Last pipeline run: tick"):
        assert text in html, text
    agri_id = str(agri["id"])[:8]
    assert cmd_review(cfg, agri_id, "promising", "call co-ops") == 0
    assert cmd_review(cfg, str(h["id"])[:8], "rejected", "needs paid option data") == 0
    assert conn.execute("select stage from finance_hypotheses").fetchone()["stage"] == "rejected"
    d2 = digest.run(conn, cfg, send=False)
    assert d2["count"] == 0  # already sent
    html2 = open(d2["path"], encoding="utf-8").read()
    assert "No new idea cleared" in html2 and "promising" in html2 and "needs paid option data" in html2

    # --- OpenAlex spend is logged and counted ---------------------------------------------
    ok, res = run_pipeline(conn, cfg, "discover", ["discover"], oa)
    assert ok and res["openalex_cost_this_run_usd"] > 0
    row = conn.execute("select stage, cost_usd from agent_runs order by started_at desc limit 1").fetchone()
    assert row["stage"] == "openalex:discover" and float(row["cost_usd"]) > 0


def test_invalid_results_are_rejected_with_clear_errors(env):
    conn, cfg, oa, bot, _ = env
    run_stage("discover", conn, cfg, oa)
    run_stage("triage", conn, cfg)
    t = pull(conn, "triage", 1, "bot-a", 30)[0]
    with pytest.raises(ResultRejected) as e:
        submit(conn, t["task_id"], {"results": [{"ref": "P1", "commercial_signal": 14, "category": "x",
                                                  "reason": "hype", "sector": "agriculture", "extra": 1}]})
    msg = " ".join(e.value.errors)
    assert "14 is greater than the maximum of 10" in msg and "Additional properties" in msg
    good = bot.answer(t)
    with pytest.raises(ResultRejected) as e:
        submit(conn, t["task_id"], {"results": good["results"][:1]})
    assert "missing verdicts" in " ".join(e.value.errors)
    with pytest.raises(ResultRejected):
        submit(conn, t["task_id"], good, worker="bot-b")  # someone else's task
    assert conn.execute("select status from agent_tasks where id=%s", (t["task_id"],)).fetchone()["status"] == "claimed"
    submit(conn, t["task_id"], good, worker="bot-a")
    with pytest.raises(ResultRejected):
        submit(conn, t["task_id"], good)  # already submitted

    # validation: private hosts are refused
    task = {"stage": "validate", "payload": {"output_schema": __import__("aroi.schemas").schemas.VALIDATION_SUBMISSION}}
    res = {"competitors": [], "price_evidence": [], "regulatory_findings": [], "ip_findings": [],
           "customer_hypotheses": [], "unit_economics_assumptions": [], "unknowns_needing_field_research": [],
           "pilot_plan": [], "recommendation": "park", "recommendation_reason": "x", "memo": "m", "searches_used": 1,
           "sources": [{"id": "S1", "url": "http://127.0.0.1/admin", "title": "t", "fetched": True, "quoted_text": ""}]}
    assert any("private" in x or "public" in x for x in agent_queue.validate_result(task, res))


def test_failures_retry_then_give_up(env):
    conn, cfg, oa, bot, _ = env
    cfg.task_max_attempts = 2
    run_stage("discover", conn, cfg, oa)
    run_stage("triage", conn, cfg)
    n_tasks = conn.execute("select count(*) n from agent_tasks").fetchone()["n"]
    # attempt 1: lease expires
    pull(conn, "triage", 100, "slow-bot", 30)
    conn.execute("update agent_tasks set lease_expires_at = now() - interval '1 minute'")
    conn.commit()
    assert agent_queue.expire_leases(conn) == n_tasks
    conn.commit()
    assert conn.execute("select count(*) n from agent_tasks where status='pending' and attempts=1").fetchone()["n"] == n_tasks
    # attempt 2: expires again -> failed; papers released as failed, not stuck in 'triaging'
    pull(conn, "triage", 100, "slow-bot", 30)
    conn.execute("update agent_tasks set lease_expires_at = now() - interval '1 minute'")
    conn.commit()
    agent_queue.ingest(conn, cfg)
    assert conn.execute("select count(*) n from agent_tasks where status='failed'").fetchone()["n"] == n_tasks
    assert conn.execute("select count(*) n from papers where status='triaging'").fetchone()["n"] == 0
    assert conn.execute("select count(*) n from papers where status='failed'").fetchone()["n"] == 5
    # release does not burn an attempt
    conn.execute("update papers set status='new' where status='failed'")
    conn.commit()
    run_stage("triage", conn, cfg)
    t = pull(conn, "triage", 1, "b", 30)[0]
    agent_queue.release(conn, t["task_id"], "out of time")
    assert conn.execute("select attempts, status from agent_tasks where id=%s", (t["task_id"],)).fetchone() == {
        "attempts": 0, "status": "pending"}


def test_ingest_error_goes_back_to_pending(env):
    conn, cfg, oa, bot, _ = env
    run_stage("discover", conn, cfg, oa)
    run_stage("triage", conn, cfg)
    t = pull(conn, "triage", 1, "b", 30)[0]
    submit(conn, t["task_id"], bot.answer(t))
    conn.execute("update papers set status='new' where status='triaging'")  # simulate a conflicting change
    conn.execute("update agent_tasks set result = jsonb_set(result, '{results,0,ref}', '\"P99\"') where id=%s", (t["task_id"],))
    conn.commit()
    # P99 is skipped, not a crash; force a genuine failure instead
    conn.execute("update agent_tasks set result = '{}'::jsonb where id=%s", (t["task_id"],))
    conn.commit()
    st = agent_queue.ingest(conn, cfg, ["triage"])["triage"]
    assert st["retry"] == 1
    row = conn.execute("select status, last_error from agent_tasks where id=%s", (t["task_id"],)).fetchone()
    assert row["status"] == "pending" and row["last_error"].startswith("ingest:")


def test_multi_sector_paper(env):
    conn, cfg, oa, bot, _ = env
    run_stage("discover", conn, cfg, oa)
    conn.execute("update papers set sectors = array['energy','agriculture'] where openalex_id='E1'")
    conn.commit()
    work(conn, cfg, bot, "triage")
    e1 = conn.execute("select sector, status from papers where openalex_id='E1'").fetchone()
    assert e1["status"] == "queued" and e1["sector"] in ("energy", "agriculture")
    assert conn.execute("select count(*) n from agent_tasks where stage='triage' and sector='agriculture'").fetchone()["n"] >= 1


def test_sectors_setting_limits_scope(env):
    conn, cfg, oa, bot, calls = env
    cfg.sectors = ["energy"]
    run_stage("discover", conn, cfg, oa)
    assert {p["sector"] for p in conn.execute("select sector from papers").fetchall()} == {"energy"}
    assert all("field.id%3A11" not in c for c in calls)


def test_openalex_budget_guard(env):
    conn, cfg, oa, bot, calls = env
    cfg.monthly_budget_usd = 0.0
    ok, res = run_pipeline(conn, cfg, "daily", ["discover"], oa)
    assert "stopped" in res["discover"] and calls == []
    with pytest.raises(BudgetExceeded):
        from aroi import budget
        budget.check(conn, cfg)


def test_truncated_sweep_does_not_skip_ahead(env):
    from collections import defaultdict
    from datetime import date
    from aroi import sectors
    from aroi.db import get_state, set_state
    from aroi.stages import discover
    from .fakes import make_work

    conn, cfg, oa, bot, _ = env

    class StubOA:
        last_count = 500
        last_truncated = True

        def iter_works(self, flt, max_results, sort):
            assert sort == "created_date:asc" and "from_created_date:" in flt
            w = make_work("W500", "Hermetic bags", date="2026-01-01")
            w["created_date"] = "2026-03-05"
            yield w

    key = f"sweep_last_date:{cfg.country}:agriculture"
    set_state(conn, key, "2026-03-01")
    conn.commit()
    stats = defaultdict(int)
    discover._sweep(conn, cfg, StubOA(), sectors.get("agriculture"), stats)
    assert get_state(conn, key) == "2026-03-05", "resume from last seen created_date, not today"
    assert stats["sweep_truncated"] == ["agriculture"]
    StubOA.last_truncated = False
    discover._sweep(conn, cfg, StubOA(), sectors.get("agriculture"), defaultdict(int))
    assert get_state(conn, key) == date.today().isoformat()


def test_digest_heartbeat_warns_when_stale(env):
    conn, cfg, oa, bot, _ = env
    conn.execute("insert into pipeline_runs (command, ok, started_at, finished_at) values "
                 "('daily', false, now() - interval '3 days', now() - interval '3 days')")
    conn.commit()
    h = digest.build(conn, cfg)["health"]
    text = " ".join(h["warnings"])
    assert "No pipeline run for 72 hours" in text and "had errors" in text
