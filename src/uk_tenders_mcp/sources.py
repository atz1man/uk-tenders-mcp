"""Clients for UK public procurement and company data.

Two tender sources, both publishing OCDS:
- Contracts Finder: lower-value English notices.
- Find a Tender: above-threshold notices for the whole UK, and since the Procurement Act 2023
  most new notices. In a typical week it carries ten times as many open tenders as Contracts Finder.

Design rules:
- Every record keeps the URL it came from, so an agent can cite it.
- A missing record is reported as missing, never filled in.
- Network calls have timeouts and bounded retries with backoff.
- One source failing does not hide the other: results say which sources answered.
"""

from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx

from .models import Company, Tender, days_until

CF_SEARCH = "https://www.contractsfinder.service.gov.uk/Published/Notices/OCDS/Search"
CF_NOTICE = "https://www.contractsfinder.service.gov.uk/Notice/{guid}"
FTS_SEARCH = "https://www.find-tender.service.gov.uk/api/1.0/ocdsReleasePackages"
FTS_NOTICE = "https://www.find-tender.service.gov.uk/Notice/{id}"
CH_API = "https://api.company-information.service.gov.uk/company/{number}"
CH_PUBLIC = "https://find-and-update.company-information.service.gov.uk/company/{number}"

CONTRACTS_FINDER = "Contracts Finder"
FIND_A_TENDER = "Find a Tender"

RETRYABLE = {429, 500, 502, 503, 504}
_GUID = re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}")
_TS = "%Y-%m-%dT%H:%M:%S"

# NUTS 1 codes used by Find a Tender, shown as names so they match Contracts Finder's regions.
NUTS1 = {
    "UKC": "North East", "UKD": "North West", "UKE": "Yorkshire and the Humber",
    "UKF": "East Midlands", "UKG": "West Midlands", "UKH": "East of England", "UKI": "London",
    "UKJ": "South East", "UKK": "South West", "UKL": "Wales", "UKM": "Scotland",
    "UKN": "Northern Ireland",
}


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
            response = await client.get(url, params=params, auth=auth, timeout=30)
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


def region_name(raw: str | None) -> str | None:
    """'UKI32' -> 'London'; names pass through; unknown codes are kept as given."""
    if not raw:
        return None
    raw = raw.strip()
    return NUTS1.get(raw[:3].upper(), raw) if re.fullmatch(r"UK[A-Z0-9]*", raw.upper()) else raw


def _cpv_codes(tender: dict[str, Any]) -> list[str]:
    """CPV codes from the main classification, additional ones, and every item.

    Find a Tender often leaves tender.classification empty and puts CPV codes under
    items[].additionalClassifications, so all four places are read.
    """
    found: list[str] = []

    def add(c: dict[str, Any] | None) -> None:
        if c and str(c.get("scheme", "")).upper() == "CPV" and c.get("id") and str(c["id"]) not in found:
            found.append(str(c["id"]))

    add(tender.get("classification"))
    for c in tender.get("additionalClassifications") or []:
        add(c)
    for item in tender.get("items") or []:
        add(item.get("classification"))
        for c in item.get("additionalClassifications") or []:
            add(c)
    return found


ENGLAND = {"North East", "North West", "Yorkshire and the Humber", "East Midlands", "West Midlands",
           "East of England", "London", "South East", "South West"}


def _regions(release: dict[str, Any], tender: dict[str, Any]) -> list[str]:
    """Every delivery region the buyer ticked; the buyer's own region only if none were given.

    Buyers often tick every region for a national framework, so all are kept rather than the first.
    """
    found: list[str] = []
    for item in tender.get("items") or []:
        for address in item.get("deliveryAddresses") or []:
            name = region_name(address.get("region"))
            if name and name not in found:
                found.append(name)
    if not found:
        for party in release.get("parties") or []:
            if "buyer" in (party.get("roles") or []):
                name = region_name((party.get("address") or {}).get("region"))
                if name:
                    found.append(name)
                break
    return found


