"""End-to-end run of every stage against a real Postgres, with OpenAlex and Claude faked.
Set TEST_DATABASE_URL to a throwaway database; the test drops and recreates the public schema."""
import os

import pytest

from aroi import db, digest
from aroi.cli import cmd_review, run_stage
from aroi.config import Config
from aroi.llm import LLM, BudgetExceeded
from aroi.openalex import OpenAlexClient

from .fakes import FakeAnthropic, openalex_transport

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
    calls: list[str] = []
    oa = OpenAlexClient("test-key", "me@example.com", transport=openalex_transport(calls))
    fake = FakeAnthropic()
    llm = LLM(cfg, db.connect(DB_URL), client=fake)
    yield conn, cfg, oa, llm, fake, calls
    conn.close()


def test_full_pipeline(env):
    conn, cfg, oa, llm, fake, calls = env

    # --- discover ---------------------------------------------------------------
    s = run_stage("discover", conn, cfg, llm, oa)
    sweep_url = next(c for c in calls if "search=" not in c)
    assert "institutions.country_code%3Aza" in sweep_url and "primary_topic.field.id%3A11" in sweep_url
    assert "api_key=test-key" in sweep_url
    papers = conn.execute("select openalex_id, doi, status, found_by from papers order by openalex_id").fetchall()
    ids = {p["openalex_id"] for p in papers}
    assert ids == {"W1", "W2", "W3", "W4"}, "W99 shares W1's DOI and must merge into it"
    w1 = next(p for p in papers if p["openalex_id"] == "W1")
    assert w1["doi"] == "10.1/hermetic" and "sweep" in w1["found_by"] and len(w1["found_by"]) > 1
    assert next(p for p in papers if p["openalex_id"] == "W3")["status"] == "triaged_out"
    assert s["queries"] == 12

    # running discovery again must not create duplicates
    run_stage("discover", conn, cfg, llm, oa)
    assert conn.execute("select count(*) n from papers").fetchone()["n"] == 4

    # --- triage -----------------------------------------------------------------
    t = run_stage("triage", conn, cfg, llm, oa)
    assert t["queued"] == 2 and t["dropped"] == 1
    assert conn.execute("select status from papers where openalex_id='W2'").fetchone()["status"] == "triaged_out"

    # --- analyze ------------------------------------------------------------------
    a = run_stage("analyze", conn, cfg, llm, oa)
    assert a["analyzed"] == 2 and a["full_text"] == 1  # only W1 has GROBID text
    fs = conn.execute("select evidence_quote, quote_verified from findings").fetchall()
    assert sum(f["quote_verified"] for f in fs) == 2, "real abstract quotes verify"
    assert not any(f["quote_verified"] for f in fs if "aflatoxin" in f["evidence_quote"]), "invented quote rejected"
    assert any("content.openalex.org/works/W1.grobid-xml" in c for c in calls)
    analysis_prompt = next(c for c in fake.calls if "problem" in c["output_config"]["format"]["schema"]["properties"])
    assert "untrusted" in analysis_prompt["system"]

    # --- generate -------------------------------------------------------------------
    g = run_stage("generate", conn, cfg, llm, oa)
    assert g["created"] == 1 and g["merged"] == 1, "same idea from two papers -> one opportunity"
    opp = conn.execute("select * from opportunities").fetchone()
    assert float(opp["generator_confidence"]) == 1.0  # clamped from 1.7
    assert "IPR-PFRD" in opp["ip_pathway"] and "University of Pretoria" in opp["ip_pathway"]
    links = conn.execute("select relationship, supporting_finding_ids from opportunity_papers").fetchall()
    assert sorted(l["relationship"] for l in links) == ["origin", "supporting"]
    assert all(len(l["supporting_finding_ids"]) == 1 for l in links), "unknown ref F9 dropped"

    # --- validate ---------------------------------------------------------------------
    v = run_stage("validate", conn, cfg, llm, oa)
    assert v["validated"] == 1 and v["searches"] == 5  # 2 in the paused turn + 3 after
    ev = conn.execute("select source_url, evidence_type, claim_supported from evidence_items").fetchall()
    urls = {e["source_url"] for e in ev}
    assert urls == {"https://picsbags.example/za", "https://dalrrd.example/regs"}, "only API-cited URLs are stored"
    assert not any("Ghost Co" in e["claim_supported"] for e in ev)
    mv = conn.execute("select memo, structured from market_validations").fetchone()
    assert "[S1]" in mv["memo"] and "SOURCES" in mv["memo"]
    unknowns = " ".join(mv["structured"]["unknowns_needing_field_research"])
    assert "Ghost Co" in unknowns and "PICS bag: about R70" in unknowns

    # --- score ------------------------------------------------------------------------
    sc = run_stage("score", conn, cfg, llm, oa)
    assert sc["scored"] == 1
    opp = conn.execute("select * from opportunities").fetchone()
    assert float(opp["total_score"]) == 80.0  # all 4s
    assert opp["evidence_grade"] == "medium"  # model said high, but only 2 web sources -> capped
    assert opp["recommended_action"] == "prioritize_validation"
    assert opp["next_action"].startswith("Call the UP")

    # --- costs logged -----------------------------------------------------------------
    runs = conn.execute("select stage, status, cost_usd, web_searches from agent_runs").fetchall()
    assert all(r["status"] == "ok" for r in runs)
    assert sum(r["web_searches"] for r in runs) == 5
    assert sum(float(r["cost_usd"]) for r in runs) > 0

    # --- digest + review loop ---------------------------------------------------------
    d = digest.run(conn, cfg, send=True)
    assert d["count"] == 1 and d["emailed"] is False
    html = open(d["path"], encoding="utf-8").read()
    assert "Hermetic maize storage bags" in html and "picsbags.example" in html and "80" in html
    assert cmd_review(cfg, str(opp["id"])[:8], "promising", "call co-ops") == 0
    d2 = digest.run(conn, cfg, send=False)
    assert d2["count"] == 0  # already sent
    html2 = open(d2["path"], encoding="utf-8").read()
    assert "No new opportunity cleared the bar" in html2 and "promising" in html2


def test_budget_guard_stops_llm_calls(env):
    conn, cfg, oa, llm, fake, _ = env
    run_stage("discover", conn, cfg, llm, oa)
    cfg.monthly_budget_usd = 0.0
    with pytest.raises(BudgetExceeded):
        run_stage("triage", conn, cfg, llm, oa)
    assert fake.calls == []
    assert conn.execute("select count(*) n from papers where status='queued'").fetchone()["n"] == 0
