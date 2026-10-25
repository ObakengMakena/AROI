"""Work queue between the deterministic pipeline (GitHub Actions) and the Grok Bot agents that do the
AI stages (triage, analyze, generate, validate, score).

  pipeline:  enqueue  -> agent_tasks rows (status 'pending') with instructions, input and output_schema
  bot:       pull     -> claims tasks (lease, attempts += 1) and prints them as JSON
  bot:       submit   -> result is validated against the task's strict JSON schema plus stage checks;
                         invalid results are rejected with clear errors, valid ones become 'submitted'
  pipeline:  ingest   -> the stage's existing downstream code stores the result ('done'); an ingest
                         error sends the task back to 'pending' until max_attempts, then 'failed'

CLI (bots use AGENT_DATABASE_URL, the least-privilege aroi_agent role):
  python -m aroi.agent_queue pull   --stage triage --limit 3 [--worker NAME] [--out tasks.json]
  python -m aroi.agent_queue submit --task-id ID --file result.json [--worker NAME]
  python -m aroi.agent_queue release --task-id ID [--reason TEXT]
  python -m aroi.agent_queue stats
  python -m aroi.agent_queue schema --stage validate        # print an example task's output schema
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import socket
import sys
from typing import Any

import psycopg
from jsonschema import Draft202012Validator
from psycopg.types.json import Jsonb

from . import db
from .config import Config

log = logging.getLogger(__name__)

AI_STAGES = ["triage", "analyze", "generate", "validate", "score"]
ACTIVE = ("pending", "claimed", "submitted")


class ResultRejected(ValueError):
    def __init__(self, errors: list[str]):
        super().__init__("; ".join(errors))
        self.errors = errors


def stage_module(stage: str):
    from .stages import analyze, opportunities, score, triage, validate

    mods = {"triage": triage, "analyze": analyze, "generate": opportunities, "validate": validate, "score": score}
    if stage not in mods:
        raise ValueError(f"unknown stage {stage!r}; expected one of {AI_STAGES}")
    return mods[stage]


# ---------------------------------------------------------------------------
# pipeline side
# ---------------------------------------------------------------------------
def enqueue(conn, cfg, stage: str, subject_key: str, subject_ids: list, payload: dict[str, Any],
            sector: str | None = None, priority: float = 0) -> str | None:
    """Insert a pending task unless one is already active for this subject. Returns the id or None."""
    row = conn.execute(
        """insert into agent_tasks (stage, sector, subject_key, subject_ids, payload, priority, max_attempts)
           values (%s,%s,%s,%s,%s,%s,%s)
           on conflict (stage, subject_key) where status in ('pending','claimed','submitted') do nothing
           returning id""",
        (stage, sector, subject_key, list(subject_ids), Jsonb(payload), priority, cfg.task_max_attempts),
    ).fetchone()
    return str(row["id"]) if row else None


def has_active(conn, stage: str, subject_key: str) -> bool:
    return bool(conn.execute(
        "select 1 from agent_tasks where stage=%s and subject_key=%s and status = any(%s)",
        (stage, subject_key, list(ACTIVE)),
    ).fetchone())


def ingest(conn, cfg, stages: list[str] | None = None) -> dict[str, Any]:
    """Store every submitted result with the stage's downstream code, then settle given-up tasks."""
    stats: dict[str, Any] = {}
    for stage in stages or AI_STAGES:
        mod = stage_module(stage)
        st = {"done": 0, "retry": 0, "failed": 0}
        tasks = conn.execute(
            """select * from agent_tasks where stage=%s and status='submitted'
                order by submitted_at for update skip locked""",
            (stage,),
        ).fetchall()
        for t in tasks:
            try:
                with conn.transaction():
                    # re-check: the agent role could have flipped status to 'submitted' by hand
                    errs = validate_result(t, t["result"])
                    if errs:
                        raise ResultRejected(errs)
                    r = mod.ingest(conn, cfg, t)
                    conn.execute(
                        "update agent_tasks set status='done', completed_at=now(), last_error=null where id=%s",
                        (t["id"],),
                    )
                st["done"] += 1
                for k, v in (r or {}).items():
                    if isinstance(v, (int, float)):
                        st[k] = st.get(k, 0) + v
                    elif isinstance(v, dict):
                        d = st.setdefault(k, {})
                        for kk, vv in v.items():
                            d[kk] = d.get(kk, 0) + vv
            except Exception as e:
                log.warning("Ingest of %s task %s failed: %s", stage, t["id"], e)
                err = f"ingest: {e}"[:1000]
                final = t["attempts"] >= t["max_attempts"]
                conn.execute(
                    """update agent_tasks set status=%s, last_error=%s, result=null,
                         error_log = error_log || %s, completed_at = case when %s then null else completed_at end
                        where id=%s""",
                    ("failed" if final else "pending", err, Jsonb([{"attempt": t["attempts"], "error": err}]),
                     final, t["id"]),
                )
                st["failed" if final else "retry"] += 1
            conn.commit()
        stats[stage] = st
    stats["given_up"] = settle_failures(conn)
    return stats


