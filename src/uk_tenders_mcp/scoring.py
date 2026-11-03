"""Rules-based bid scoring: GO / MAYBE / NO-GO with the reasons shown.

Rules first, because they are cheap, explainable and testable. A learned model
can replace the weights later, using bid/pass decisions as labels.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from .models import Tender, Verdict

GO_AT = 7
MAYBE_AT = 4


@dataclass(frozen=True)
class Profile:
    name: str
    description: str
    keywords: tuple[str, ...]
    cpv_prefixes: tuple[str, ...] = ()
    required_any: tuple[str, ...] = ()
    exclusions: tuple[str, ...] = ()
    min_value: float | None = None
    max_value: float | None = None
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
        description="Rail and civils minor works: track, structures, drainage, stations.",
        keywords=("railway", "rail", "track", "bridge", "culvert", "drainage", "embankment",
                  "earthworks", "station platform", "railway platform", "platform works", "station",
                  "retaining wall", "civil engineering"),
        cpv_prefixes=("4523", "4524", "45221", "45234", "71311"),
        required_any=("rail", "railway", "bridge", "civil", "culvert", "drainage", "station"),
        exclusions=("rolling stock", "catering", "cleaning services", "software licence", "ticketing system"),
        min_value=25_000,
        max_value=5_000_000,
    ),
    "fire_safety": Profile(
        name="fire_safety",
        description="Passive fire protection, fire doors, compartmentation and fire risk remedials.",
        keywords=("fire door", "fire stopping", "firestopping", "compartmentation", "fire risk",
                  "passive fire", "intumescent", "fire safety"),
        cpv_prefixes=("4534", "35111", "71317"),
        required_any=("fire",),
        exclusions=("fire engine", "fire appliance", "firefighting vehicle", "training course"),
        min_value=10_000,
        max_value=3_000_000,
    ),
}


def score_tender(tender: Tender, profile: Profile) -> Verdict:
    text = f"{tender.title} {tender.description}".lower()
    reasons: list[str] = []

    def verdict(v: str, s: int) -> Verdict:
        return Verdict(ocid=tender.ocid, title=tender.title, verdict=v, score=s,
                       reasons=reasons, source_url=tender.source_url)

    excluded = [e for e in profile.exclusions if profile.has(e, text)]
    if excluded:
        reasons.append(f"Excluded term: {', '.join(excluded)}")
        return verdict("NO-GO", 0)

    if profile.required_any and not any(profile.has(a, text) for a in profile.required_any):
        reasons.append("No anchor term for this profile")
        return verdict("NO-GO", 0)

    score = 0
    hits = [k for k in profile.keywords if profile.has(k, text)]
    if hits:
        gained = min(6, 2 * len(hits))
        score += gained
        reasons.append(f"Keywords ({gained}): {', '.join(hits[:5])}")

    cpv_hits = [c for c in tender.cpv_codes if c.startswith(profile.cpv_prefixes)] if profile.cpv_prefixes else []
    if cpv_hits:
        score += 4
        reasons.append(f"CPV match (4): {', '.join(cpv_hits)}")

    if tender.value_gbp is None:
        reasons.append("No value published")
    else:
        low = profile.min_value or 0
        high = profile.max_value or float("inf")
        if low <= tender.value_gbp <= high:
            score += 2
            reasons.append(f"Value in range (2): £{tender.value_gbp:,.0f}")
        else:
            score -= 3
            reasons.append(f"Value out of range (-3): £{tender.value_gbp:,.0f}")

    if score >= GO_AT:
        return verdict("GO", score)
    if score >= MAYBE_AT:
        return verdict("MAYBE", score)
    return verdict("NO-GO", score)
