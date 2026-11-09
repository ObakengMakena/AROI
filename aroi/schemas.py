"""Strict JSON schemas for the results Grok Bot agents submit to the work queue (aroi/agent_queue.py).
Every object sets additionalProperties=false and lists all properties as required, so a submission
with a missing, extra or misspelled field is rejected with a clear error instead of half-ingested.
Numeric ranges are enforced here too (and clamped again in code, as a second line of defence)."""
from __future__ import annotations

from .scoring import CRITERIA


def _obj(props: dict, required: list[str] | None = None) -> dict:
    return {
        "type": "object",
        "properties": props,
        "required": required if required is not None else list(props),
        "additionalProperties": False,
    }


STR = {"type": "string"}
STR_LIST = {"type": "array", "items": STR}

CONF = {"type": "number", "minimum": 0, "maximum": 1}
NONEMPTY = {"type": "string", "minLength": 1}


def triage_schema(categories: tuple[str, ...], sectors: tuple[str, ...] | None = None) -> dict:
    item = {
        "ref": STR,
        "commercial_signal": {"type": "integer", "minimum": 0, "maximum": 10},
        "category": {"type": "string", "enum": [*dict.fromkeys(categories), "not_relevant"]},
        "reason": NONEMPTY,
    }
    if sectors:
        # papers can belong to several sectors; the agent names the one it is triaging the paper into
        item["sector"] = {"type": "string", "enum": list(sectors)}
    return _obj({"results": {"type": "array", "items": _obj(item)}})


ANALYSIS = _obj(
    {
        "problem": STR,
        "methods": STR,
        "setting": STR,
        "findings": {
            "type": "array",
            "items": _obj(
                {
                    "claim": NONEMPTY,
                    "evidence_quote": STR,
                    "location": STR,
                    "measured_or_inferred": {"type": "string", "enum": ["measured", "inferred"]},
                    "limitations": STR_LIST,
                }
            ),
        },
        "novelty_claim": STR,
        "readiness_level": {
            "type": "string",
            "enum": ["review_or_theory", "simulation_or_lab", "prototype_or_pilot", "real_world_trial",
                     "certified_or_commercial", "unclear"],
        },
        "readiness_evidence": STR_LIST,
        "applications_to_investigate": STR_LIST,
        "missing_information": STR_LIST,
        "evidence_confidence": CONF,
    }
)

_FINDINGS = ANALYSIS["properties"]["findings"]

FINANCE_ANALYSIS = _obj(
    {
        "hypothesis": STR,
        "signal_definition": STR,
        "instruments_and_market": STR,
        "data_period": STR,
        "data_needed": STR_LIST,
        "out_of_sample_tested": {"type": "boolean"},
        "transaction_costs_modelled": {"type": "boolean"},
        "liquidity_considered": {"type": "boolean"},
        "reported_performance": STR,
        "findings": _FINDINGS,
        "red_flags": STR_LIST,
        "replication_difficulty": {"type": "string", "enum": ["easy", "moderate", "hard"]},
        "evidence_confidence": CONF,
    }
)

OPPORTUNITIES = _obj(
    {
        "opportunities": {
            "type": "array",
            "items": _obj(
                {
                    "title": STR,
                    "customer_problem": STR,
                    "target_customer": STR,
                    "proposed_solution": STR,
                    "revenue_model": STR,
                    "enabling_finding_refs": STR_LIST,
                    "why_existing_solutions_fall_short": STR,
                    "delivery_requirements": STR_LIST,
                    "commercial_assumptions": STR_LIST,
                    "key_risks": STR_LIST,
                    "cheapest_disproving_experiment": STR,
                    "confidence": CONF,
                }
            ),
        }
    }
)

_REF = {"type": "string", "pattern": "^(S[0-9]+|none)$",
        "description": "Source id like S3 from the `sources` list, or 'none'"}

VALIDATION = _obj(
    {
        "competitors": {"type": "array", "items": _obj({"name": STR, "what_they_offer": STR, "source_ref": _REF})},
        "price_evidence": {"type": "array", "items": _obj({"item": STR, "price": STR, "source_ref": _REF})},
        "regulatory_findings": {"type": "array", "items": _obj({"finding": STR, "source_ref": _REF})},
        "ip_findings": {"type": "array", "items": _obj({"finding": STR, "source_ref": _REF})},
        "customer_hypotheses": STR_LIST,
        "unit_economics_assumptions": STR_LIST,
        "unknowns_needing_field_research": STR_LIST,
        "pilot_plan": STR_LIST,
        "recommendation": {"type": "string", "enum": ["investigate", "park", "reject"]},
        "recommendation_reason": STR,
    }
)

# What a validation agent submits: the structured fields above plus its memo and the list of pages it
# actually fetched. Only sources with fetched=true and an http(s) URL are stored as evidence.
SOURCE = _obj(
    {
        "id": {"type": "string", "pattern": "^S[0-9]+$"},
        "url": {"type": "string", "pattern": "^https?://", "maxLength": 2000},
        "title": STR,
        "fetched": {"type": "boolean"},
        "quoted_text": STR,
    }
)
VALIDATION_SUBMISSION = _obj(
    {
        **VALIDATION["properties"],
        "memo": NONEMPTY,
        "searches_used": {"type": "integer", "minimum": 0, "maximum": 100},
        "sources": {"type": "array", "items": SOURCE, "maxItems": 60},
    }
)

_CRITERION = _obj({"score": {"type": "integer", "enum": [1, 2, 3, 4, 5]}, "rationale": STR})

REVIEW = _obj(
    {
        "criteria": _obj({key: _CRITERION for key in CRITERIA}),
        "evidence_grade": {"type": "string", "enum": ["high", "medium", "low"]},
        "blocking_risks": {
            "type": "array",
            "items": _obj({"risk": STR, "severity": {"type": "string", "enum": ["blocker", "major", "minor"]}}),
        },
        "unsupported_claims": STR_LIST,
        "next_action": STR,
    }
)
