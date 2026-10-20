"""JSON schemas for structured outputs. Every object sets additionalProperties=false and lists all
properties as required, as Claude's structured-outputs feature expects. Numeric ranges are not
supported by the schema grammar, so they are enforced in code (see clamp helpers in the stages)."""
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

TRIAGE = _obj(
    {
        "results": {
            "type": "array",
            "items": _obj(
                {
                    "ref": STR,
                    "commercial_signal": {"type": "integer", "enum": list(range(11))},
                    "sector": {
                        "type": "string",
                        "enum": ["crops_and_soil", "food_processing_and_preservation", "livestock_and_aquaculture",
                                 "agri_waste_and_bioproducts", "farm_technology", "not_relevant"],
                    },
                    "reason": STR,
                }
            ),
        }
    }
)

ANALYSIS = _obj(
    {
        "problem": STR,
        "methods": STR,
        "setting": STR,
        "findings": {
            "type": "array",
            "items": _obj(
                {
                    "claim": STR,
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
            "enum": ["review_or_theory", "lab", "greenhouse_or_pilot", "field_trial",
                     "multi_site_or_commercial", "unclear"],
        },
        "readiness_evidence": STR_LIST,
        "applications_to_investigate": STR_LIST,
        "missing_information": STR_LIST,
        "evidence_confidence": {"type": "number"},
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
                    "confidence": {"type": "number"},
                }
            ),
        }
    }
)

_REF = {"type": "string", "description": "Source id like S3 from the memo's SOURCES list, or 'none'"}

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
