"""Weekly digest: the top new leads per sector, quant hypotheses to test, what changed in your
pipeline, and what it cost."""
from __future__ import annotations

import logging
import smtplib
import ssl
from datetime import datetime, timezone
from email.message import EmailMessage
from pathlib import Path
from typing import Any

import psycopg
from jinja2 import Environment, select_autoescape

from . import sectors

log = logging.getLogger(__name__)

TEMPLATE = """<!doctype html>
<html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>AROI digest {{ today }}</title></head>
<body style="margin:0;padding:0;background:#f4f3ef;font-family:-apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif;color:#1d1d1b;">
<div style="max-width:680px;margin:0 auto;padding:24px 16px;">
  <div style="font-size:12px;letter-spacing:.08em;text-transform:uppercase;color:#6b6b63;">AROI · {{ country_name }} · {{ sectors|length }} sectors</div>
  <h1 style="font-size:24px;margin:4px 0 4px;">Research-to-business digest</h1>
  <div style="color:#6b6b63;font-size:14px;margin-bottom:20px;">{{ period }} · {{ total_leads }} new lead{{ '' if total_leads == 1 else 's' }}</div>

  {% if health.warnings %}
  <div style="background:#fdecea;border-left:4px solid #a3261d;border-radius:6px;padding:12px 14px;margin-bottom:16px;font-size:14px;">
    <strong>Pipeline health:</strong>
    <ul style="margin:6px 0 0 18px;padding:0;">{% for w in health.warnings %}<li>{{ w }}</li>{% endfor %}</ul>
  </div>
  {% endif %}

  <table role="presentation" style="width:100%;border-collapse:collapse;background:#fff;border-radius:10px;margin-bottom:12px;font-size:13px;">
    <tr style="color:#6b6b63;text-align:left;"><th style="padding:10px;font-weight:500;" colspan="8">Funnel this period</th></tr>
    <tr style="color:#6b6b63;"><td style="padding:4px 10px;">Sector</td>{% for label in funnel_labels %}<td style="padding:4px 6px;text-align:right;">{{ label }}</td>{% endfor %}</tr>
    {% for s in sectors %}
    <tr style="border-top:1px solid #eee;"><td style="padding:6px 10px;">{{ s.name }}</td>{% for k in funnel_keys %}<td style="padding:6px 6px;text-align:right;">{{ s.funnel[k] }}</td>{% endfor %}</tr>
    {% endfor %}
  </table>
  <div style="font-size:12px;color:#6b6b63;margin-bottom:24px;">Last pipeline run: {{ health.last_run or 'never' }}{% if health.last_ok is not none %} ({{ 'ok' if health.last_ok else 'had errors' }}){% endif %} ·
    agent queue: {{ health.pending }} pending, {{ health.claimed }} in progress, {{ health.failed_period }} failed this period{% if health.stale %}, {{ health.stale }} stale{% endif %}</div>

  <table role="presentation" style="width:100%;border-collapse:collapse;background:#fff;border-radius:10px;margin-bottom:24px;font-size:13px;">
    <tr style="color:#6b6b63;text-align:left;">
      <th style="padding:10px;font-weight:500;">Sector</th><th style="padding:10px 6px;font-weight:500;text-align:right;">Found</th>
      <th style="padding:10px 6px;font-weight:500;text-align:right;">Analysed</th><th style="padding:10px 6px;font-weight:500;text-align:right;">Ideas</th>
      <th style="padding:10px;font-weight:500;text-align:right;">Leads</th></tr>
    {% for s in sectors %}
    <tr style="border-top:1px solid #eee;"><td style="padding:8px 10px;">{{ s.name }}</td>
      <td style="padding:8px 6px;text-align:right;">{{ s.found }}</td><td style="padding:8px 6px;text-align:right;">{{ s.analysed }}</td>
      <td style="padding:8px 6px;text-align:right;">{{ s.ideas }}</td><td style="padding:8px 10px;text-align:right;font-weight:600;">{{ s.leads|length }}</td></tr>
    {% endfor %}
  </table>

  {% for s in sectors if s.kind == 'product' %}
  <h2 style="font-size:13px;letter-spacing:.06em;text-transform:uppercase;color:#6b6b63;margin:24px 0 10px;">{{ s.name }}</h2>
  {% if not s.leads %}
  <div style="background:#fff;border-radius:10px;padding:14px 16px;margin-bottom:12px;font-size:14px;color:#444;">
    No new idea cleared {{ min_score }} this week{% if s.below %} ({{ s.below }} scored below){% endif %}. Returning nothing beats inventing something.</div>
  {% endif %}
  {% for o in s.leads %}
  <div style="background:#fff;border-radius:10px;padding:18px;margin-bottom:16px;border-left:4px solid {{ o.color }};">
    <div style="font-size:12px;color:#6b6b63;">score {{ "%.0f"|format(o.total_score) }}/100 · evidence {{ o.evidence_grade }} · {{ o.action_label }}</div>
    <h3 style="font-size:18px;margin:6px 0 8px;">{{ o.title }}</h3>
    <p style="margin:0 0 8px;"><strong>Customer:</strong> {{ o.target_customer }}</p>
    <p style="margin:0 0 8px;"><strong>Problem:</strong> {{ o.customer_problem }}</p>
    <p style="margin:0 0 8px;"><strong>Product:</strong> {{ o.proposed_solution }}</p>
    {% if o.blockers %}<p style="margin:0 0 8px;color:#a3261d;"><strong>Blocking risks:</strong> {{ o.blockers }}</p>{% endif %}
    <p style="margin:0 0 8px;"><strong>Next action:</strong> {{ o.next_action }}</p>
    <p style="margin:0 0 8px;"><strong>Cheapest disproving test:</strong> {{ o.next_experiment }}</p>
    <p style="margin:0 0 10px;color:#444;font-size:13px;"><strong>IP:</strong> {{ o.ip_pathway|replace("\\n", " · ") }}</p>
    <table role="presentation" style="width:100%;border-collapse:collapse;font-size:12px;margin-bottom:10px;">
      {% for c in o.criteria %}
      <tr><td style="padding:2px 0;color:#444;width:62%;">{{ c.label }} ({{ c.weight }}%)</td>
          <td style="padding:2px 0;">{{ '●' * c.score }}{{ '○' * (5 - c.score) }}</td></tr>
      {% endfor %}
    </table>
    <div style="font-size:13px;"><strong>Research:</strong>
      <ul style="margin:4px 0 6px;padding-left:18px;">
      {% for p in o.papers %}<li><a href="{{ p.url }}" style="color:#1f5c99;">{{ p.title }}</a> — {{ p.institutions or 'institution unknown' }} ({{ p.publication_date }})</li>{% endfor %}
      </ul>
      {% if o.evidence %}<strong>Market sources:</strong>
      <ul style="margin:4px 0 0;padding-left:18px;">
      {% for e in o.evidence %}<li><a href="{{ e.source_url }}" style="color:#1f5c99;">{{ e.source_title or e.source_url }}</a> — {{ e.claim_supported }}</li>{% endfor %}
      </ul>{% endif %}
    </div>
    <div style="font-size:11px;color:#8a8a80;margin-top:10px;">id <strong>{{ o.short_id }}</strong> · record your decision with the "Record a decision" workflow</div>
  </div>
  {% endfor %}
  {% endfor %}

  {% if quant_enabled %}
  <h2 style="font-size:13px;letter-spacing:.06em;text-transform:uppercase;color:#6b6b63;margin:28px 0 4px;">Quant finance — hypotheses to test</h2>
  <p style="font-size:12px;color:#8a8a80;margin:0 0 10px;">Research leads only, ranked by how testable and well-reported they are — not by profit, and not trading signals.
  Nothing here should be traded before an out-of-sample backtest with realistic costs and a paper-trading period.</p>
  {% if not finance %}<div style="background:#fff;border-radius:10px;padding:14px 16px;margin-bottom:12px;font-size:14px;color:#444;">No new hypothesis scored {{ finance_min }}+ for testability this week.</div>{% endif %}
  {% for h in finance %}
  <div style="background:#fff;border-radius:10px;padding:16px;margin-bottom:14px;border-left:4px solid {{ '#2e7d4f' if h.recommended_action == 'backtest_candidate' else '#c58a12' }};">
    <div style="font-size:12px;color:#6b6b63;">testability {{ "%.0f"|format(h.testability_score) }}/100 · {{ h.recommended_action|replace('_', ' ') }} · replication {{ h.replication_difficulty }}</div>
    <p style="margin:6px 0 8px;font-weight:600;">{{ h.hypothesis }}</p>
    <p style="margin:0 0 6px;font-size:13px;"><strong>Instruments:</strong> {{ h.instruments_and_market }} · <strong>Period:</strong> {{ h.data_period }}</p>
    <p style="margin:0 0 6px;font-size:13px;"><strong>Reported:</strong> {{ h.reported_performance }}</p>
    <p style="margin:0 0 6px;font-size:13px;">{{ '✓' if h.out_of_sample_tested else '✗' }} out-of-sample · {{ '✓' if h.transaction_costs_modelled else '✗' }} costs modelled · {{ '✓' if h.liquidity_considered else '✗' }} liquidity</p>
    {% if h.red_flags %}<p style="margin:0 0 6px;font-size:13px;color:#a3261d;"><strong>Red flags:</strong> {{ h.red_flags|join('; ') }}</p>{% endif %}
    <p style="margin:0 0 6px;font-size:13px;"><strong>Data needed:</strong> {{ h.data_needed|join('; ') }}</p>
    <p style="margin:0;font-size:13px;"><a href="{{ h.url }}" style="color:#1f5c99;">{{ h.title }}</a> — {{ h.institutions or 'institution unknown' }}</p>
    <div style="font-size:11px;color:#8a8a80;margin-top:8px;">id <strong>{{ h.short_id }}</strong></div>
  </div>
  {% endfor %}
  {% endif %}

  {% if changes %}
  <div style="background:#fff;border-radius:10px;padding:16px;margin:20px 0 16px;">
    <h3 style="margin:0 0 8px;font-size:15px;">Your pipeline since last digest</h3>
    <ul style="margin:0;padding-left:18px;font-size:14px;">
    {% for c in changes %}<li><strong>{{ c.title }}</strong> → {{ c.decision }}{% if c.notes %} — {{ c.notes }}{% endif %}</li>{% endfor %}
    </ul>
  </div>
  {% endif %}

  <p style="font-size:12px;color:#8a8a80;line-height:1.5;">Scores rank what to investigate next; they are not investment advice.
  Every market source above is a page the research agent reported opening, tied to the claim it supports. OpenAlex spend this period ${{ spend_period }} ·
  month to date ${{ spend_month }} of ${{ budget }}.</p>
</div></body></html>"""

