"""Sector registry. Each sector defines how it is discovered (the OpenAlex sweep filter), how papers
are screened (triage categories and what counts as commercial signal), and the market context the
agents reason with. Add a sector here plus seed queries in sql/seed.sql; nothing else changes.

OpenAlex field/subfield ids follow the Scopus ASJC codes, e.g. 1702 = Artificial Intelligence."""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class Sector:
    key: str
    label: str
    kind: str  # "product" -> opportunity pipeline; "quant" -> research-only hypothesis track
    sweep_filter: str  # OpenAlex filter fragment selecting the sector's papers
    triage_categories: tuple[str, ...]
    signal_guide: str  # what a 9-10 looks like vs 0-2 for this sector
    readiness_hint: str
    context: dict[str, str] = field(default_factory=dict)  # per-country market/regulatory notes
    ip_note: dict[str, str] = field(default_factory=dict)


SECTORS: dict[str, Sector] = {}


def _add(s: Sector) -> None:
    SECTORS[s.key] = s


_add(
    Sector(
        key="agriculture",
        label="agriculture and food systems",
        kind="product",
        # 11 = Agricultural and Biological Sciences (food science, soil, agronomy, animal science...)
        sweep_filter="primary_topic.field.id:11",
        triage_categories=("crops_and_soil", "food_processing_and_preservation", "livestock_and_aquaculture",
                           "agri_waste_and_bioproducts", "farm_technology"),
        signal_guide="a measured result (material, method, organism, device, formulation, process) with an obvious "
        "buyer such as farmers, processors or packhouses, and a plausible low-cost path to a product",
        readiness_hint="lab -> greenhouse/pilot -> field trial -> multi-site/commercial",
        context={
            "ZA": """- Agriculture is dual: large commercial farms (export horticulture, citrus, grain, sugar, red meat, poultry)
  and ~2 million smallholder/subsistence households with very different purchasing power. Name which one.
- Buyers: commercial farmers, agri co-ops and input retailers, food processors and packhouses, retailers'
  supplier programmes, feedlots, municipalities, government extension programmes.
- Regulation to check (verify current status): fertilisers, farm feeds, agricultural and stock remedies
  (Act 36 of 1947); foodstuffs (Act 54 of 1972); grading/export standards (Act 119 of 1990, PPECB for
  perishable exports); GMOs (Act 15 of 1997); Plant Breeders' Rights.
- Constraints: water scarcity, energy cost and load-shedding, long distances to market."""
        },
    )
)

_add(
    Sector(
        key="energy",
        label="renewable energy and energy storage",
        kind="product",
        # 21 = Energy (energy engineering, renewable energy, fuel technology, nuclear)
        sweep_filter="primary_topic.field.id:21",
        triage_categories=("solar_and_wind", "batteries_and_storage", "mini_grids_and_grid_tech",
                           "bioenergy_and_clean_cooking", "efficiency_and_heating", "hydrogen_and_fuels"),
        signal_guide="a measured performance or cost improvement (cell, battery chemistry, controller, process, "
        "system design) that a household, business, farm, mine, municipality or utility would pay for",
        readiness_hint="simulation -> lab prototype -> pilot installation -> field deployment -> commercial",
        context={
            "ZA": """- Electricity is expensive and unreliable (Eskom load-shedding history, rising municipal tariffs), so
  backup power, solar PV, batteries and efficiency have real demand from households, SMEs, farms and mines.
- Buyers: homeowners and estates, commercial & industrial (C&I) sites, mines, farms, municipalities,
  independent power producers, installers and distributors (who decide what gets sold).
- Regulation to check (verify current status): Electricity Regulation Act (as amended in 2024) and NERSA
  licensing/registration thresholds for generation; municipal approval for grid-tied systems; NRS 097-2
  grid-connection rules and approved inverter lists; NRCS/SABS compulsory specifications for electrical
  products; REIPPPP/BESIPPPP procurement rounds; carbon tax and energy-efficiency (12L) incentives.
- Constraints: imported components priced in dollars, battery theft/vandalism, installer quality,
  grid-curtailment in some provinces."""
        },
    )
)

