"""Test doubles: OpenAlex records/transport and a fake Grok Bot agent that answers queue tasks with
canned, schema-shaped results, so the whole pipeline can run offline against a real Postgres."""
from __future__ import annotations

import json
import re
from types import SimpleNamespace
from urllib.parse import parse_qs, urlparse

import httpx

ABSTRACTS = {
    "maize": (
        "We tested hermetic storage bags made from recycled polyethylene for smallholder maize in Limpopo. "
        "In field trials, grain losses fell from 12.4% to 1.9% over six months of storage. "
        "Bags cost an estimated R45 each to produce at pilot scale and could be reused for three seasons."
    ),
    "maize2": (
        "Smallholder maize storage losses were measured across 40 households in Mpumalanga. "
        "Hermetic bags reduced weevil damage by 85% compared with polypropylene sacks after 24 weeks. "
        "Adoption was limited by availability in local retail stores."
    ),
    "battery": (
        "We assessed second-life electric vehicle batteries as backup storage for small retailers in Gauteng. "
        "Repurposed modules retained 78% of rated capacity after 1,200 cycles in a 9-month field deployment. "
        "System cost was 41% lower than new lithium iron phosphate packs of equal usable capacity."
    ),
    "volatility": (
        "We forecast JSE Top 40 implied volatility using heterogeneous autoregressive models with option-implied inputs. "
        "Out-of-sample forecasts reduced mean squared error by 14% relative to GARCH over 2015-2024. "
        "A delta-hedged straddle strategy remained profitable after transaction costs of 2 volatility points."
    ),
    "policy": (
        "This study reviews agricultural policy discourse in South African provincial departments between 1994 "
        "and 2020 using qualitative content analysis of 230 policy documents and stakeholder interviews. "
        "Themes of land reform, extension services and market access dominated the discourse."
    ),
}


def _inverted(text: str) -> dict[str, list[int]]:
    inv: dict[str, list[int]] = {}
    for i, w in enumerate(text.split()):
        inv.setdefault(w, []).append(i)
    return inv


def make_work(oa_id, title, doi=None, abstract_key="maize", country="ZA", inst="University of Pretoria",
              date="2026-09-15", grobid=True):
    return {
        "id": f"https://openalex.org/{oa_id}",
        "doi": doi,
        "title": title,
        "display_name": title,
        "publication_date": date,
        "type": "article",
        "language": "en",
        "primary_location": {
            "landing_page_url": f"https://example.org/{oa_id}",
            "source": {"display_name": "South African Journal of Science"},
        },
        "open_access": {"is_oa": True, "oa_status": "gold", "oa_url": f"https://example.org/{oa_id}.pdf"},
        "best_oa_location": {"pdf_url": f"https://example.org/{oa_id}.pdf"},
        "authorships": [
            {
                "author_position": "first",
                "author": {"display_name": "T. Mokoena", "orcid": None},
                "is_corresponding": True,
                "institutions": [
                    {"id": f"https://openalex.org/I{abs(hash(inst)) % 10**8}", "display_name": inst,
                     "ror": None, "country_code": country, "type": "education"}
                ],
            }
        ],
        "primary_topic": {
            "display_name": "Post-harvest storage",
            "field": {"display_name": "Agricultural and Biological Sciences"},
            "subfield": {"display_name": "Food Science"},
        },
        "keywords": [{"display_name": "hermetic storage"}],
        "funders": [{"display_name": "National Research Foundation"}],
        "cited_by_count": 3,
        "abstract_inverted_index": _inverted(ABSTRACTS[abstract_key]) if abstract_key else None,
        "has_content": {"grobid_xml": grobid, "pdf": grobid},
    }


SWEEP = [
    make_work("W1", "Hermetic recycled-polyethylene bags cut smallholder maize storage losses", doi="https://doi.org/10.1/hermetic"),
    make_work("W2", "Agricultural policy discourse in provincial departments", abstract_key="policy", grobid=False),
    make_work("W3", "A short note", abstract_key=None, grobid=False),  # no abstract -> skipped
]
ENERGY = [make_work("E1", "Second-life EV batteries as backup storage for small businesses during load-shedding",
                    abstract_key="battery", inst="University of Johannesburg", grobid=False)]
