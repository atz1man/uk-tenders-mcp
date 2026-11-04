"""Typed records returned by the tools. Every record carries the URL it came from."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Literal

from pydantic import BaseModel, Field


def days_until(iso: str | None, now: datetime | None = None) -> int | None:
    """Whole days from now until an ISO timestamp; negative once it has passed. None if unknown."""
    if not iso:
        return None
    try:
        when = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    except ValueError:
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    now = now or datetime.now(timezone.utc)
    return int((when - now).total_seconds() // 86400)


class Tender(BaseModel):
    ocid: str
    title: str
    buyer: str | None = None
    description: str = ""
    value_gbp: float | None = None
    published: str | None = None
    deadline: str | None = None
    cpv_codes: list[str] = Field(default_factory=list)
    category: str | None = None  # works / services / goods, as the buyer classed it
    region: str | None = None  # display label: one region, a short list, "England-wide" or "UK-wide"
    regions: list[str] = Field(default_factory=list)
    sme_suitable: bool | None = None
    source: str = "Contracts Finder"
    also_listed_on: list[str] = Field(default_factory=list)
    source_url: str

    def listing(self, now: datetime | None = None, description_chars: int = 300) -> dict[str, Any]:
        """Compact view for a model's context: the fields a bid decision starts from."""
        desc = self.description if len(self.description) <= description_chars else (
            self.description[:description_chars].rsplit(" ", 1)[0] + "…")
        return {
            "title": self.title,
            "buyer": self.buyer,
            "value_gbp": self.value_gbp,
            "deadline": self.deadline,
            "days_left": days_until(self.deadline, now),
            "category": self.category,
            "region": self.region,
            "sme_suitable": self.sme_suitable,
            "source": self.source,
            "also_listed_on": self.also_listed_on,
            "description": desc,
            "source_url": self.source_url,
        }


class Company(BaseModel):
    number: str
    name: str
    status: str | None = None
    incorporated: str | None = None
    locality: str | None = None
    postcode: str | None = None
    sic_codes: list[str] = Field(default_factory=list)
    has_insolvency_history: bool = False
    accounts_overdue: bool = False
    source_url: str


class Verdict(BaseModel):
    ocid: str
    title: str
    verdict: Literal["GO", "MAYBE", "NO-GO"]
    score: int
    reasons: list[str]
    buyer: str | None = None
    value_gbp: float | None = None
    deadline: str | None = None
    days_left: int | None = None
    region: str | None = None
    source: str = "Contracts Finder"
    source_url: str
