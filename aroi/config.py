"""Runtime configuration, read from environment variables (or a local .env file)."""
from __future__ import annotations

import os
from dataclasses import dataclass, field

try:  # optional convenience for local runs
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:  # pragma: no cover
    pass


def _int(name: str, default: int) -> int:
    v = os.getenv(name)
    return int(v) if v not in (None, "") else default


def _float(name: str, default: float) -> float:
    v = os.getenv(name)
    return float(v) if v not in (None, "") else default


def _bool(name: str, default: bool) -> bool:
    v = os.getenv(name)
    if v in (None, ""):
        return default
    return v.strip().lower() in ("1", "true", "yes", "on")


@dataclass
class Config:
    # --- connections -------------------------------------------------------
    database_url: str = field(default_factory=lambda: os.getenv("DATABASE_URL", ""))
    openalex_api_key: str = field(default_factory=lambda: os.getenv("OPENALEX_API_KEY", ""))
    contact_email: str = field(default_factory=lambda: os.getenv("CONTACT_EMAIL", ""))
    # Connection string the Grok Bot agents use (least-privilege role aroi_agent, see sql/roles.sql).
    # Falls back to DATABASE_URL for local runs.
    agent_database_url: str = field(
        default_factory=lambda: os.getenv("AGENT_DATABASE_URL") or os.getenv("DATABASE_URL", "")
    )

    # --- scope -------------------------------------------------------------
    country: str = field(default_factory=lambda: os.getenv("TARGET_COUNTRY", "ZA"))
    # Comma-separated sector keys from aroi/sectors.py. All five run by default.
    sectors: list[str] = field(
        default_factory=lambda: [
            x.strip() for x in (os.getenv("SECTORS") or "agriculture,energy,ai,construction,quant_finance").split(",") if x.strip()
        ]
    )
    sweep_work_types: str = field(
        default_factory=lambda: os.getenv("SWEEP_WORK_TYPES", "article|preprint|dissertation|report|book-chapter")
    )
    first_run_lookback_days: int = field(default_factory=lambda: _int("FIRST_RUN_LOOKBACK_DAYS", 90))
    sweep_overlap_days: int = field(default_factory=lambda: _int("SWEEP_OVERLAP_DAYS", 14))
    max_sweep_pages: int = field(default_factory=lambda: _int("MAX_SWEEP_PAGES", 20))  # x100 results
    keyword_results_per_query: int = field(default_factory=lambda: _int("KEYWORD_RESULTS_PER_QUERY", 50))

    # --- agent work queue (AI stages are done by Grok Bot agents) ------------
    task_lease_minutes: int = field(default_factory=lambda: _int("TASK_LEASE_MINUTES", 120))
    task_max_attempts: int = field(default_factory=lambda: _int("TASK_MAX_ATTEMPTS", 3))
    # publication_date works on the free OpenAlex plan. created_date (catches back-dated records) needs
    # Member+/Partner; if refused, discovery falls back to publication_date and reports it.
    discovery_date_field: str = field(default_factory=lambda: os.getenv("DISCOVERY_DATE_FIELD") or "publication_date")

    # --- throughput & thresholds ----------------------------------------------
    triage_batch_size: int = field(default_factory=lambda: _int("TRIAGE_BATCH_SIZE", 15))
    triage_max_per_sector: int = field(default_factory=lambda: _int("TRIAGE_MAX_PER_SECTOR", 60))
    triage_threshold: int = field(default_factory=lambda: _int("TRIAGE_THRESHOLD", 6))  # 0-10
    analyze_per_sector: int = field(default_factory=lambda: _int("ANALYZE_PER_SECTOR", 5))
    use_full_text: bool = field(default_factory=lambda: _bool("USE_FULL_TEXT", True))
    full_text_max_chars: int = field(default_factory=lambda: _int("FULL_TEXT_MAX_CHARS", 45000))
    min_evidence_for_opportunities: float = field(default_factory=lambda: _float("MIN_EVIDENCE_FOR_OPPS", 0.35))
    validate_per_sector: int = field(default_factory=lambda: _int("VALIDATE_PER_SECTOR", 2))
    min_quote_chars: int = field(default_factory=lambda: _int("MIN_QUOTE_CHARS", 30))
    score_per_run: int = field(default_factory=lambda: _int("SCORE_PER_RUN", 10))
    merge_similarity: float = field(default_factory=lambda: _float("MERGE_SIMILARITY", 0.55))

    # --- budget guard (USD): OpenAlex is the only metered API left -------------
    monthly_budget_usd: float = field(default_factory=lambda: _float("MONTHLY_BUDGET_USD", 30.0))
    heartbeat_max_hours: float = field(default_factory=lambda: _float("HEARTBEAT_MAX_HOURS", 36.0))

    # --- digest ---------------------------------------------------------------
    digest_min_score: float = field(default_factory=lambda: _float("DIGEST_MIN_SCORE", 55.0))
    digest_top_n_per_sector: int = field(default_factory=lambda: _int("DIGEST_TOP_N_PER_SECTOR", 3))
    digest_to: str = field(default_factory=lambda: os.getenv("DIGEST_TO", ""))
    smtp_host: str = field(default_factory=lambda: os.getenv("SMTP_HOST", "smtp.gmail.com"))
    smtp_port: int = field(default_factory=lambda: _int("SMTP_PORT", 465))
    smtp_user: str = field(default_factory=lambda: os.getenv("SMTP_USER", ""))
    smtp_password: str = field(default_factory=lambda: os.getenv("SMTP_PASSWORD", ""))
    output_dir: str = field(default_factory=lambda: os.getenv("OUTPUT_DIR", "out"))

    def require(self, *names: str) -> None:
        missing = [n for n in names if not getattr(self, n)]
        if missing:
            env = ", ".join(n.upper() for n in missing)
            raise SystemExit(f"Missing configuration: {env}. Set it in .env or as a GitHub secret.")
