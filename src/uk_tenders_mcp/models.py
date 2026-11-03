"""Typed records returned by the tools. Every record carries the URL it came from."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class Tender(BaseModel):
    ocid: str
    title: str
    buyer: str | None = None
    description: str = ""
    value_gbp: float | None = None
    published: str | None = None
    deadline: str | None = None
    cpv_codes: list[str] = Field(default_factory=list)
    source_url: str


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
    source_url: str