def settle_failures(conn) -> int:
    """Tasks that ran out of attempts (expired leases or bad ingests) get the stage's give_up hook once,
    so their papers/opportunities don't sit in 'triaging'/'analyzing' forever."""
    expire_leases(conn)
    rows = conn.execute(
        "select * from agent_tasks where status='failed' and completed_at is null for update skip locked"
    ).fetchall()
    for t in rows:
        with conn.transaction():
            stage_module(t["stage"]).give_up(conn, t)
            conn.execute("update agent_tasks set completed_at=now() where id=%s", (t["id"],))
    conn.commit()
    return len(rows)


# ---------------------------------------------------------------------------
# bot side
# ---------------------------------------------------------------------------
def expire_leases(conn) -> int:
    rows = conn.execute(
        """update agent_tasks set
             status = case when attempts >= max_attempts then 'failed' else 'pending' end,
             last_error = 'lease expired before a valid result was submitted',
             error_log = error_log || jsonb_build_array(jsonb_build_object(
                 'attempt', attempts, 'error', 'lease expired', 'worker', claimed_by)),
             claimed_by = null, claimed_at = null, lease_expires_at = null
           where status='claimed' and lease_expires_at < now() returning id""",
    ).fetchall()
    return len(rows)


def pull(conn, stage: str, limit: int, worker: str, lease_minutes: int) -> list[dict[str, Any]]:
    stage_module(stage)  # validates the name
    expire_leases(conn)
    rows = conn.execute(
        """with c as (
             select id from agent_tasks where stage=%s and status='pending' and attempts < max_attempts
              order by priority desc, created_at limit %s for update skip locked)
           update agent_tasks t set status='claimed', claimed_by=%s, claimed_at=now(),
                  lease_expires_at = now() + make_interval(mins => %s), attempts = t.attempts + 1
             from c where t.id = c.id
           returning t.*""",
        (stage, limit, worker, lease_minutes),
    ).fetchall()
    conn.commit()
    out = []
    for t in sorted(rows, key=lambda r: (-float(r["priority"]), r["created_at"])):
        p = t["payload"]
        out.append({
            "task_id": str(t["id"]),
            "stage": t["stage"],
            "sector": t["sector"],
            "attempt": t["attempts"],
            "max_attempts": t["max_attempts"],
            "lease_expires_at": t["lease_expires_at"].isoformat(),
            "previous_errors": [e.get("error") for e in t["error_log"]][-3:],
            "prompt_version": p.get("prompt_version"),
            "instructions": p["instructions"],
            "input": p["input"],
            "output_schema": p["output_schema"],
        })
    return out


def validate_result(task: dict[str, Any], result: Any) -> list[str]:
    errors = []
    v = Draft202012Validator(task["payload"]["output_schema"])
    for e in sorted(v.iter_errors(result), key=lambda e: list(e.absolute_path)):
        path = "/".join(str(x) for x in e.absolute_path) or "(root)"
        errors.append(f"{path}: {e.message}")
    if errors:
        return errors[:25]
    return stage_module(task["stage"]).check(task["payload"], result)


def submit(conn, task_id: str, result: Any, worker: str | None = None) -> dict[str, Any]:
    t = conn.execute("select * from agent_tasks where id=%s for update", (task_id,)).fetchone()
    if not t:
        raise ResultRejected([f"no task with id {task_id}"])
    if t["status"] != "claimed":
        raise ResultRejected([f"task is '{t['status']}', not 'claimed' - pull it first (leases expire)"])
    if worker and t["claimed_by"] and worker != t["claimed_by"]:
        raise ResultRejected([f"task is claimed by {t['claimed_by']!r}, not {worker!r}"])
    errors = validate_result(t, result)
    if errors:
        conn.execute(
            "update agent_tasks set last_error=%s, error_log = error_log || %s where id=%s",
            ("rejected: " + "; ".join(errors)[:900], Jsonb([{"attempt": t["attempts"], "error": "rejected",
                                                           "details": errors[:10]}]), task_id),
        )
        conn.commit()
        raise ResultRejected(errors)
    conn.execute(
        """update agent_tasks set status='submitted', result=%s, submitted_at=now(), last_error=null,
             lease_expires_at=null where id=%s""",
        (Jsonb(result), task_id),
    )
    conn.commit()
    return {"task_id": task_id, "status": "submitted"}


