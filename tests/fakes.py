"""Test doubles: OpenAlex records/transport and a fake Anthropic client that returns canned,
schema-shaped answers, so the whole pipeline can run offline against a real Postgres."""
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
KEYWORD = [
    # same paper as W1 under a different OpenAlex id but same DOI (upper case) -> must dedupe
    make_work("W99", "Hermetic recycled-polyethylene bags cut smallholder maize storage losses", doi="https://doi.org/10.1/HERMETIC"),
    make_work("W4", "Hermetic bags reduce weevil damage in stored maize in Mpumalanga", abstract_key="maize2",
              inst="University of Zimbabwe", country="ZW", grobid=False),
]

GROBID = """<TEI xmlns="http://www.tei-c.org/ns/1.0"><text><body>
<div><head>Results</head><p>Across 60 bags, mean grain losses fell from 12.4% to 1.9% over six months of storage.</p>
<p>Bag integrity remained above 95% after three seasons of reuse.</p></div></body></text></TEI>"""


def openalex_transport(calls: list):
    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        url = urlparse(str(request.url))
        if url.netloc == "content.openalex.org":
            return httpx.Response(200, text=GROBID)
        q = parse_qs(url.query)
        results = KEYWORD if "search" in q else SWEEP
        return httpx.Response(
            200, json={"meta": {"count": len(results), "next_cursor": None, "cost_usd": 0.0001}, "results": results}
        )

    return httpx.MockTransport(handler)


# ---------------------------------------------------------------------------
# Fake Anthropic client
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
            for ref, title in re.findall(r'<paper ref="(P\d+)">\nTitle: ([^\n]+)', user):
                maize = "maize" in title.lower()
                out["results"].append({
                    "ref": ref, "commercial_signal": 8 if maize else 2,
                    "sector": "food_processing_and_preservation" if maize else "not_relevant",
                    "reason": "measured storage result" if maize else "policy analysis",
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
                "novelty_claim": "recycled polymer", "readiness_level": "field_trial",
                "readiness_evidence": ["field trial"], "applications_to_investigate": ["storage bags"],
                "missing_information": ["unit cost at scale"], "evidence_confidence": 0.7,
            }
        elif "opportunities" in props:
            out = {"opportunities": [
                {"title": "Hermetic maize storage bags for smallholder farmers",
                 "customer_problem": "Post-harvest losses", "target_customer": "Smallholder maize farmers via agri-dealers",
                 "proposed_solution": "Reusable hermetic bag", "revenue_model": "Unit sales via co-ops",
                 "enabling_finding_refs": ["F1", "F9"], "why_existing_solutions_fall_short": "Imported bags expensive",
                 "delivery_requirements": ["film extrusion partner"], "commercial_assumptions": ["Farmers pay R60"],
                 "key_risks": ["Counterfeit bags"], "cheapest_disproving_experiment": "Pre-sell 50 bags at 2 co-ops",
                 "confidence": 1.7},  # out-of-range on purpose -> clamped
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


class FakeAnthropic:
    def __init__(self):
        self.calls: list[dict] = []
        self.research_turns = 0
        self.messages = FakeMessages(self)
