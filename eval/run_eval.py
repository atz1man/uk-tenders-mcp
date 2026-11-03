"""Evaluate the scoring rules against a labelled set before changing them.

Run:  python eval/run_eval.py [--profile rail_civils] [--min-accuracy 0.8]
Exits non-zero if accuracy drops below the threshold, or if any NO-GO case is scored GO
(a false GO wastes bid effort, so it is treated as a hard failure).
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path

from uk_tenders_mcp.models import Tender
from uk_tenders_mcp.scoring import PROFILES, score_tender

LABELS = ("GO", "MAYBE", "NO-GO")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--profile", default="rail_civils")
    parser.add_argument("--data", default=str(Path(__file__).parent / "labelled.jsonl"))
    parser.add_argument("--min-accuracy", type=float, default=0.8)
    args = parser.parse_args()

    profile = PROFILES[args.profile]
    rows = [json.loads(line) for line in Path(args.data).read_text().splitlines() if line.strip()]
    confusion: Counter[tuple[str, str]] = Counter()
    misses = []
    for i, row in enumerate(rows, 1):
        t = Tender(ocid=f"eval-{i}", title=row["title"], description=row["description"],
                   value_gbp=row["value"], cpv_codes=row["cpv"], source_url="eval")
        got = score_tender(t, profile).verdict
        confusion[(row["expected"], got)] += 1
        if got != row["expected"]:
            misses.append((row["title"], row["expected"], got))

    correct = sum(n for (e, g), n in confusion.items() if e == g)
    accuracy = correct / len(rows)
    false_go = confusion[("NO-GO", "GO")]

    print(f"Profile: {profile.name}   Cases: {len(rows)}   Accuracy: {accuracy:.0%}   False GO: {false_go}\n")
    print("expected \\ got   " + "".join(f"{g:>8}" for g in LABELS))
    for e in LABELS:
        print(f"{e:<16}" + "".join(f"{confusion[(e, g)]:>8}" for g in LABELS))
    if misses:
        print("\nMisses:")
        for title, exp, got in misses:
            print(f"  - {title}: expected {exp}, got {got}")

    if false_go or accuracy < args.min_accuracy:
        print("\nFAIL")
        return 1
    print("\nPASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
