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
    assert cfg.sectors == ["agriculture", "energy", "ai", "construction", "quant_finance"]

    # --- discover: every sector sweeps; one bad query is skipped, not fatal ---------------
    s = run_stage("discover", conn, cfg, llm, oa)
    sweeps = [c for c in calls if "search=" not in c and "content.openalex" not in c]
    assert len(sweeps) == 5
    for frag in ("primary_topic.field.id%3A11", "primary_topic.field.id%3A21", "primary_topic.subfield.id%3A1702%7C1707",
                 "primary_topic.subfield.id%3A2205%7C2215%7C3322", "primary_topic.subfield.id%3A2003"):
        assert any(frag in c and "institutions.country_code%3Aza" in c for c in sweeps), frag
    assert "api_key=test-key" in sweeps[0]
    assert s["queries"] == 36 and s["query_errors"] == 1  # 37 seeded queries, the TinyML one fails
    papers = conn.execute("select openalex_id, doi, status, found_by, sector from papers order by openalex_id").fetchall()
    assert {p["openalex_id"]: p["sector"] for p in papers} == {
        "E1": "energy", "Q1": "quant_finance", "W1": "agriculture", "W2": "agriculture", "W3": "agriculture",
        "W4": "agriculture"}, "W99 shares W1's DOI and must merge into it"
    w1 = next(p for p in papers if p["openalex_id"] == "W1")
    assert w1["doi"] == "10.1/hermetic" and "sweep:agriculture" in w1["found_by"] and len(w1["found_by"]) == 2
    assert next(p for p in papers if p["openalex_id"] == "W3")["status"] == "triaged_out"

    run_stage("discover", conn, cfg, llm, oa)
    assert conn.execute("select count(*) n from papers").fetchone()["n"] == 6, "no duplicates on re-run"

    # --- triage: each sector screened with its own prompt and categories -------------------
    t = run_stage("triage", conn, cfg, llm, oa)
    assert t["queued"] == 4 and t["dropped"] == 1
    assert t["queued_by_sector"] == {"agriculture": 2, "energy": 1, "quant_finance": 1}
    triage_calls = [c for c in fake.calls if "results" in c["output_config"]["format"]["schema"]["properties"]]
    assert any("renewable energy" in c["system"] for c in triage_calls)
    energy_enum = next(c for c in triage_calls if "renewable energy" in c["system"])[
        "output_config"]["format"]["schema"]["properties"]["results"]["items"]["properties"]["category"]["enum"]
    assert "batteries_and_storage" in energy_enum and "crops_and_soil" not in energy_enum

    # --- analyze: per-sector quota; finance gets the hypothesis analyst --------------------
    a = run_stage("analyze", conn, cfg, llm, oa)
    assert a["analyzed"] == 4 and a["by_sector"] == {"agriculture": 2, "energy": 1, "quant_finance": 1}
    assert a["full_text"] == 1  # only W1 has GROBID text
    fs = conn.execute("select f.evidence_quote, f.quote_verified, p.sector from findings f join papers p on p.id=f.paper_id").fetchall()
    assert not any(f["quote_verified"] for f in fs if "aflatoxin" in f["evidence_quote"]), "invented quote rejected"
    assert sum(f["quote_verified"] for f in fs if f["sector"] == "agriculture") == 2
    assert any(f["quote_verified"] for f in fs if f["sector"] == "quant_finance")
    assert any("content.openalex.org/works/W1.grobid-xml" in c for c in calls)
    analysis_prompts = [c for c in fake.calls if "problem" in c["output_config"]["format"]["schema"]["properties"]]
    assert all("untrusted" in c["system"] for c in analysis_prompts)
    h = conn.execute("select * from finance_hypotheses").fetchone()
    # 25 OOS + 20 costs + 0 liquidity + 8 moderate + 10 verified quote + 12 (0.6 conf) - 5 (1 red flag) = 70
    assert float(h["testability_score"]) == 70.0 and h["recommended_action"] == "backtest_candidate"
    assert h["instruments_and_market"] == "JSE Top 40 index options"

    # --- generate: product sectors only; merging stays within a sector -------------------
    g = run_stage("generate", conn, cfg, llm, oa)
    assert g["created"] == 2 and g["merged"] == 1, "two agri papers -> one idea; energy -> its own"
    opps = {o["sector"]: o for o in conn.execute("select * from opportunities").fetchall()}
    assert set(opps) == {"agriculture", "energy"}, "finance papers never become product opportunities"
    agri = opps["agriculture"]
    assert float(agri["generator_confidence"]) == 1.0  # clamped from 1.7
    assert "IPR-PFRD" in agri["ip_pathway"] and "University of Pretoria" in agri["ip_pathway"]
    assert "University of Zimbabwe" in agri["ip_pathway"], "merged paper's IP route is kept too"
    assert "University of Johannesburg" in opps["energy"]["ip_pathway"]
    energy_gen = next(c for c in fake.calls if "batteries" in c["messages"][0]["content"]
                      and "opportunities" in c["output_config"]["format"]["schema"]["properties"])
    assert "NERSA" in energy_gen["system"] and "Plant Breeders" not in energy_gen["system"]

    # --- validate: one per sector per run -------------------------------------------------
    v = run_stage("validate", conn, cfg, llm, oa)
    assert v["validated"] == 2 and v["by_sector"] == {"agriculture": 1, "energy": 1}
    assert v["searches"] == 10  # 5 per validation (2 in the paused turn + 3 after)
    ev = conn.execute("select source_url, claim_supported from evidence_items").fetchall()
    assert {e["source_url"] for e in ev} == {"https://picsbags.example/za", "https://dalrrd.example/regs"}
    assert not any("Ghost Co" in e["claim_supported"] for e in ev)
    mv = conn.execute("select memo, structured from market_validations limit 1").fetchone()
    assert "[S1]" in mv["memo"] and "SOURCES" in mv["memo"]
    unknowns = " ".join(mv["structured"]["unknowns_needing_field_research"])
    assert "Ghost Co" in unknowns and "PICS bag: about R70" in unknowns

    # --- score ------------------------------------------------------------------------
    sc = run_stage("score", conn, cfg, llm, oa)
    assert sc["scored"] == 2
    for o in conn.execute("select * from opportunities").fetchall():
        assert float(o["total_score"]) == 80.0  # all 4s
        assert o["evidence_grade"] == "medium"  # model said high, but only 2 web sources -> capped
        assert o["recommended_action"] == "prioritize_validation"

    # --- costs logged -----------------------------------------------------------------
    runs = conn.execute("select stage, status, cost_usd, web_searches from agent_runs").fetchall()
    assert all(r["status"] == "ok" for r in runs)
    assert sum(r["web_searches"] for r in runs) == 10
    assert {r["stage"].split(":")[0] for r in runs} >= {"triage", "analysis", "opportunities", "validation_research", "review"}

    # --- digest + review loop ---------------------------------------------------------
    d = digest.run(conn, cfg, send=True)
    assert d["count"] == 3 and d["emailed"] is False
    assert d["by_sector"] == {"agriculture": 1, "energy": 1, "ai": 0, "construction": 0, "quant_finance": 1}
    html = open(d["path"], encoding="utf-8").read()
    for text in ("Hermetic maize storage bags", "Second-life battery backup packs", "picsbags.example",
                 "Quant finance — hypotheses to test", "JSE Top 40 index options", "not trading signals",
                 "Artificial intelligence and data products"):
        assert text in html, text
    agri_id = str(agri["id"])[:8]
    assert cmd_review(cfg, agri_id, "promising", "call co-ops") == 0
    assert cmd_review(cfg, str(h["id"])[:8], "rejected", "needs paid option data") == 0
    assert conn.execute("select stage from finance_hypotheses").fetchone()["stage"] == "rejected"
    d2 = digest.run(conn, cfg, send=False)
    assert d2["count"] == 0  # already sent
    html2 = open(d2["path"], encoding="utf-8").read()
    assert "No new idea cleared" in html2 and "promising" in html2 and "needs paid option data" in html2


def test_sectors_setting_limits_scope(env):
    conn, cfg, oa, llm, fake, calls = env
    cfg.sectors = ["energy"]
    run_stage("discover", conn, cfg, llm, oa)
    assert {p["sector"] for p in conn.execute("select sector from papers").fetchall()} == {"energy"}
    assert all("field.id%3A11" not in c for c in calls)


def test_budget_guard_stops_llm_calls(env):
    conn, cfg, oa, llm, fake, _ = env
    run_stage("discover", conn, cfg, llm, oa)
    cfg.monthly_budget_usd = 0.0
    with pytest.raises(BudgetExceeded):
        run_stage("triage", conn, cfg, llm, oa)
    assert fake.calls == []
    assert conn.execute("select count(*) n from papers where status='queued'").fetchone()["n"] == 0