ACTION_LABELS = {
    "prioritize_validation": "prioritise validation",
    "investigate_uncertainties": "investigate uncertainties",
    "deprioritize": "deprioritise",
    "resolve_blocker_first": "resolve blocker first",
}
ACTION_COLORS = {
    "prioritize_validation": "#2e7d4f",
    "investigate_uncertainties": "#c58a12",
    "resolve_blocker_first": "#a3261d",
    "deprioritize": "#9a9a90",
}


FINANCE_MIN_TESTABILITY = 40


def _opportunity_card(conn, o) -> dict[str, Any]:
    sc = conn.execute(
        "select criteria_json from opportunity_scores where opportunity_id=%s order by scored_at desc limit 1",
        (o["id"],),
    ).fetchone()
    criteria = sorted(
        (v for k, v in (sc["criteria_json"] if sc else {}).items() if not k.startswith("_")),
        key=lambda c: -c["weight"],
    )
    papers = conn.execute(
        """select p.title, p.publication_date, coalesce('https://doi.org/' || p.doi, p.landing_url) as url,
                  (select string_agg(distinct i.name, '; ') from paper_institutions pi
                     join institutions i on i.id=pi.institution_id where pi.paper_id=p.id) as institutions
             from opportunity_papers op join papers p on p.id=op.paper_id where op.opportunity_id=%s
            order by op.relationship desc""",
        (o["id"],),
    ).fetchall()
    evidence = conn.execute(
        """select distinct on (source_url) source_url, source_title, claim_supported from evidence_items
            where opportunity_id=%s and evidence_type <> 'web_citation' order by source_url limit 6""",
        (o["id"],),
    ).fetchall()
    action = o["recommended_action"] or "investigate_uncertainties"
    return {
        **o,
        "short_id": str(o["id"])[:8],
        "criteria": criteria,
        "papers": papers,
        "evidence": evidence,
        "blockers": "; ".join(b["risk"] for b in o["blocking_risks"] if b.get("severity") in ("blocker", "major")),
        "action_label": ACTION_LABELS.get(action, action),
        "color": ACTION_COLORS.get(action, "#9a9a90"),
    }


