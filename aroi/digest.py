"""Weekly digest: the top new opportunities, what changed in your pipeline, and what it cost."""
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

log = logging.getLogger(__name__)

TEMPLATE = """<!doctype html>
<html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>AROI digest {{ today }}</title></head>
<body style="margin:0;padding:0;background:#f4f3ef;font-family:-apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif;color:#1d1d1b;">
<div style="max-width:680px;margin:0 auto;padding:24px 16px;">
  <div style="font-size:12px;letter-spacing:.08em;text-transform:uppercase;color:#6b6b63;">AROI · {{ country_name }} · {{ sector }}</div>
  <h1 style="font-size:24px;margin:4px 0 4px;">Research-to-business digest</h1>
  <div style="color:#6b6b63;font-size:14px;margin-bottom:20px;">{{ period }}</div>

  <table role="presentation" style="width:100%;border-collapse:collapse;background:#fff;border-radius:10px;margin-bottom:20px;font-size:14px;">
    <tr>
      {% for label, value in stats %}
      <td style="padding:12px 10px;text-align:center;border-right:1px solid #eee;">
        <div style="font-size:20px;font-weight:600;">{{ value }}</div>
        <div style="color:#6b6b63;font-size:12px;">{{ label }}</div></td>
      {% endfor %}
    </tr>
  </table>

  {% if not opportunities %}
  <div style="background:#fff;border-radius:10px;padding:16px;margin-bottom:20px;">
    <strong>No new opportunity cleared the bar ({{ min_score }}+) this week.</strong>
    <p style="margin:8px 0 0;color:#444;">That is a normal outcome — the system is built to return nothing rather than
    invent something. {{ below }} scored opportunit{{ 'y' if below == 1 else 'ies' }} fell below the threshold.</p>
  </div>
  {% endif %}

  {% for o in opportunities %}
  <div style="background:#fff;border-radius:10px;padding:18px;margin-bottom:16px;border-left:4px solid {{ o.color }};">
    <div style="font-size:12px;color:#6b6b63;">#{{ loop.index }} · score {{ "%.0f"|format(o.total_score) }}/100 · evidence {{ o.evidence_grade }} · {{ o.action_label }}</div>
    <h2 style="font-size:18px;margin:6px 0 8px;">{{ o.title }}</h2>
    <p style="margin:0 0 8px;"><strong>Customer:</strong> {{ o.target_customer }}</p>
    <p style="margin:0 0 8px;"><strong>Problem:</strong> {{ o.customer_problem }}</p>
    <p style="margin:0 0 8px;"><strong>Product:</strong> {{ o.proposed_solution }}</p>
    {% if o.blockers %}<p style="margin:0 0 8px;color:#a3261d;"><strong>Blocking risks:</strong> {{ o.blockers }}</p>{% endif %}
    <p style="margin:0 0 8px;"><strong>Next action:</strong> {{ o.next_action }}</p>
    <p style="margin:0 0 8px;"><strong>Cheapest disproving test:</strong> {{ o.next_experiment }}</p>
    <p style="margin:0 0 10px;color:#444;font-size:13px;"><strong>IP:</strong> {{ o.ip_pathway|replace("\n", " · ") }}</p>
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
    <div style="font-size:11px;color:#8a8a80;margin-top:10px;">id {{ o.id }} · record your decision: <code>python -m aroi review {{ o.short_id }} promising|rejected "notes"</code></div>
  </div>
  {% endfor %}

  {% if changes %}
  <div style="background:#fff;border-radius:10px;padding:16px;margin-bottom:16px;">
    <h3 style="margin:0 0 8px;font-size:15px;">Your pipeline since last digest</h3>
    <ul style="margin:0;padding-left:18px;font-size:14px;">
    {% for c in changes %}<li><strong>{{ c.title }}</strong> → {{ c.decision }}{% if c.notes %} — {{ c.notes }}{% endif %}</li>{% endfor %}
    </ul>
  </div>
  {% endif %}

  <p style="font-size:12px;color:#8a8a80;line-height:1.5;">Scores rank what to investigate next; they are not investment advice.
  Every market source above is a page the research agent actually retrieved. Spend this period ${{ spend_period }} ·
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


def build(conn: psycopg.Connection, cfg) -> dict[str, Any]:
    last = conn.execute("select max(sent_at) as t from digests").fetchone()["t"]
    since = last or datetime(1970, 1, 1, tzinfo=timezone.utc)

    def count(sql: str) -> int:
        return conn.execute(sql, (since,)).fetchone()["n"]

    stats = [
        ("papers found", count("select count(*) n from papers where discovered_at > %s")),
        ("passed triage", count("select count(*) n from papers where updated_at > %s and status in ('queued','analyzed')")),
        ("analysed", count("select count(*) n from papers where analyzed_at > %s")),
        ("ideas generated", count("select count(*) n from opportunities where created_at > %s")),
    ]
    spend_period = float(
        conn.execute("select coalesce(sum(cost_usd),0) s from agent_runs where started_at > %s", (since,)).fetchone()["s"]
    )
    spend_month = float(
        conn.execute(
            "select coalesce(sum(cost_usd),0) s from agent_runs where started_at >= date_trunc('month', now())"
        ).fetchone()["s"]
    )

    rows = conn.execute(
        """select * from opportunities where pipeline_status='scored' and digest_sent_at is null
             and stage <> 'rejected' and total_score >= %s
           order by total_score desc, evidence_grade limit %s""",
        (cfg.digest_min_score, cfg.digest_top_n),
    ).fetchall()
    below = conn.execute(
        """select count(*) n from opportunities where pipeline_status='scored' and digest_sent_at is null
             and total_score < %s""",
        (cfg.digest_min_score,),
    ).fetchone()["n"]

    opportunities = []
    for o in rows:
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
        opportunities.append(
            {
                **o,
                "short_id": str(o["id"])[:8],
                "criteria": criteria,
                "papers": papers,
                "evidence": evidence,
                "blockers": "; ".join(b["risk"] for b in o["blocking_risks"] if b.get("severity") in ("blocker", "major")),
                "action_label": ACTION_LABELS.get(action, action),
                "color": ACTION_COLORS.get(action, "#9a9a90"),
            }
        )

    changes = conn.execute(
        """select o.title, r.decision, r.notes from reviews r join opportunities o on o.id=r.opportunity_id
            where r.reviewed_at > %s order by r.reviewed_at""",
        (since,),
    ).fetchall()

    from .prompts import COUNTRY_NAMES

    today = datetime.now(timezone.utc).date().isoformat()
    html = (
        Environment(autoescape=select_autoescape(["html"]), enable_async=False)
        .from_string(TEMPLATE)
        .render(
            today=today,
            period=f"Since {since.date().isoformat()}" if last else f"First digest · {today}",
            country_name=COUNTRY_NAMES.get(cfg.country, cfg.country),
            sector=cfg.sector,
            stats=stats,
            opportunities=opportunities,
            below=below,
            min_score=int(cfg.digest_min_score),
            changes=changes,
            spend_period=f"{spend_period:.2f}",
            spend_month=f"{spend_month:.2f}",
            budget=f"{cfg.monthly_budget_usd:.0f}",
        )
    )
    return {"html": html, "opportunity_ids": [o["id"] for o in rows], "today": today, "count": len(rows)}


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
            f"AROI: {d['count']} research opportunit{'y' if d['count'] == 1 else 'ies'} to investigate"
            if d["count"]
            else "AROI: weekly research digest (no new leads above threshold)"
        )
        try:
            emailed = send_email(cfg, subject, d["html"])
        except Exception as e:  # never lose the digest because email failed
            log.error("Email failed: %s", e)
    with conn.transaction():
        conn.execute(
            "insert into digests (opportunity_ids, html, emailed) values (%s,%s,%s)",
            (d["opportunity_ids"], d["html"], emailed),
        )
        if d["opportunity_ids"]:
            conn.execute(
                "update opportunities set digest_sent_at=now() where id = any(%s)", (d["opportunity_ids"],)
            )
    conn.commit()
    log.info("Digest written to %s (%s opportunities, emailed=%s)", path, d["count"], emailed)
    return {"path": str(path), "count": d["count"], "emailed": emailed}