FINANCE = [make_work("Q1", "Forecasting JSE Top 40 implied volatility with HAR models", abstract_key="volatility",
                     inst="University of Cape Town", grobid=False)]
KEYWORD = [
    # same paper as W1 under a different OpenAlex id but same DOI (upper case) -> must dedupe
    make_work("W99", "Hermetic recycled-polyethylene bags cut smallholder maize storage losses", doi="https://doi.org/10.1/HERMETIC"),
    make_work("W4", "Hermetic bags reduce weevil damage in stored maize in Mpumalanga", abstract_key="maize2",
              inst="University of Zimbabwe", country="ZW", grobid=False),
]

GROBID = """<TEI xmlns="http://www.tei-c.org/ns/1.0"><text><body>
<div><head>Results</head><p>Across 60 bags, mean grain losses fell from 12.4% to 1.9% over six months of storage.</p>
<p>Bag integrity remained above 95% after three seasons of reuse.</p></div></body></text></TEI>"""


PLAN_MSG = ('{"error":"Plan upgrade required","message":"The \\"from_created_date:2026-07-28\\" filter is part '
            'of daily sync, which comes with the Member+ and Partner plans."}')
BUDGET_MSG = '{"error":"Insufficient budget","message":"Insufficient budget. Keyless requests ... resets at midnight UTC"}'


def openalex_transport(calls: list, budget_exhausted: bool = False):
    """Mimics live OpenAlex (Oct 2026): created_date filters/sorts are plan-gated and answer 429."""
    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        url = urlparse(str(request.url))
        if url.netloc == "content.openalex.org":
            return httpx.Response(200, text=GROBID)
        q = parse_qs(url.query)
        flt = q.get("filter", [""])[0]
        if budget_exhausted:
            return httpx.Response(429, text=BUDGET_MSG)
        if "created_date" in flt or "created_date" in q.get("sort", [""])[0]:
            return httpx.Response(429, text=PLAN_MSG)
        search = q.get("search", [""])[0]
        if search:
            if "TinyML" in search:  # simulate a query OpenAlex rejects
                return httpx.Response(400, json={"error": "Invalid query"})
            results = KEYWORD if "hermetic" in search else []
        elif "primary_topic.field.id:11" in flt:
            results = SWEEP
        elif "primary_topic.field.id:21" in flt:
            results = ENERGY
        elif "primary_topic.subfield.id:2003" in flt:
            results = FINANCE
        else:
            results = []
        return httpx.Response(
            200, json={"meta": {"count": len(results), "next_cursor": None, "cost_usd": 0.0001}, "results": results}
        )

    return httpx.MockTransport(handler)


# ---------------------------------------------------------------------------
# Canned answers (shaped like the old model client, reused by FakeBot)
# ---------------------------------------------------------------------------
def _usage(tin=1500, tout=600, searches=0):
    return SimpleNamespace(
        input_tokens=tin, output_tokens=tout, cache_creation_input_tokens=0, cache_read_input_tokens=0,
        server_tool_use=SimpleNamespace(web_search_requests=searches) if searches else None,
    )


def _text(obj) -> SimpleNamespace:
    return SimpleNamespace(type="text", text=json.dumps(obj), citations=None)


