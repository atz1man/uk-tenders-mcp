"""MCP server: read-only tools over UK public tenders and Companies House.

Tool design choices:
- Task-shaped tools ("score this week's tenders for this profile"), not raw table access.
- Read-only, capped and compact, so one call cannot flood the model's context.
- Every result carries source_url and says which sources answered; 'found: false' is a valid answer.
- Fetches are cached briefly, so a search followed by a score does not hit the APIs twice.
"""

from __future__ import annotations

import os
import re
import time
from datetime import datetime, timezone
from typing import Any, Literal

import logging

import httpx
from mcp.server.mcpserver import MCPServer
from mcp.types import ToolAnnotations

from .models import Tender
from .scoring import PROFILES, score_tender
from .sources import (CONTRACTS_FINDER, FIND_A_TENDER, FetchResult, SourceError, fetch_company,
                      fetch_tenders, normalise_company_number)

server = MCPServer(
    name="uk-tenders",
    instructions=(
        "Read-only tools over UK public procurement notices (Find a Tender and Contracts Finder) and "
        "Companies House. Every result has a source_url: cite it. Results say which sources answered; if "
        "one failed, say the list may be incomplete. If a tool returns found=false or an error, say so; "
        "never fill gaps from memory."
    ),
)

# httpx logs every request at INFO; a week of Find a Tender is ~30 requests of noise on stderr.
logging.getLogger("httpx").setLevel(logging.WARNING)

READ_ONLY = ToolAnnotations(read_only_hint=True, open_world_hint=True)
SOURCES = {"all": (FIND_A_TENDER, CONTRACTS_FINDER), "find_a_tender": (FIND_A_TENDER,),
           "contracts_finder": (CONTRACTS_FINDER,)}
CACHE_SECONDS = 600
PARTIAL_CACHE_SECONDS = 60  # a result missing a source or some days is retried sooner

_client: httpx.AsyncClient | None = None
_cache: dict[tuple[int, tuple[str, ...]], tuple[float, FetchResult]] = {}


def client() -> httpx.AsyncClient:
    global _client
    if _client is None:
        _client = httpx.AsyncClient(headers={"User-Agent": "uk-tenders-mcp/0.2"})
    return _client


def _clamp(value: int, low: int, high: int) -> int:
    return max(low, min(high, int(value)))


async def _load(days: int, source: str) -> FetchResult:
    key = (days, SOURCES[source])
    hit = _cache.get(key)
    if hit:
        complete = all(s.ok and not s.missing_days for s in hit[1].sources.values())
        if time.monotonic() - hit[0] < (CACHE_SECONDS if complete else PARTIAL_CACHE_SECONDS):
            return hit[1]
    result = await fetch_tenders(client(), days=days, sources=SOURCES[source])
    if any(s.ok for s in result.sources.values()):
        _cache[key] = (time.monotonic(), result)
    return result


def _in_region(tender: Tender, wanted: list[str] | None) -> bool:
    """Match any wanted region against every region the buyer ticked. A notice with no region is kept
    out of a region-filtered search rather than guessed in."""
    if not wanted:
        return True
    have = [r.lower() for r in tender.regions] or ([tender.region.lower()] if tender.region else [])
    return any(w.lower().strip() in r for w in wanted for r in have)


def _sources_block(result: FetchResult) -> dict[str, Any]:
    return {name: status.as_dict() for name, status in result.sources.items()}