_add(
    Sector(
        key="ai",
        label="artificial intelligence and data products",
        kind="product",
        # 1702 = Artificial Intelligence (incl. NLP), 1707 = Computer Vision and Pattern Recognition
        sweep_filter="primary_topic.subfield.id:1702|1707",
        triage_categories=("african_language_ai", "computer_vision", "decision_support_and_forecasting",
                           "low_resource_and_edge_ai", "fraud_risk_and_finance_ai", "health_and_public_sector_ai"),
        signal_guide="a working model, dataset or method with measured accuracy on a real local task (language, "
        "images, forecasting, fraud) that a business or public body would pay to use, especially where "
        "local data, languages or low connectivity give an edge over global products",
        readiness_hint="benchmark/offline evaluation -> prototype app -> pilot with real users -> deployed",
        context={
            "ZA": """- 12 official languages; most commercial AI tools work poorly in isiZulu, isiXhosa, Sesotho, Setswana etc.
- Mobile-first users, expensive data, patchy connectivity: offline/edge and low-bandwidth products matter.
- Buyers: banks and insurers, retailers, telcos, mines, agribusiness, call centres, government departments,
  municipalities, health providers. Enterprise sales cycles are long; SMEs buy simple SaaS.
- Regulation to check: POPIA (Protection of Personal Information Act 4 of 2013) and the Information
  Regulator; sector rules (FSCA for financial services, health data rules); national AI policy work.
- Global incumbents (OpenAI, Google, Microsoft, AWS) are local competitors; ask what local data, language
  or distribution advantage would survive them."""
        },
        ip_note={
            "ZA": "Computer programs 'as such' are excluded from patents in SA (Patents Act 57 of 1978); value usually "
            "sits in copyright, datasets, trained models and know-how — ask the TTO who owns the data and model weights."
        },
    )
)

_add(
    Sector(
        key="construction",
        label="real estate, construction and building materials",
        kind="product",
        # 2205 Civil and Structural Engineering, 2215 Building and Construction, 3322 Urban Studies
        sweep_filter="primary_topic.subfield.id:2205|2215|3322",
        triage_categories=("low_cost_materials", "energy_efficient_buildings", "informal_and_affordable_housing",
                           "construction_methods_and_automation", "property_valuation_and_proptech"),
        signal_guide="a measured result on a material, building system, method or valuation model (strength, "
        "durability, cost, thermal performance, accuracy) that developers, contractors, housing programmes or "
        "property owners would pay for",
        readiness_hint="lab specimens -> prototype structure/pilot -> certified or code-compliant -> commercial",
        context={
            "ZA": """- Huge housing backlog, informal settlements, and expensive cement and steel; strong interest in cheaper,
  faster, thermally better walling and in fire-safe informal housing upgrades.
- Buyers: developers, contractors, social housing institutions, government housing programmes (subsidy/BNG),
  hardware retailers, property owners and managers, banks and valuers.
- Regulation to check (verify current status): National Building Regulations and Building Standards Act
  103 of 1977 and SANS 10400 (incl. XA energy efficiency); Agrement South Africa certification for
  non-standard building systems (often essential, and slow); NHBRC home enrolment; SANS cement/concrete
  standards; municipal plan approval; Property Practitioners Act 22 of 2019 for real-estate services.
- Constraints: certification time and cost, conservative specifiers, theft on site, local-content rules
  in public procurement."""
        },
    )
)

_add(
    Sector(
        key="quant_finance",
        label="quantitative finance, options and market research",
        kind="quant",
        # 2003 = Finance
        sweep_filter="primary_topic.subfield.id:2003",
        triage_categories=("volatility_and_options", "market_microstructure", "portfolio_and_risk",
                           "return_predictability_and_factors", "alternative_data"),
        signal_guide="a specific, testable signal or pricing/risk method with reported out-of-sample performance on "
        "named instruments and data that could be obtained and re-tested",
        readiness_hint="theory -> in-sample backtest -> out-of-sample test -> costs & liquidity included -> live evidence",
        context={
            "ZA": """- JSE equities and JSE equity/currency derivatives (options mainly on indices like the Top 40, liquid large
  caps and USD/ZAR); single-stock options liquidity is thin, so spreads and costs dominate small edges.
- Data for options (historical chains, implied vols) is costly; free data is mostly end-of-day equity prices.
- Regulation: FSCA; FAIS Act if you ever advise others or manage their money. Personal trading has no licence
  requirement, but every finding is a research hypothesis, not a trading signal."""
        },
    )
)


def get(key: str) -> Sector:
    try:
        return SECTORS[key]
    except KeyError:
        raise SystemExit(f"Unknown sector {key!r}. Choose from: {', '.join(SECTORS)}") from None
