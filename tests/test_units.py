import pytest

from aroi import schemas
from aroi.llm import cost_of
from aroi.normalize import (
    normalize_doi,
    parse_work,
    quote_in_source,
    reconstruct_abstract,
    tei_to_text,
    title_fingerprint,
)
from aroi.scoring import CRITERIA, action_for, cap_grade, weighted_score

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


def test_cost_of():
    assert cost_of("claude-haiku-5-5", 50_000, 0) == pytest.approx(0.005)
    assert cost_of("claude-haiku-5-5", 200_000, 0) == pytest.approx(0.10)  # long-prompt tier
    assert cost_of("claude-sonnet-5-5", 0, 1_000_000) == pytest.approx(10.0)
    assert cost_of("claude-sonnet-5-5", 0, 0, searches=3) == pytest.approx(0.03)


def _walk(node, path="$"):
    if isinstance(node, dict):
        yield path, node
        for k, v in node.items():
            yield from _walk(v, f"{path}.{k}")
    elif isinstance(node, list):
        for i, v in enumerate(node):
            yield from _walk(v, f"{path}[{i}]")


@pytest.mark.parametrize("name", ["TRIAGE", "ANALYSIS", "OPPORTUNITIES", "VALIDATION", "REVIEW"])
def test_schemas_fit_structured_output_rules(name):
    schema = getattr(schemas, name)
    unsupported = {"minimum", "maximum", "minLength", "maxLength", "multipleOf", "maxItems"}
    for path, node in _walk(schema):
        if node.get("type") == "object":
            assert node.get("additionalProperties") is False, path
            assert sorted(node["required"]) == sorted(node["properties"]), path
        assert not (unsupported & set(node)), path
