import pytest

from aroi import prompts, schemas, sectors
from aroi.cli import rotate_sectors
from aroi.normalize import (
    normalize_doi,
    parse_work,
    check_quote,
    quote_in_source,
    verify_quote,
    reconstruct_abstract,
    tei_to_text,
    title_fingerprint,
)
from aroi import scoring
from aroi.scoring import CRITERIA, action_for, cap_grade, finance_action, weighted_score

from .fakes import make_work


def test_reconstruct_abstract():
    inv = {"Hermetic": [0], "bags": [1], "cut": [2], "losses": [3], "by": [4], "80%.": [5]}
    assert reconstruct_abstract(inv) == "Hermetic bags cut losses by 80%."
    assert reconstruct_abstract(None) is None
    assert reconstruct_abstract({}) is None


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("https://doi.org/10.1000/ABC.123", "10.1000/abc.123"),
        ("http://dx.doi.org/10.1000/x", "10.1000/x"),
        ("doi:10.1000/x", "10.1000/x"),
        (None, None),
        ("", None),
    ],
)
def test_normalize_doi(raw, expected):
    assert normalize_doi(raw) == expected


def test_title_fingerprint_ignores_case_accents_punctuation_and_tags():
    a = title_fingerprint("Effect of <i>Bacillus</i> on Maize — a Field Trial")
    b = title_fingerprint("effect of bacillus on maize: a field trial")
    assert a == b
    assert title_fingerprint("Café") == title_fingerprint("Cafe")


def test_parse_work_extracts_institutions_and_abstract():
    p = parse_work(make_work("W1", "Hermetic maize storage", doi="https://doi.org/10.1/A"))
    assert p["openalex_id"] == "W1"
    assert p["doi"] == "10.1/a"
    assert p["abstract"].startswith("We tested hermetic")
    assert p["countries"] == ["ZA"]
    assert p["institutions"][0]["name"] == "University of Pretoria"
    assert p["has_grobid"] is True


def test_tei_to_text():
    xml = """<TEI xmlns="http://www.tei-c.org/ns/1.0"><text><body>
      <div><head>Results</head><p>Losses fell from 12% to 2%.</p></div>
      <figure><figDesc>Table 1. Weevil counts.</figDesc></figure></body></text></TEI>"""
    t = tei_to_text(xml)
    assert "## Results" in t and "Losses fell from 12% to 2%." in t and "Weevil counts" in t
    assert tei_to_text("not xml") is None


def test_quote_in_source():
    src = "In field trials, grain losses fell from 12.4% to 1.9% over six months of storage."
    assert quote_in_source("grain losses fell from 12.4% to 1.9%", src)
    assert quote_in_source("Grain  losses fell from 12.4%…over six months of storage", src)
    assert not quote_in_source("grain losses fell to zero", src)
    assert not quote_in_source("short", src)
    assert not quote_in_source("", src)


def test_quote_check_min_length_and_negation():
    src = ("The coating did not reduce corrosion in coastal exposure tests. "
           "Losses fell from 12.4% to 1.9% over six months of storage in Limpopo.")
    assert check_quote("fell from 12.4% to 1.9%", src) == (False, "too_short")  # < 30 chars
    assert check_quote("reduce corrosion in coastal exposure tests", src) == (False, "negated")
    assert check_quote("Losses fell from 12.4% to 1.9% over six months", src) == (True, "ok")
    assert check_quote("Losses fell from 12.4% to 0.5% over six months", src) == (False, "not_found")
    ok, where, why = verify_quote("Losses fell from 12.4% to 1.9% over six months", "abstract only", src)
    assert (ok, where, why) == (True, "full_text", "ok")


def test_confidence_scales_with_verified_ratio():
    from aroi.stages.analyze import confidence_cap
    assert confidence_cap(0, 3, True) == pytest.approx(0.3)
    assert confidence_cap(3, 3, True) == pytest.approx(1.0)
    assert confidence_cap(3, 3, False) == pytest.approx(0.6)
    assert confidence_cap(1, 2, True) == pytest.approx(0.65)