class FakeMessages:
    def __init__(self, parent):
        self.parent = parent

    def create(self, **kw):
        self.parent.calls.append(kw)
        if kw.get("tools"):
            return self._research(kw)
        schema = kw["output_config"]["format"]["schema"]
        props = schema["properties"]
        user = kw["messages"][0]["content"]
        if "results" in props:
            out = {"results": []}
            enum = props["results"]["items"]["properties"]["category"]["enum"]
            for ref, title in re.findall(r'<paper ref="(P\d+)">\nTitle: ([^\n]+)', user):
                hit = any(w in title.lower() for w in ("maize", "batteries", "volatility"))
                out["results"].append({
                    "ref": ref, "commercial_signal": 8 if hit else 2,
                    "category": enum[0] if hit else "not_relevant",
                    "reason": "measured result" if hit else "policy analysis",
                })
        elif "problem" in props:
            m = re.search(r"<abstract>\n(.*?)\n</abstract>", user, re.S)
            sentences = [s.strip() + "." for s in m.group(1).split(". ") if s.strip()]
            out = {
                "problem": "Storage losses of smallholder maize", "methods": "Field trial", "setting": "Limpopo, maize",
                "findings": [
                    {"claim": "Hermetic bags reduced losses", "evidence_quote": sentences[1].rstrip("."),
                     "location": "abstract", "measured_or_inferred": "measured", "limitations": ["one region"]},
                    {"claim": "Fabricated", "evidence_quote": "bags eliminated aflatoxin entirely in all trials",
                     "location": "results", "measured_or_inferred": "measured", "limitations": []},
                ],
                "novelty_claim": "recycled polymer", "readiness_level": "real_world_trial",
                "readiness_evidence": ["field trial"], "applications_to_investigate": ["storage bags"],
                "missing_information": ["unit cost at scale"], "evidence_confidence": 0.7,
            }
        elif "hypothesis" in props:
            m = re.search(r"<abstract>\n(.*?)\n</abstract>", user, re.S)
            sentences = [s.strip() for s in m.group(1).split(". ") if s.strip()]
            out = {
                "hypothesis": "HAR models with option-implied inputs forecast JSE Top 40 implied volatility better than GARCH",
                "signal_definition": "HAR regression on daily, weekly, monthly realised and implied vol",
                "instruments_and_market": "JSE Top 40 index options", "data_period": "2015-2024",
                "data_needed": ["Top 40 option chains", "index prices"],
                "out_of_sample_tested": True, "transaction_costs_modelled": True, "liquidity_considered": False,
                "reported_performance": "14% lower MSE out of sample",
                "findings": [{"claim": "Lower forecast error", "evidence_quote": sentences[1], "location": "abstract",
                              "measured_or_inferred": "measured", "limitations": ["single index"]}],
                "red_flags": ["single market regime"], "replication_difficulty": "moderate", "evidence_confidence": 0.6,
            }
        elif "opportunities" in props and "batteries" in user:
            out = {"opportunities": [
                {"title": "Second-life battery backup packs for spaza shops",
                 "customer_problem": "Stock spoilage and lost sales during outages", "target_customer": "Township retailers",
                 "proposed_solution": "Refurbished EV-module backup pack", "revenue_model": "Rent-to-own",
                 "enabling_finding_refs": ["F1"], "why_existing_solutions_fall_short": "New LFP packs too expensive",
                 "delivery_requirements": ["module testing rig"], "commercial_assumptions": ["Shops pay R400/month"],
                 "key_risks": ["Battery safety certification"], "cheapest_disproving_experiment": "Offer 10 shops a 3-month trial",
                 "confidence": 0.6},
            ]}
        elif "opportunities" in props:
            out = {"opportunities": [
                {"title": "Hermetic maize storage bags for smallholder farmers",
                 "customer_problem": "Post-harvest losses", "target_customer": "Smallholder maize farmers via agri-dealers",
                 "proposed_solution": "Reusable hermetic bag", "revenue_model": "Unit sales via co-ops",
                 "enabling_finding_refs": ["F1"], "why_existing_solutions_fall_short": "Imported bags expensive",
                 "delivery_requirements": ["film extrusion partner"], "commercial_assumptions": ["Farmers pay R60"],
                 "key_risks": ["Counterfeit bags"], "cheapest_disproving_experiment": "Pre-sell 50 bags at 2 co-ops",
                 "confidence": 0.9},  # out-of-range on purpose -> clamped
            ]}
        elif "competitors" in props:
            out = {
                "competitors": [
                    {"name": "PICS bags", "what_they_offer": "hermetic triple-layer bags", "source_ref": "S1"},
                    {"name": "Ghost Co", "what_they_offer": "made up", "source_ref": "S9"},
                ],
                "price_evidence": [{"item": "PICS bag", "price": "about R70", "source_ref": "none"}],
                "regulatory_findings": [{"finding": "No registration needed for storage bags", "source_ref": "S2"}],
                "ip_findings": [],
                "customer_hypotheses": ["co-ops buy in bulk"], "unit_economics_assumptions": ["R45 cost"],
                "unknowns_needing_field_research": ["willingness to pay"], "pilot_plan": ["sell 50 bags"],
                "recommendation": "investigate", "recommendation_reason": "real problem, incumbent pricey",
            }
        elif "criteria" in props:
            keys = list(props["criteria"]["properties"])
            out = {
                "criteria": {k: {"score": 4, "rationale": "ok"} for k in keys},
                "evidence_grade": "high",
                "blocking_risks": [{"risk": "Counterfeits", "severity": "minor"}],
                "unsupported_claims": ["R60 price"],
                "next_action": "Call the UP technology transfer office",
            }
        else:  # pragma: no cover
            raise AssertionError(f"unexpected schema {list(props)}")
        return SimpleNamespace(content=[_text(out)], stop_reason="end_turn", usage=_usage())

    def _research(self, kw):
        self.parent.research_turns += 1
        # first turn pauses to exercise the pause_turn continuation
        if self.parent.research_turns % 2 == 1:
            return SimpleNamespace(
                content=[SimpleNamespace(type="text", text="Searching for alternatives. ", citations=None)],
                stop_reason="pause_turn", usage=_usage(3000, 100, 2),
            )
        c1 = SimpleNamespace(url="https://picsbags.example/za", title="PICS bags South Africa", cited_text="PICS bags sell for R70")
        c2 = SimpleNamespace(url="https://dalrrd.example/regs", title="Regulations", cited_text="storage containers")
        blocks = [
            SimpleNamespace(type="text", text="## Existing alternatives\nPICS hermetic bags are sold locally", citations=[c1]),
            SimpleNamespace(type="text", text=". No product registration appears to be required.", citations=[c2]),
            SimpleNamespace(type="text", text="\n## Verdict\nInvestigate.", citations=None),
        ]
        return SimpleNamespace(content=blocks, stop_reason="end_turn", usage=_usage(9000, 1200, 3))


