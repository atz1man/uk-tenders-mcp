"""Rules-based bid scoring: GO / MAYBE / NO-GO with the reasons shown.

Rules first, because they are cheap, explainable and testable. A learned model
can replace the weights later, using bid/pass decisions as labels.

Every rule change is checked against eval/labelled.jsonl, which includes real notices
that earlier versions scored wrongly.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime

from .models import Tender, Verdict, days_until

GO_AT = 7
MAYBE_AT = 4
# A notice worth this many times the profile's ceiling is programme-scale: out of reach as a prime.
OUT_OF_REACH = 10


@dataclass(frozen=True)
class Profile:
    name: str
    description: str
    keywords: tuple[str, ...]
    cpv_prefixes: tuple[str, ...] = ()
    # Adjacent services (for example civil engineering consultancy): a weaker signal than works codes.
    related_cpv_prefixes: tuple[str, ...] = ()
    required_any: tuple[str, ...] = ()
    exclusions: tuple[str, ...] = ()
    # Signs the notice is a study, a system or a supply contract rather than work this profile does.
    negatives: tuple[str, ...] = ()
    # Points for the buyer's own works / services / goods label.
    categories: dict[str, int] = field(default_factory=dict, compare=False)
    min_value: float | None = None
    max_value: float | None = None
    min_days_to_bid: int = 7
    _patterns: dict[str, re.Pattern[str]] = field(default_factory=dict, compare=False, repr=False)

    def has(self, term: str, text: str) -> bool:
        """Whole-word match, so 'rail' does not match 'trail' or 'guardrail'."""
        pattern = self._patterns.get(term)
        if pattern is None:
            pattern = re.compile(rf"(?<![a-z]){re.escape(term.lower())}(?![a-z])")
            self._patterns[term] = pattern
        return bool(pattern.search(text))


PROFILES: dict[str, Profile] = {
    "rail_civils": Profile(
        name="rail_civils",
        description="Rail and civils minor works: track, structures, bridges, drainage, earthworks, stations.",
        # 'track' alone matched "track record"; only rail-specific track terms count.
        keywords=("railway", "rail", "permanent way", "track renewal", "track works", "railway track", "bridge", "footbridge", "culvert", "drainage", "foul water",
                  "surface water", "embankment", "earthworks", "retaining wall", "station platform",
                  "railway platform", "platform works", "railway station", "level crossing",
                  "civil engineering", "civils", "resurfacing", "structures"),
        # 4520 general civil works, 4522-4524 bridges/pipelines/drainage/roads/rail, 45111 earthworks.
        cpv_prefixes=("4520", "4522", "4523", "4524", "45111"),
        related_cpv_prefixes=("71311", "71312"),
        # Bare 'station' is not an anchor: ambulance, fire, police and bus stations kept coming through.
        required_any=("rail", "railway", "bridge", "footbridge", "civil", "civils", "culvert", "drainage",
                      "foul water", "earthworks", "retaining wall"),
        exclusions=("rolling stock", "catering", "cleaning services", "software licence", "ticketing system"),
        negatives=("feasibility study", "consultancy", "software", "management system", "data migration",
                   "hosting", "research", "case studies", "devices", "supply of", "wayfinding"),
        categories={"works": 2, "services": 0, "goods": -3},
        min_value=25_000,
        max_value=5_000_000,
    ),
    "fire_safety": Profile(
        name="fire_safety",
        description="Passive fire protection, fire doors, compartmentation and fire risk remedials.",
        keywords=("fire door", "fire doors", "fire stopping", "firestopping", "compartmentation", "fire risk",
                  "passive fire", "intumescent", "fire safety", "fire damper", "fire dampers"),
        cpv_prefixes=("4534", "35111", "71317", "45421131"),
        required_any=("fire",),
        exclusions=("fire engine", "fire appliance", "firefighting vehicle", "training course",
                    "fire station"),
        negatives=("software", "training", "consultancy", "supply of", "research"),
        categories={"works": 2, "services": 0, "goods": -2},
        min_value=10_000,
        max_value=3_000_000,
    ),
}


def score_tender(tender: Tender, profile: Profile, now: datetime | None = None) -> Verdict:
    text = f"{tender.title} {tender.description}".lower()
    reasons: list[str] = []
    days_left = days_until(tender.deadline, now)

    def verdict(v: str, s: int) -> Verdict:
        return Verdict(ocid=tender.ocid, title=tender.title, verdict=v, score=s, reasons=reasons,
                       buyer=tender.buyer, value_gbp=tender.value_gbp, deadline=tender.deadline,
                       days_left=days_left, region=tender.region, source=tender.source,
                       source_url=tender.source_url)

    excluded = [e for e in profile.exclusions if profile.has(e, text)]
    if excluded:
        reasons.append(f"Excluded term: {', '.join(excluded)}")
        return verdict("NO-GO", 0)

    if profile.required_any and not any(profile.has(a, text) for a in profile.required_any):
        reasons.append("No anchor term for this profile")
        return verdict("NO-GO", 0)

    if profile.max_value and tender.value_gbp and tender.value_gbp > OUT_OF_REACH * profile.max_value:
        reasons.append(f"Programme-scale value, £{tender.value_gbp:,.0f}: more than {OUT_OF_REACH}x the "
                       f"£{profile.max_value:,.0f} ceiling. Worth a look only as a subcontractor.")
        return verdict("NO-GO", 0)

    title = tender.title.lower()
    hits = [k for k in profile.keywords if profile.has(k, text)]
    cpv_hits = [c for c in tender.cpv_codes if c.startswith(profile.cpv_prefixes)] if profile.cpv_prefixes else []
    anchor_in_title = any(profile.has(a, title) for a in profile.required_any)
    if not (anchor_in_title or cpv_hits or len(hits) >= 2):
        # One passing mention deep in a description ("drainage" in a festival notice) is not a signal.
        reasons.append("Profile terms appear only in passing: not in the title, no works CPV, under two keywords")
        return verdict("NO-GO", 0)

    score = 0
    if hits:
        gained = min(6, 2 * len(hits))
        score += gained
        reasons.append(f"Keywords (+{gained}): {', '.join(hits[:5])}")

    if cpv_hits:
        score += 4
        reasons.append(f"CPV match (+4): {', '.join(cpv_hits[:3])}")
    elif profile.related_cpv_prefixes:
        related = [c for c in tender.cpv_codes if c.startswith(profile.related_cpv_prefixes)]
        if related:
            score += 2
            reasons.append(f"Related services CPV (+2): {', '.join(related[:3])}")

    if tender.category and tender.category in profile.categories:
        points = profile.categories[tender.category]
        if points:
            score += points
            reasons.append(f"Buyer classes it as {tender.category} ({points:+d})")

    negs = [n for n in profile.negatives if profile.has(n, text)]
    if negs and not cpv_hits and tender.category != "works":
        reasons.append(f"A study, system or supply contract, with no works CPV or works category: {', '.join(negs[:3])}")
        return verdict("NO-GO", 0)
    if negs:
        lost = min(6, 3 * len(negs))
        score -= lost
        reasons.append(f"Looks like a study, system or supply contract (-{lost}): {', '.join(negs[:3])}")

    if tender.value_gbp is None:
        reasons.append("No value published")
    else:
        low = profile.min_value or 0
        high = profile.max_value or float("inf")
        if low <= tender.value_gbp <= high:
            score += 2
            reasons.append(f"Value in range (+2): £{tender.value_gbp:,.0f}")
        else:
            score -= 3
            reasons.append(f"Value out of range (-3): £{tender.value_gbp:,.0f}")

    if tender.sme_suitable:
        score += 1
        reasons.append("Buyer marks it suitable for SMEs (+1)")

    if days_left is not None and days_left < profile.min_days_to_bid:
        score -= 2
        reasons.append(f"Closes in {max(days_left, 0)} days, little time to bid (-2)")

    if score >= GO_AT:
        return verdict("GO", score)
    if score >= MAYBE_AT:
        return verdict("MAYBE", score)
    return verdict("NO-GO", score)
