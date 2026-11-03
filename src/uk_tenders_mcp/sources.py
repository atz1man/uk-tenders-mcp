"""Clients for UK public procurement and company data.

Design rules:
- Every record keeps the URL it came from, so an agent can cite it.
- A missing record is reported as missing, never filled in.
- Network calls have timeouts and bounded retries with backoff.
"""

from __future__ import annotations

import asyncio
import re
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx

from .models import Company, Tender

CF_SEARCH = "https://www.contractsfinder.service.gov.uk/Published/Notices/OCDS/Search"
CF_NOTICE = "https://www.contractsfinder.service.gov.uk/Notice/{guid}"
CH_API = "https://api.company-information.service.gov.uk/company/{number}"
CH_PUBLIC = "https://find-and-update.company-information.service.gov.uk/company/{number}"

RETRYABLE = {429, 500, 502, 503, 504}
_GUID = re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}")


class SourceError(Exception):
    """A source could not be reached or returned an error."""


async def get_json(
    client: httpx.AsyncClient,
    url: str,
    *,
    params: dict[str, Any] | None = None,
    auth: tuple[str, str] | None = None,
    retries: int = 3,
    backoff: float = 1.0,
) -> Any | None:
    """GET a JSON document. Returns None on 404. Raises SourceError on anything else that fails."""
    for attempt in range(retries):
        try:
            response = await client.get(url, params=params, auth=auth, timeout=20)
        except httpx.TransportError as exc:
            if attempt == retries - 1:
                raise SourceError(f"Could not reach {url}: {exc}") from exc
            await asyncio.sleep(backoff * 2**attempt)
            continue
        if response.status_code == 404:
            return None
        if response.status_code in RETRYABLE and attempt < retries - 1:
            await asyncio.sleep(backoff * 2**attempt)
            continue
        if response.status_code >= 400:
            raise SourceError(f"{url} returned HTTP {response.status_code}")
        return response.json()
    raise SourceError(f"Gave up on {url} after {retries} attempts")


def notice_url(ocid: str) -> str:
    """Contracts Finder notice page, derived from the GUID inside the OCID."""
    match = _GUID.search(ocid)
    return CF_NOTICE.format(guid=match.group(0)) if match else CF_SEARCH


def parse_release(release: dict[str, Any]) -> Tender:
    """Turn one OCDS release into a Tender. Missing fields stay empty rather than guessed."""
    tender = release.get("tender") or {}
    value = tender.get("value") or {}
    amount = value.get("amount")
    value_gbp = float(amount) if amount is not None and value.get("currency", "GBP") == "GBP" else None

    buyer = (release.get("buyer") or {}).get("name")
    if not buyer:
        for party in release.get("parties") or []:
            if "buyer" in (party.get("roles") or []):
                buyer = party.get("name")
                break

    cpv: list[str] = []
    classification = tender.get("classification") or {}
    if classification.get("scheme", "").upper() == "CPV" and classification.get("id"):
        cpv.append(str(classification["id"]))
    for item in tender.get("items") or []:
        c = item.get("classification") or {}
        if c.get("scheme", "").upper() == "CPV" and c.get("id") and str(c["id"]) not in cpv:
            cpv.append(str(c["id"]))

    ocid = release.get("ocid") or release.get("id") or "unknown"
    return Tender(
        ocid=ocid,
        title=(tender.get("title") or "Untitled notice").strip(),
        buyer=buyer,
        description=(tender.get("description") or "").strip(),
        value_gbp=value_gbp,
        published=release.get("date"),
        deadline=(tender.get("tenderPeriod") or {}).get("endDate"),
        cpv_codes=cpv,
        source_url=notice_url(ocid),
    )


async def fetch_recent_tenders(
    client: httpx.AsyncClient, days: int = 7, max_pages: int = 3, page_size: int = 100
) -> list[Tender]:
    """Open tenders published on Contracts Finder in the last `days` days."""
    now = datetime.now(timezone.utc)
    params: dict[str, Any] | None = {
        "publishedFrom": (now - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%S"),
        "publishedTo": now.strftime("%Y-%m-%dT%H:%M:%S"),
        "stages": "tender",
        "limit": page_size,
    }
    url: str | None = CF_SEARCH
    tenders: list[Tender] = []
    seen: set[str] = set()
    for _ in range(max_pages):
        if not url:
            break
        data = await get_json(client, url, params=params) or {}
        for release in data.get("releases") or []:
            t = parse_release(release)
            if t.ocid not in seen:
                seen.add(t.ocid)
                tenders.append(t)
        url = (data.get("links") or {}).get("next")
        params = None  # the next link already carries its query
    return tenders


def normalise_company_number(raw: str) -> str | None:
    """Companies House numbers are 8 characters; digits-only numbers are zero-padded."""
    value = re.sub(r"\s+", "", raw or "").upper()
    if value.isdigit():
        value = value.zfill(8)
    return value if re.fullmatch(r"[A-Z0-9]{8}", value) else None


def parse_company(data: dict[str, Any], number: str) -> Company:
    address = data.get("registered_office_address") or {}
    accounts = data.get("accounts") or {}
    return Company(
        number=data.get("company_number", number),
        name=data.get("company_name", ""),
        status=data.get("company_status"),
        incorporated=data.get("date_of_creation"),
        locality=address.get("locality"),
        postcode=address.get("postal_code"),
        sic_codes=list(data.get("sic_codes") or []),
        has_insolvency_history=bool(data.get("has_insolvency_history", False)),
        accounts_overdue=bool(accounts.get("overdue", False)),
        source_url=CH_PUBLIC.format(number=data.get("company_number", number)),
    )


async def fetch_company(client: httpx.AsyncClient, number: str, api_key: str) -> Company | None:
    data = await get_json(client, CH_API.format(number=number), auth=(api_key, ""))
    return parse_company(data, number) if data else None