class CannedModel:
    def __init__(self):
        self.calls: list[dict] = []
        self.research_turns = 0
        self.messages = FakeMessages(self)


class FakeBot:
    """Plays the Grok Bot agent: answers a pulled task from its instructions, input and output_schema."""

    def __init__(self, unfetched_source: bool = True):
        self.client = CannedModel()
        self.tasks: list[dict] = []
        self.unfetched_source = unfetched_source

    def answer(self, task: dict) -> dict:
        self.tasks.append(task)
        kw = {"system": task["instructions"], "messages": [{"role": "user", "content": task["input"]}],
              "output_config": {"format": {"type": "json_schema", "schema": task["output_schema"]}}}
        resp = self.client.messages.create(**kw)
        out = json.loads(resp.content[0].text)
        props = task["output_schema"]["properties"]
        if "results" in props and "sector" in props["results"]["items"]["properties"]:
            cands = dict(re.findall(r'<paper ref="(P\d+)">.*?Candidate sectors: ([^\n]+)', task["input"], re.S))
            from aroi import sectors as _sec
            for r in out["results"]:
                keys = cands[r["ref"]].split(", ")
                r["sector"] = next((k for k in keys if r["category"] in _sec.get(k).triage_categories), keys[0])
        if "memo" in props:
            out["memo"] = ("## Existing alternatives\nPICS hermetic bags are sold locally [S1]. No product "
                           "registration appears to be required [S2].\n## Verdict\nInvestigate.\n\nSOURCES\n"
                           "[S1] https://picsbags.example/za\n[S2] https://dalrrd.example/regs")
            out["searches_used"] = 5
            out["sources"] = [
                {"id": "S1", "url": "https://picsbags.example/za", "title": "PICS bags South Africa",
                 "fetched": True, "quoted_text": "PICS bags sell for R70"},
                {"id": "S2", "url": "https://dalrrd.example/regs", "title": "Regulations", "fetched": True,
                 "quoted_text": "storage containers"},
            ]
            if self.unfetched_source:  # listed but never opened: must not become evidence
                out["sources"].append({"id": "S9", "url": "https://ghostco.example/", "title": "Ghost Co",
                                       "fetched": False, "quoted_text": ""})
        return out
