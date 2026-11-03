"""MCP server: read-only tools over UK public tenders and Companies House.

Tool design choices:
- Task-shaped tools ("score recent tenders for this profile"), not raw table access.
- Read-only, capped and paginated, so one call cannot flood the model's context.
- Every result carries source_url; 'found: false' is a valid answer.
"""

from __future__ import annotations

import os
from typing import Any

import httpx
from mcp.server.mcpserver import MCPServer
from mcp.types import ToolAnnotations

from .scoring import PROFILES, score_tender
from .sources import SourceError, fetch_company, fetch_recent_tenders, normalise_company_number

server = MCPServer(
    name="uk-tenders",
    instructions=(
        "Read-only tools over UK public procurement notices (Contracts Finder) and Companies House. "
        "Every result has a source_url: cite it. If a tool returns found=false or an error, say so; "
        "never fill gaps from memory."
    ),
)

READ_ONLY = ToolAnnotations(read_only_hint=True, open_world_hint=True)
_client: httpx.AsyncClient | None = None


def client() -> httpx.AsyncClient:
    global _client
    if _client is None:
        _client = httpx.AsyncClient(headers={"User-Agent": "uk-tenders-mcp/0.1"})
    return _client


def _clamp(value: int, low: int, high: int) -> int:
    return max(low, min(high, int(value)))


@server.tool(annotations=READ_ONLY)
async def search_tenders(days: int = 7, keywords: list[str] | None = None, limit: int = 20) -> dict[str, Any]:
    """Find open UK public-sector tenders published on Contracts Finder in the last `days` days (1-30).

    Use when the user asks what tenders are out, optionally filtered by keywords (any match, in title
    or description). Returns at most `limit` (1-50) tenders, newest first, each with a source_url to cite.
    """
    days, limit = _clamp(days, 1, 30), _clamp(limit, 1, 50)
    try:
        tenders = await fetch_recent_tenders(client(), days=days)
    except SourceError as exc:
        return {"found": False, "error": str(exc)}
    if keywords:
        terms = [k.lower() for k in keywords if k.strip()]
        tenders = [t for t in tenders if any(k in f"{t.title} {t.description}".lower() for k in terms)]
    tenders.sort(key=lambda t: t.published or "", reverse=True)
    return {
        "found": bool(tenders),
        "count": len(tenders),
        "returned": min(len(tenders), limit),
        "tenders": [t.model_dump() for t in tenders[:limit]],
    }


@server.tool(annotations=READ_ONLY)
async def list_profiles() -> dict[str, Any]:
    """List the bid profiles that score_tenders can use, with what each one covers."""
    return {"profiles": [{"name": p.name, "description": p.description} for p in PROFILES.values()]}


@server.tool(annotations=READ_ONLY)
async def score_tenders(profile: str = "rail_civils", days: int = 7, limit: int = 20) -> dict[str, Any]:
    """Score recent tenders GO / MAYBE / NO-GO against a bid profile, with the reasons for each score.

    Use when the user asks which tenders are worth bidding for. Call list_profiles for valid names.
    Returns GO and MAYBE first, at most `limit` (1-50), each with reasons and a source_url.
    """
    chosen = PROFILES.get(profile)
    if chosen is None:
        return {"found": False, "error": f"Unknown profile '{profile}'", "profiles": list(PROFILES)}
    days, limit = _clamp(days, 1, 30), _clamp(limit, 1, 50)
    try:
        tenders = await fetch_recent_tenders(client(), days=days)
    except SourceError as exc:
        return {"found": False, "error": str(exc)}
    order = {"GO": 0, "MAYBE": 1, "NO-GO": 2}
    verdicts = sorted((score_tender(t, chosen) for t in tenders), key=lambda v: (order[v.verdict], -v.score))
    summary = {k: sum(1 for v in verdicts if v.verdict == k) for k in order}
    return {
        "found": bool(verdicts),
        "profile": chosen.name,
        "summary": summary,
        "results": [v.model_dump() for v in verdicts[:limit]],
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