def region_label(regions: list[str]) -> str | None:
    if not regions:
        return None
    if set(NUTS1.values()) <= set(regions):
        return "UK-wide"
    if ENGLAND <= set(regions):
        return "England-wide"
    if len(regions) <= 3:
        return ", ".join(regions)
    return f"{len(regions)} regions"


def _sme(tender: dict[str, Any]) -> bool | None:
    flags = [s.get("sme") for s in [tender.get("suitability") or {}] +
             [(lot.get("suitability") or {}) for lot in tender.get("lots") or []] if "sme" in s]
    return any(flags) if flags else None


def parse_release(release: dict[str, Any], source: str = CONTRACTS_FINDER) -> Tender:
    """Turn one OCDS release into a Tender. Missing fields stay empty rather than guessed."""
    tender = release.get("tender") or {}
    value = tender.get("value") or {}
    amount = value.get("amount", value.get("amountGross"))
    value_gbp = float(amount) if amount is not None and value.get("currency", "GBP") == "GBP" else None

    buyer = (release.get("buyer") or {}).get("name")
    if not buyer:
        for party in release.get("parties") or []:
            if "buyer" in (party.get("roles") or []):
                buyer = party.get("name")
                break

    ocid = release.get("ocid") or release.get("id") or "unknown"
    if source == FIND_A_TENDER and release.get("id"):
        url = FTS_NOTICE.format(id=release["id"])
    else:
        url = notice_url(ocid)

    return Tender(
        ocid=ocid,
        title=(tender.get("title") or "Untitled notice").strip(),
        buyer=buyer,
        description=(tender.get("description") or "").strip(),
        value_gbp=value_gbp,
        published=release.get("date"),
        deadline=(tender.get("tenderPeriod") or {}).get("endDate"),
        cpv_codes=_cpv_codes(tender),
        category=tender.get("mainProcurementCategory"),
        region=region_label(regions := _regions(release, tender)),
        regions=regions,
        sme_suitable=_sme(tender),
        source=source,
        source_url=url,
    )


@dataclass
class SourceStatus:
    ok: bool = True
    count: int = 0
    truncated: bool = False
    error: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {k: v for k, v in self.__dict__.items() if v not in (None, False) or k == "ok"}


@dataclass
class FetchResult:
    tenders: list[Tender] = field(default_factory=list)
    sources: dict[str, SourceStatus] = field(default_factory=dict)


async def fetch_contracts_finder(
    client: httpx.AsyncClient, days: int = 7, max_pages: int = 5, page_size: int = 100,
) -> tuple[list[Tender], bool]:
    """Open tenders published on Contracts Finder in the last `days` days. Returns (tenders, truncated)."""
    now = datetime.now(timezone.utc)
    params: dict[str, Any] | None = {
        "publishedFrom": (now - timedelta(days=days)).strftime(_TS),
        "publishedTo": now.strftime(_TS),
        "stages": "tender",
        "limit": page_size,
    }
    url: str | None = CF_SEARCH
    tenders: list[Tender] = []
    seen: set[str] = set()
    for _ in range(max_pages):
        data = await get_json(client, url, params=params) or {}
        for release in data.get("releases") or []:
            t = parse_release(release, CONTRACTS_FINDER)
            if t.ocid not in seen:
                seen.add(t.ocid)
                tenders.append(t)
        url = (data.get("links") or {}).get("next")
        params = None  # the next link already carries its query
        if not url:
            return tenders, False
    return tenders, True


async def _fts_slice(
    client: httpx.AsyncClient, start: datetime, end: datetime, max_pages: int,
) -> tuple[list[dict[str, Any]], bool]:
    """All releases updated in [start, end), following Find a Tender's cursor links."""
    params: dict[str, Any] | None = {"updatedFrom": start.strftime(_TS), "updatedTo": end.strftime(_TS), "limit": 100}
    url: str | None = FTS_SEARCH
    releases: list[dict[str, Any]] = []
    for _ in range(max_pages):
        data = await get_json(client, url, params=params) or {}
        releases.extend(data.get("releases") or [])
        url = (data.get("links") or {}).get("next")
        params = None
        if not url:
            return releases, False
    return releases, True