FUNNEL_KEYS = ["found", "passed_triage", "analysed", "verified_quotes", "ideas", "validated", "scored"]
FUNNEL_LABELS = ["Found", "Passed triage", "Analysed", "Verified quotes", "Ideas", "Validated", "Scored"]


def _health(conn, cfg, since) -> dict[str, Any]:
    """Failure heartbeat: a silent pipeline should be loud in the digest."""
    last = conn.execute(
        "select started_at, finished_at, ok, command from pipeline_runs order by started_at desc limit 1"
    ).fetchone()
    errs = conn.execute(
        "select count(*) n from pipeline_runs where started_at > %s and ok is false", (since,)
    ).fetchone()["n"]
    q = conn.execute(
        """select count(*) filter (where status='pending') pending,
                  count(*) filter (where status='claimed') claimed,
                  count(*) filter (where status='claimed' and lease_expires_at < now()) stale,
                  count(*) filter (where status='failed' and created_at > %s) failed_period,
                  min(created_at) filter (where status='pending') oldest_pending
             from agent_tasks""",
        (since,),
    ).fetchone()
    now = datetime.now(timezone.utc)
    warnings = []
    if not last:
        warnings.append("The pipeline has never recorded a run.")
    else:
        age_h = (now - last["started_at"]).total_seconds() / 3600
        if age_h > cfg.heartbeat_max_hours:
            warnings.append(f"No pipeline run for {age_h:.0f} hours (last: {last['command']} at "
                            f"{last['started_at']:%Y-%m-%d %H:%M} UTC). Check the GitHub Actions schedule.")
        if last["ok"] is False:
            warnings.append(f"The last run ({last['command']}) had errors; see pipeline_runs.results.")
        if last["finished_at"] is None and age_h > 2:
            warnings.append("The last run never finished (crashed or timed out).")
    disc = conn.execute(
        """select results->'discover' d from pipeline_runs where results ? 'discover'
            order by started_at desc limit 1"""
    ).fetchone()
    if disc and disc["d"]:
        for p in (disc["d"].get("fatal") or []) + (disc["d"].get("problems") or []):
            warnings.append(f"Discovery: {p}")
    if errs:
        warnings.append(f"{errs} pipeline run(s) with errors this period.")
    if q["failed_period"]:
        warnings.append(f"{q['failed_period']} agent task(s) failed after all retries this period.")
    if q["stale"]:
        warnings.append(f"{q['stale']} agent task(s) claimed but not submitted before the lease ran out.")
    if q["oldest_pending"] and (now - q["oldest_pending"]).total_seconds() > cfg.heartbeat_max_hours * 3600:
        warnings.append(f"Agent tasks have been waiting since {q['oldest_pending']:%Y-%m-%d %H:%M} UTC: "
                        "are the bots running?")
    return {
        "last_run": f"{last['command']} at {last['started_at']:%Y-%m-%d %H:%M} UTC" if last else None,
        "last_ok": last["ok"] if last else None,
        "pending": q["pending"], "claimed": q["claimed"], "stale": q["stale"],
        "failed_period": q["failed_period"], "errored_runs": errs, "warnings": warnings,
    }