def release(conn, task_id: str, reason: str | None) -> None:
    """Give a claimed task back without using up an attempt (e.g. the bot ran out of time)."""
    conn.execute(
        """update agent_tasks set status='pending', claimed_by=null, claimed_at=null, lease_expires_at=null,
             attempts = greatest(attempts - 1, 0), last_error=%s where id=%s and status='claimed'""",
        (f"released: {reason or 'no reason given'}", task_id),
    )
    conn.commit()


def stats(conn) -> dict[str, Any]:
    rows = conn.execute(
        """select stage, status, count(*) n,
                  count(*) filter (where status='claimed' and lease_expires_at < now()) stale,
                  min(created_at) filter (where status='pending') oldest_pending
             from agent_tasks group by 1, 2 order by 1, 2"""
    ).fetchall()
    out: dict[str, Any] = {}
    for r in rows:
        s = out.setdefault(r["stage"], {})
        s[r["status"]] = r["n"]
        if r["stale"]:
            s["stale"] = s.get("stale", 0) + r["stale"]
        if r["oldest_pending"]:
            s["oldest_pending"] = r["oldest_pending"].isoformat()
    return out


# ---------------------------------------------------------------------------
def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")
    ap = argparse.ArgumentParser(prog="python -m aroi.agent_queue", description="AROI agent work queue")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("pull", help="claim pending tasks and print them as JSON")
    p.add_argument("--stage", required=True, choices=AI_STAGES)
    p.add_argument("--limit", type=int, default=1)
    p.add_argument("--worker", default=os.getenv("AROI_WORKER") or f"grok-bot@{socket.gethostname()}")
    p.add_argument("--out", help="also write the tasks to this file")
    s = sub.add_parser("submit", help="validate and submit a result")
    s.add_argument("--task-id", required=True)
    s.add_argument("--file", required=True, help="JSON file with the result object ('-' for stdin)")
    s.add_argument("--worker", default=os.getenv("AROI_WORKER"))
    r = sub.add_parser("release", help="hand a claimed task back without using an attempt")
    r.add_argument("--task-id", required=True)
    r.add_argument("--reason")
    sub.add_parser("stats", help="queue counts per stage and status")
    sc = sub.add_parser("schema", help="print the output schema of the newest task for a stage")
    sc.add_argument("--stage", required=True, choices=AI_STAGES)
    a = ap.parse_args(argv)

    cfg = Config()
    url = cfg.agent_database_url
    if not url:
        print("Set AGENT_DATABASE_URL (or DATABASE_URL).", file=sys.stderr)
        return 2
    conn = db.connect(url)
    if a.cmd == "pull":
        tasks = pull(conn, a.stage, a.limit, a.worker, cfg.task_lease_minutes)
        text = json.dumps(tasks, indent=1, ensure_ascii=False, default=str)
        if a.out:
            with open(a.out, "w", encoding="utf-8") as f:
                f.write(text)
        print(text)
        return 0
    if a.cmd == "submit":
        try:
            raw = sys.stdin.read() if a.file == "-" else open(a.file, encoding="utf-8").read()
            result = json.loads(raw)
        except (OSError, json.JSONDecodeError) as e:
            print(json.dumps({"ok": False, "errors": [f"could not read JSON: {e}"]}, indent=1))
            return 1
        try:
            print(json.dumps({"ok": True, **submit(conn, a.task_id, result, a.worker)}, indent=1))
            return 0
        except ResultRejected as e:
            print(json.dumps({"ok": False, "errors": e.errors,
                              "hint": "fix these and submit again before the lease expires"}, indent=1))
            return 1
        except psycopg.Error as e:
            print(json.dumps({"ok": False, "errors": [f"database error: {e}"]}, indent=1))
            return 1
    if a.cmd == "release":
        release(conn, a.task_id, a.reason)
        print(json.dumps({"ok": True}))
        return 0
    if a.cmd == "stats":
        print(json.dumps(stats(conn), indent=1))
        return 0
    if a.cmd == "schema":
        row = conn.execute(
            "select payload->'output_schema' s from agent_tasks where stage=%s order by created_at desc limit 1",
            (a.stage,),
        ).fetchone()
        print(json.dumps(row["s"] if row else None, indent=1))
        return 0
    return 1


if __name__ == "__main__":
    sys.exit(main())