def test_weighted_score_matches_blueprint():
    assert weighted_score({k: 3 for k in CRITERIA}) == 60.0
    assert weighted_score({k: 5 for k in CRITERIA}) == 100.0
    assert weighted_score({k: 1 for k in CRITERIA}) == 20.0
    with pytest.raises(ValueError):
        weighted_score({k: 6 for k in CRITERIA})
    with pytest.raises(ValueError):
        weighted_score({"scientific_evidence": 3})


def test_action_and_grade_guards():
    assert action_for(80, "high", False) == "prioritize_validation"
    assert action_for(80, "low", False) == "investigate_uncertainties"  # weak evidence can't be prioritised
    assert action_for(90, "high", True) == "resolve_blocker_first"
    assert action_for(60, "medium", False) == "investigate_uncertainties"
    assert action_for(40, "high", False) == "deprioritize"
    assert cap_grade("high", verified_quotes=0, web_sources=0) == "low"
    assert cap_grade("high", verified_quotes=2, web_sources=1) == "medium"
    assert cap_grade("high", verified_quotes=2, web_sources=5) == "high"
    assert cap_grade("medium", verified_quotes=5, web_sources=9) == "medium"


def _walk(node, path="$"):
    if isinstance(node, dict):
        yield path, node
        for k, v in node.items():
            yield from _walk(v, f"{path}.{k}")
    elif isinstance(node, list):
        for i, v in enumerate(node):
            yield from _walk(v, f"{path}[{i}]")


@pytest.mark.parametrize("name", ["TRIAGE", "ANALYSIS", "FINANCE_ANALYSIS", "OPPORTUNITIES", "VALIDATION", "VALIDATION_SUBMISSION", "REVIEW"])
def test_schemas_are_strict_and_valid(name):
    from jsonschema import Draft202012Validator
    schema = schemas.triage_schema(("a", "b"), ("x", "y")) if name == "TRIAGE" else getattr(schemas, name)
    Draft202012Validator.check_schema(schema)
    for path, node in _walk(schema):
        if node.get("type") == "object":
            assert node.get("additionalProperties") is False, path
            assert sorted(node["required"]) == sorted(node["properties"]), path


def test_testability_score():
    best = scoring.testability_score(out_of_sample=True, costs_modelled=True, liquidity_considered=True,
                             replication="easy", verified_quotes=2, evidence_confidence=1.0, red_flags=0)
    assert best == 100.0 and finance_action(best) == "backtest_candidate"
    weak = scoring.testability_score(out_of_sample=False, costs_modelled=False, liquidity_considered=False,
                             replication="hard", verified_quotes=0, evidence_confidence=0.5, red_flags=8)
    assert weak == 0.0 and finance_action(weak) == "discard"
    assert finance_action(45) == "watch"


def test_rotate_sectors():
    keys = ["a", "b", "c"]
    assert rotate_sectors(keys, 0) == ["a", "b", "c"]
    assert rotate_sectors(keys, 4) == ["b", "c", "a"]
    assert rotate_sectors([], 3) == []


@pytest.mark.parametrize("key", list(sectors.SECTORS))
def test_every_sector_renders_every_prompt(key):
    for name in ("TRIAGE_SYSTEM", "ANALYSIS_SYSTEM", "FINANCE_ANALYSIS_SYSTEM", "OPPORTUNITY_SYSTEM",
                 "VALIDATION_SYSTEM", "REVIEW_SYSTEM"):
        text = prompts.render(getattr(prompts, name), "ZA", key)
        assert "{" not in text.replace("{{", ""), (key, name)
    sec = sectors.get(key)
    assert sec.context.get("ZA") and sec.triage_categories and "not_relevant" not in sec.triage_categories
    assert sec.sweep_filter.startswith("primary_topic.")