def build(conn: psycopg.Connection, cfg) -> dict[str, Any]:
    last = conn.execute("select max(sent_at) as t from digests").fetchone()["t"]
    since = last or datetime(1970, 1, 1, tzinfo=timezone.utc)

    sector_rows: list[dict[str, Any]] = []
    opp_ids: list[Any] = []
    for key in cfg.sectors:
        sec = sectors.get(key)
        counts = conn.execute(
            """select (select count(*) from papers where sector=%(k)s and discovered_at > %(t)s) as found,
                      (select count(*) from papers where sector=%(k)s and analyzed_at > %(t)s) as analysed,
                      (select count(*) from opportunities where sector=%(k)s and created_at > %(t)s) as ideas""",
            {"k": key, "t": since},
        ).fetchone()
        leads: list[dict[str, Any]] = []
        below = 0
        if sec.kind == "product":
            rows = conn.execute(
                """select * from opportunities where sector=%s and pipeline_status='scored' and digest_sent_at is null
                     and stage <> 'rejected' and total_score >= %s
                   order by total_score desc limit %s""",
                (key, cfg.digest_min_score, cfg.digest_top_n_per_sector),
            ).fetchall()
            leads = [_opportunity_card(conn, o) for o in rows]
            opp_ids += [o["id"] for o in rows]
            below = conn.execute(
                """select count(*) n from opportunities where sector=%s and pipeline_status='scored'
                     and digest_sent_at is null and total_score < %s""",
                (key, cfg.digest_min_score),
            ).fetchone()["n"]
        f = conn.execute(
            """select
                 (select count(*) from papers where %(k)s = any(sectors) and discovered_at > %(t)s) as found,
                 (select count(*) from papers where sector=%(k)s and discovered_at > %(t)s
                     and status in ('queued','analyzing','analyzed')) as passed_triage,
                 (select count(*) from papers where sector=%(k)s and analyzed_at > %(t)s) as analysed,
                 (select count(*) from findings f join papers p on p.id=f.paper_id
                     where p.sector=%(k)s and f.created_at > %(t)s and f.quote_verified) as verified_quotes,
                 (select count(*) from opportunities where sector=%(k)s and created_at > %(t)s) as ideas,
                 (select count(*) from market_validations m join opportunities o on o.id=m.opportunity_id
                     where o.sector=%(k)s and m.created_at > %(t)s) as validated,
                 (select count(*) from opportunity_scores s join opportunities o on o.id=s.opportunity_id
                     where o.sector=%(k)s and s.scored_at > %(t)s) as scored""",
            {"k": key, "t": since},
        ).fetchone()
        sector_rows.append({"key": key, "funnel": dict(f), "kind": sec.kind, "name": sec.label[:1].upper() + sec.label[1:],
                            "leads": leads, "below": below, **counts})

    quant_enabled = any(sectors.get(k).kind == "quant" for k in cfg.sectors)
    finance = []
    if quant_enabled:
        finance = conn.execute(
            """select h.*, p.title, coalesce('https://doi.org/' || p.doi, p.landing_url) as url,
                      (select string_agg(distinct i.name, '; ') from paper_institutions pi
                         join institutions i on i.id=pi.institution_id where pi.paper_id=p.id) as institutions
                 from finance_hypotheses h join papers p on p.id = h.paper_id
                where h.digest_sent_at is null and h.stage <> 'rejected' and h.testability_score >= %s
                order by h.testability_score desc limit %s""",
            (FINANCE_MIN_TESTABILITY, cfg.digest_top_n_per_sector),
        ).fetchall()
        finance = [{**h, "short_id": str(h["id"])[:8]} for h in finance]
        for row in sector_rows:
            if row["kind"] == "quant":
                row["leads"] = finance

    spend_period = float(
        conn.execute("select coalesce(sum(cost_usd),0) s from agent_runs where started_at > %s", (since,)).fetchone()["s"]
    )
    spend_month = float(
        conn.execute(
            "select coalesce(sum(cost_usd),0) s from agent_runs where started_at >= date_trunc('month', now())"
        ).fetchone()["s"]
    )
    health = _health(conn, cfg, since)
    changes = conn.execute(
        """select coalesce(o.title, h.hypothesis) as title, r.decision, r.notes from reviews r
             left join opportunities o on o.id = r.opportunity_id
             left join finance_hypotheses h on h.id = r.finance_hypothesis_id
            where r.reviewed_at > %s order by r.reviewed_at""",
        (since,),
    ).fetchall()

    from .prompts import COUNTRY_NAMES

    today = datetime.now(timezone.utc).date().isoformat()
    total = len(opp_ids) + len(finance)
    html = (
        Environment(autoescape=select_autoescape(["html"]))
        .from_string(TEMPLATE)
        .render(
            today=today,
            period=f"Since {since.date().isoformat()}" if last else f"First digest · {today}",
            country_name=COUNTRY_NAMES.get(cfg.country, cfg.country),
            sectors=sector_rows,
            total_leads=total,
            min_score=int(cfg.digest_min_score),
            quant_enabled=quant_enabled,
            finance=finance,
            finance_min=FINANCE_MIN_TESTABILITY,
            changes=changes,
            spend_period=f"{spend_period:.2f}",
            spend_month=f"{spend_month:.2f}",
            budget=f"{cfg.monthly_budget_usd:.0f}",
            health=health,
            funnel_keys=FUNNEL_KEYS,
            funnel_labels=FUNNEL_LABELS,
        )
    )
    return {
        "html": html,
        "opportunity_ids": opp_ids,
        "finance_ids": [h["id"] for h in finance],
        "today": today,
        "count": total,
        "by_sector": {r["key"]: len(r["leads"]) for r in sector_rows},
        "funnel": {r["key"]: r["funnel"] for r in sector_rows},
        "health": health,
    }