@server.tool(annotations=READ_ONLY)
async def search_tenders(
    days: int = 7,
    keywords: list[str] | None = None,
    regions: list[str] | None = None,
    buyer: str | None = None,
    source: Literal["all", "find_a_tender", "contracts_finder"] = "all",
    limit: int = 20,
) -> dict[str, Any]:
    """Find open UK public-sector tenders published or updated in the last `days` days (1-30).

    Searches Find a Tender and Contracts Finder together, removes notices listed on both, and drops
    tenders whose deadline has passed. Optional filters: keywords (any match in title or description),
    regions (for example ["London", "South East"]) and buyer (part of the buyer's name, for example
    "Derbyshire County Council", to see what else a buyer has out). Returns at most `limit` (1-50), soonest
    deadline first, each with buyer, value, deadline, days_left and a source_url to cite.
    """
    days, limit = _clamp(days, 1, 30), _clamp(limit, 1, 50)
    if source not in SOURCES:
        return {"found": False, "error": f"source must be one of {list(SOURCES)}"}
    result = await _load(days, source)
    if not any(s.ok for s in result.sources.values()):
        return {"found": False, "error": "No tender source answered.", "sources": _sources_block(result)}

    tenders = [t for t in result.tenders if _in_region(t, regions)]
    if buyer and buyer.strip():
        wanted = re.sub(r"\s+", " ", buyer.strip().lower())
        tenders = [t for t in tenders if t.buyer and wanted in re.sub(r"\s+", " ", t.buyer.lower())]
    if keywords:
        # Whole words, as in scoring: 'rail' must not match 'trail' or 'guardrail'.
        patterns = [re.compile(rf"(?<![a-z]){re.escape(k.lower().strip())}(?![a-z])") for k in keywords if k.strip()]
        tenders = [t for t in tenders if any(p.search(f"{t.title} {t.description}".lower()) for p in patterns)]
    tenders.sort(key=lambda t: (t.deadline is None, t.deadline or ""))
    now = datetime.now(timezone.utc)
    return {
        "found": bool(tenders),
        "count": len(tenders),
        "returned": min(len(tenders), limit),
        "sources": _sources_block(result),
        "tenders": [t.listing(now) for t in tenders[:limit]],
    }


@server.tool(annotations=READ_ONLY)
async def list_profiles() -> dict[str, Any]:
    """List the bid profiles that score_tenders can use, with what each one covers."""
    return {"profiles": [{"name": p.name, "description": p.description,
                          "value_range_gbp": [p.min_value, p.max_value]} for p in PROFILES.values()]}


@server.tool(annotations=READ_ONLY)
async def score_tenders(
    profile: str = "rail_civils",
    days: int = 7,
    regions: list[str] | None = None,
    include_no_go: bool = False,
    limit: int = 20,
) -> dict[str, Any]:
    """Score this week's open tenders GO / MAYBE / NO-GO against a bid profile, with reasons.

    Use when the user asks which tenders are worth bidding for. Call list_profiles for valid names.
    Searches Find a Tender and Contracts Finder. Returns GO then MAYBE, highest score first, at most
    `limit` (1-50), each with buyer, value, deadline, days_left, reasons and a source_url. NO-GO
    results are counted but left out unless include_no_go is true.
    """
    chosen = PROFILES.get(profile)
    if chosen is None:
        return {"found": False, "error": f"Unknown profile '{profile}'", "profiles": list(PROFILES)}
    days, limit = _clamp(days, 1, 30), _clamp(limit, 1, 50)
    result = await _load(days, "all")
    if not any(s.ok for s in result.sources.values()):
        return {"found": False, "error": "No tender source answered.", "sources": _sources_block(result)}

    now = datetime.now(timezone.utc)
    tenders = [t for t in result.tenders if _in_region(t, regions)]
    order = {"GO": 0, "MAYBE": 1, "NO-GO": 2}
    verdicts = sorted((score_tender(t, chosen, now) for t in tenders),
                      key=lambda v: (order[v.verdict], -v.score, v.deadline or "9999"))
    summary = {k: sum(1 for v in verdicts if v.verdict == k) for k in order}
    shown = verdicts if include_no_go else [v for v in verdicts if v.verdict != "NO-GO"]
    return {
        "found": bool(verdicts),
        "profile": chosen.name,
        "scored": len(verdicts),
        "summary": summary,
        "sources": _sources_block(result),
        "results": [v.model_dump() for v in shown[:limit]],
    }


@server.tool(annotations=READ_ONLY)
async def get_company(company_number: str) -> dict[str, Any]:
    """Look up a UK company on Companies House by its company number (for example 00445790).

    Returns status, incorporation date, SIC codes, insolvency history and whether accounts are overdue,
    with a source_url. Use before stating anything about a company's status; never infer it.
    """
    number = normalise_company_number(company_number)
    if number is None:
        return {"found": False, "error": "A company number is 8 letters or digits, for example 00445790."}
    api_key = os.environ.get("COMPANIES_HOUSE_API_KEY", "")
    if not api_key:
        return {"found": False, "error": "COMPANIES_HOUSE_API_KEY is not set on the server."}
    try:
        company = await fetch_company(client(), number, api_key)
    except SourceError as exc:
        return {"found": False, "error": str(exc)}
    if company is None:
        return {"found": False, "company_number": number, "note": "No company with this number on Companies House."}
    return {"found": True, "company": company.model_dump()}


def main() -> None:
    server.run()


if __name__ == "__main__":
    main()
