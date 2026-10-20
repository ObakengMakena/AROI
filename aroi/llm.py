"""Claude API wrapper: structured JSON calls, web-search research calls, cost logging, budget guard."""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any

import psycopg

log = logging.getLogger(__name__)

# USD per million tokens (input, output). Source: platform.claude.com/docs/en/about-claude/pricing (Oct 2026).
PRICES: dict[str, tuple[float, float]] = {
    "claude-haiku-5-5": (0.10, 0.50),
    "claude-sonnet-5-5": (2.00, 10.00),
    "claude-opus-5-5": (4.00, 20.00),
    "claude-sonnet-5": (2.00, 10.00),
    "claude-haiku-4-5": (1.00, 5.00),
}
HAIKU_55_LONG = (0.50, 2.50)  # Haiku 5.5 prompts over 100k tokens
WEB_SEARCH_USD = 0.01  # $10 per 1,000 searches


class BudgetExceeded(RuntimeError):
    pass


class LLMError(RuntimeError):
    pass


def price_for(model: str, tokens_in: int) -> tuple[float, float]:
    if model.startswith("claude-haiku-5-5") and tokens_in > 100_000:
        return HAIKU_55_LONG
    for key, p in PRICES.items():
        if model.startswith(key):
            return p
    log.warning("No price for %s; assuming Sonnet pricing for budget purposes", model)
    return PRICES["claude-sonnet-5-5"]


def cost_of(model: str, tokens_in: int, tokens_out: int, searches: int = 0) -> float:
    pin, pout = price_for(model, tokens_in)
    return tokens_in * pin / 1e6 + tokens_out * pout / 1e6 + searches * WEB_SEARCH_USD


@dataclass
class Citation:
    url: str
    title: str | None
    cited_text: str | None


@dataclass
class ResearchResult:
    memo: str
    sources: list[Citation] = field(default_factory=list)
    searches: int = 0


def _usage_numbers(usage: Any) -> tuple[int, int, int]:
    if usage is None:
        return 0, 0, 0
    tin = (
        (getattr(usage, "input_tokens", 0) or 0)
        + (getattr(usage, "cache_creation_input_tokens", 0) or 0)
        + (getattr(usage, "cache_read_input_tokens", 0) or 0)
    )
    tout = getattr(usage, "output_tokens", 0) or 0
    stu = getattr(usage, "server_tool_use", None)
    searches = (getattr(stu, "web_search_requests", 0) or 0) if stu else 0
    return tin, tout, searches