def send_email(cfg, subject: str, html: str) -> bool:
    if not (cfg.smtp_user and cfg.smtp_password and cfg.digest_to):
        log.info("SMTP not configured; digest saved to file only.")
        return False
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = cfg.smtp_user
    msg["To"] = cfg.digest_to
    msg.set_content("Your AROI digest is attached as HTML. Open it in an email client that shows HTML.")
    msg.add_alternative(html, subtype="html")
    with smtplib.SMTP_SSL(cfg.smtp_host, cfg.smtp_port, context=ssl.create_default_context()) as s:
        s.login(cfg.smtp_user, cfg.smtp_password)
        s.send_message(msg)
    return True


def run(conn: psycopg.Connection, cfg, send: bool = True) -> dict[str, Any]:
    d = build(conn, cfg)
    out_dir = Path(cfg.output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"digest-{datetime.now(timezone.utc):%Y-%m-%d-%H%M%S}.html"
    path.write_text(d["html"], encoding="utf-8")
    emailed = False
    if send:
        subject = (
            f"AROI: {d['count']} new research lead{'' if d['count'] == 1 else 's'} to investigate"
            if d["count"]
            else "AROI: weekly research digest (no new leads above threshold)"
        )
        try:
            emailed = send_email(cfg, subject, d["html"])
        except Exception as e:  # never lose the digest because email failed
            log.error("Email failed: %s", e)
    with conn.transaction():
        conn.execute(
            "insert into digests (opportunity_ids, finance_hypothesis_ids, html, emailed) values (%s,%s,%s,%s)",
            (d["opportunity_ids"], d["finance_ids"], d["html"], emailed),
        )
        if d["finance_ids"]:
            conn.execute(
                "update finance_hypotheses set digest_sent_at=now() where id = any(%s)", (d["finance_ids"],)
            )
        if d["opportunity_ids"]:
            conn.execute(
                "update opportunities set digest_sent_at=now() where id = any(%s)", (d["opportunity_ids"],)
            )
    conn.commit()
    log.info("Digest written to %s (%s opportunities, emailed=%s)", path, d["count"], emailed)
    return {"path": str(path), "count": d["count"], "by_sector": d["by_sector"], "emailed": emailed,
            "funnel": d["funnel"], "health": d["health"]}