async def fetch_find_a_tender(
    client: httpx.AsyncClient, days: int = 7, pages_per_day: int = 8, concurrency: int = 4,
) -> tuple[list[Tender], bool]:
    """Open tenders on Find a Tender, published or updated in the last `days` days.

    The API pages with a cursor, so one long window is slow (about 25 pages a week, one after
    another). The window is split into one-day slices fetched in parallel instead. Find a Tender
    mixes every notice type, so only tender and tender-update notices are kept, the newest
    release per procurement wins, and cancelled procurements are dropped.
    """
    now = datetime.now(timezone.utc)
    gate = asyncio.Semaphore(concurrency)

    async def one(i: int) -> tuple[list[dict[str, Any]], bool]:
        async with gate:
            return await _fts_slice(client, now - timedelta(days=i + 1), now - timedelta(days=i), pages_per_day)

    slices = await asyncio.gather(*(one(i) for i in range(days)))
    truncated = any(t for _, t in slices)

    latest: dict[str, dict[str, Any]] = {}
    cancelled: set[str] = set()
    for releases, _ in slices:
        for r in releases:
            tags = set(r.get("tag") or [])
            ocid = r.get("ocid")
            if not ocid:
                continue
            if "tenderCancellation" in tags or (r.get("tender") or {}).get("status") == "cancelled":
                cancelled.add(ocid)
                continue
            if not tags & {"tender", "tenderUpdate"}:
                continue
            if ocid not in latest or (r.get("date") or "") > (latest[ocid].get("date") or ""):
                latest[ocid] = r

    tenders = [parse_release(r, FIND_A_TENDER) for ocid, r in latest.items() if ocid not in cancelled]
    return tenders, truncated


def _dedupe_key(t: Tender) -> str:
    norm = lambda s: re.sub(r"[^a-z0-9]+", "", (s or "").lower())  # noqa: E731
    return f"{norm(t.title)[:80]}|{norm(t.buyer)}"


def merge_sources(batches: dict[str, list[Tender]]) -> list[Tender]:
    """Merge sources, keeping one record per notice. The same tender is often on both sites:
    the Find a Tender record wins (it carries more fields) and notes where else it is listed."""
    order = [FIND_A_TENDER, CONTRACTS_FINDER]
    merged: dict[str, Tender] = {}
    for source in sorted(batches, key=lambda s: order.index(s) if s in order else len(order)):
        for t in batches[source]:
            key = _dedupe_key(t)
            if key in merged:
                if t.source not in merged[key].also_listed_on and t.source != merged[key].source:
                    merged[key].also_listed_on.append(t.source)
            else:
                merged[key] = t
    return list(merged.values())


async def fetch_tenders(
    client: httpx.AsyncClient, days: int = 7, sources: tuple[str, ...] = (CONTRACTS_FINDER, FIND_A_TENDER),
    include_closed: bool = False,
) -> FetchResult:
    """Fetch every requested source concurrently, merge, and drop tenders whose deadline has passed."""
    fetchers = {CONTRACTS_FINDER: fetch_contracts_finder, FIND_A_TENDER: fetch_find_a_tender}
    chosen = [s for s in sources if s in fetchers]
    outcomes = await asyncio.gather(*(fetchers[s](client, days=days) for s in chosen), return_exceptions=True)

    result = FetchResult()
    batches: dict[str, list[Tender]] = {}
    for source, outcome in zip(chosen, outcomes):
        if isinstance(outcome, BaseException):
            result.sources[source] = SourceStatus(ok=False, error=str(outcome))
            continue
        tenders, truncated = outcome
        batches[source] = tenders
        result.sources[source] = SourceStatus(count=len(tenders), truncated=truncated)

    merged = merge_sources(batches)
    if not include_closed:
        merged = [t for t in merged if (d := days_until(t.deadline)) is None or d >= 0]
    result.tenders = merged
    return result


async def fetch_recent_tenders(client: httpx.AsyncClient, days: int = 7, **_: Any) -> list[Tender]:
    """Contracts Finder only. Kept for callers of version 0.1."""
    tenders, _truncated = await fetch_contracts_finder(client, days=days)
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