class LLM:
    """Every call is logged to agent_runs on its own autocommit connection, so costs are
    recorded even when the calling stage rolls back its own transaction."""

    def __init__(self, cfg, log_conn: psycopg.Connection, client: Any | None = None) -> None:
        self.cfg = cfg
        self.log_conn = log_conn
        self.log_conn.autocommit = True
        if client is None:
            import anthropic

            client = anthropic.Anthropic(api_key=cfg.anthropic_api_key or None, max_retries=4)
        self.client = client
        self.run_spend = 0.0

    # ------------------------------------------------------------------ budget
    def check_budget(self) -> None:
        if self.run_spend >= self.cfg.run_budget_usd:
            raise BudgetExceeded(f"Run budget of ${self.cfg.run_budget_usd:.2f} reached")
        row = self.log_conn.execute(
            "select coalesce(sum(cost_usd),0) as s from agent_runs where started_at >= date_trunc('month', now())"
        ).fetchone()
        spent = float(row["s"] if isinstance(row, dict) else row[0])
        if spent >= self.cfg.monthly_budget_usd:
            raise BudgetExceeded(f"Monthly budget of ${self.cfg.monthly_budget_usd:.2f} reached (spent ${spent:.2f})")

    def _start(self, stage: str, model: str, prompt_version: str, paper_id=None, opportunity_id=None) -> str:
        row = self.log_conn.execute(
            """insert into agent_runs (stage, model, prompt_version, paper_id, opportunity_id)
               values (%s, %s, %s, %s, %s) returning id""",
            (stage, model, prompt_version, paper_id, opportunity_id),
        ).fetchone()
        return row["id"] if isinstance(row, dict) else row[0]

    def _finish(self, run_id, model: str, tin: int, tout: int, searches: int, error: str | None = None) -> float:
        cost = cost_of(model, tin, tout, searches)
        self.run_spend += cost
        self.log_conn.execute(
            """update agent_runs set status=%s, tokens_in=%s, tokens_out=%s, web_searches=%s,
                   cost_usd=%s, error=%s, finished_at=now() where id=%s""",
            ("failed" if error else "ok", tin, tout, searches, cost, error, run_id),
        )
        return cost

    # ------------------------------------------------------------------ calls
    def structured(
        self,
        *,
        stage: str,
        model: str,
        system: str,
        user: str,
        schema: dict[str, Any],
        prompt_version: str,
        max_tokens: int = 4000,
        paper_id=None,
        opportunity_id=None,
    ) -> dict[str, Any]:
        """One call whose answer is guaranteed to match `schema` (structured outputs)."""
        self.check_budget()
        run_id = self._start(stage, model, prompt_version, paper_id, opportunity_id)
        tin = tout = 0
        try:
            resp = self.client.messages.create(
                model=model,
                max_tokens=max_tokens,
                system=system,
                messages=[{"role": "user", "content": user}],
                output_config={"format": {"type": "json_schema", "schema": schema}},
            )
            tin, tout, _ = _usage_numbers(getattr(resp, "usage", None))
            if resp.stop_reason == "refusal":
                raise LLMError("Model declined the request")
            if resp.stop_reason == "max_tokens":
                raise LLMError(f"Output truncated at max_tokens={max_tokens}")
            text = "".join(b.text for b in resp.content if getattr(b, "type", "") == "text")
            data = json.loads(text)
        except Exception as e:
            self._finish(run_id, model, tin, tout, 0, error=str(e)[:500])
            raise
        self._finish(run_id, model, tin, tout, 0)
        return data

    def research(
        self,
        *,
        stage: str,
        model: str,
        system: str,
        user: str,
        prompt_version: str,
        max_searches: int = 6,
        country: str = "ZA",
        max_tokens: int = 6000,
        opportunity_id=None,
    ) -> ResearchResult:
        """Free-text research with Claude's server-side web search. Every source in the
        result comes from the API's citation blocks, never from model-typed URLs."""
        self.check_budget()
        run_id = self._start(stage, model, prompt_version, opportunity_id=opportunity_id)
        tools = [
            {
                "type": "web_search_20250305",
                "name": "web_search",
                "max_uses": max_searches,
                "user_location": {"type": "approximate", "country": country},
            }
        ]
        messages: list[dict[str, Any]] = [{"role": "user", "content": user}]
        tin_total = tout_total = searches_total = 0
        memo_parts: list[str] = []
        sources: dict[str, int] = {}
        source_list: list[Citation] = []
        try:
            for _ in range(6):  # pause_turn continuations
                resp = self.client.messages.create(
                    model=model, max_tokens=max_tokens, system=system, messages=messages, tools=tools
                )
                tin, tout, s = _usage_numbers(getattr(resp, "usage", None))
                tin_total += tin
                tout_total += tout
                searches_total += s
                for block in resp.content:
                    if getattr(block, "type", "") != "text":
                        continue
                    piece = block.text
                    marks = []
                    for c in getattr(block, "citations", None) or []:
                        url = getattr(c, "url", None)
                        if not url:
                            continue
                        if url not in sources:
                            sources[url] = len(source_list) + 1
                            source_list.append(
                                Citation(url=url, title=getattr(c, "title", None), cited_text=getattr(c, "cited_text", None))
                            )
                        marks.append(f"[S{sources[url]}]")
                    if marks:
                        piece = piece + " " + "".join(dict.fromkeys(marks))
                    memo_parts.append(piece)
                if resp.stop_reason == "pause_turn":
                    if searches_total >= max_searches:
                        log.info("Search budget for this validation used up; keeping the memo so far")
                        break
                    memo_parts.append("\n")
                    messages.append({"role": "assistant", "content": resp.content})
                    continue
                if resp.stop_reason == "refusal":
                    raise LLMError("Model declined the research request")
                break
        except Exception as e:
            self._finish(run_id, model, tin_total, tout_total, searches_total, error=str(e)[:500])
            raise
        self._finish(run_id, model, tin_total, tout_total, searches_total)
        memo = "".join(memo_parts).strip()
        if source_list:
            memo += "\n\nSOURCES\n" + "\n".join(
                f"S{i}: {c.title or 'untitled'} — {c.url}" for i, c in enumerate(source_list, 1)
            )
        return ResearchResult(memo=memo, sources=source_list, searches=searches_total)
