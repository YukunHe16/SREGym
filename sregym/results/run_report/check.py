"""Measure a labeller against a check set of items whose reference answers were checked by hand.

    python -m sregym.results.run_report.check checkset.jsonl --labeller jev --out /tmp/check

Use it after changing the wording of a question in ``labels.py``, or before trusting another model as the labeller.
Each item is asked in its own request, with the same questions the report asks. (In a report, nearby entries of
one run share a request.)

A check-set line:
    {"id": "o001", "kind": "outputs" | "commands" | "env",
     "source": {"problem": ..., "idx": ...},
     "shown": {"true_fault": ..., "command": ..., "output": ...}   # commands: {"stage": ..., "command": ...}
     "reference": {"true": "yes" | "no" | "borderline" | null, ...},
     "stratum": ..., "stratum_population": N}

Items were drawn by stratum, so each item counts for N/n items of its stratum, where n is the number of items of
the stratum used for the question. A stratum_population of 0 marks an item picked by hand. It is asked but not
counted.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path

from .labeller import Labeller, LabellerError, LabellerUnusable, make_labeller, together
from .labels import MAYBE, SETTING, SURE_CHOICE, YES, command_questions, output_questions

YES_NO = {"outputs": {"true": "t", "elsewhere": "e"}, "commands": {"benchmark": "b", "internet": "n"}}
CHOICE = {"commands": ("change", "c"), "env": ("relation", "r")}


def ask_item(item: dict, labeller: Labeller) -> dict:
    """Return the labeller's answers for one item, keyed like ``reference``. A yes/no answer is a score; other answers
    are (choice, probability)."""
    shown, idx = item["shown"], item["source"]["idx"]
    if item["kind"] == "commands":
        state = {"setting": SETTING, "problem": item["source"]["problem"], "commands": [{"id": idx, **shown}]}
        questions = command_questions(idx)
    else:
        entry = {"id": idx, "command": shown["command"], "output": shown["output"]}
        state = {"setting": SETTING, "true_fault": shown["true_fault"], "entries": [entry]}
        questions = output_questions(idx, "te" if item["kind"] == "outputs" else "r")
    answers = labeller.ask(state, questions)
    said = {field: answers[f"{letter}{idx}"]["noul"] for field, letter in YES_NO.get(item["kind"], {}).items()}
    if item["kind"] in CHOICE:
        field, letter = CHOICE[item["kind"]]
        answer = answers[f"{letter}{idx}"]
        said[field] = (answer["choice"], (answer.get("probabilities") or {}).get(answer["choice"], 1.0))
    return said


def yes_no_figures(rows: list[dict], threshold: float) -> dict:
    """Accuracy of yes/no answers, weighted back to the population.

    Each row has "score", "reference" (bool), "stratum" and "population".
    """
    used = Counter(json.dumps(r["stratum"]) for r in rows)
    cells, sample = Counter(), Counter()
    for r in rows:
        said = r["score"] >= threshold
        cell = ("t" if said == r["reference"] else "f") + ("p" if said else "n")
        cells[cell] += r["population"] / used[json.dumps(r["stratum"])]
        sample[cell] += 1
    flagged, positives = cells["tp"] + cells["fp"], cells["tp"] + cells["fn"]
    return {
        "precision": round(cells["tp"] / flagged, 3) if flagged else None,
        "recall": round(cells["tp"] / positives, 3) if positives else None,
        "sample": dict(sample),
    }


def measure(items: list[dict], labeller: Labeller) -> dict:
    """Ask the labeller about every item and return the accuracy figures."""

    def ask(item: dict) -> dict | None:
        """Ask about one item. Stop the whole check if the labeller can no longer be used."""
        try:
            return ask_item(item, labeller)
        except LabellerUnusable:
            raise  # otherwise every later item would count as unanswered
        except LabellerError:
            return None

    answers = dict(zip((item["id"] for item in items), together(ask, items, labeller.workers), strict=True))
    asked = {item_id: answer for item_id, answer in answers.items() if answer is not None}
    failed = len(answers) - len(asked)
    results: dict = {"items": len(items), "not_answered": failed, "questions": {}}
    for kind, fields in YES_NO.items():
        for field in fields:
            rows = [
                {
                    "id": item["id"],
                    "score": asked[item["id"]][field],
                    "reference": item["reference"][field] == "yes",
                    "stratum": item["stratum"],
                    "population": item["stratum_population"],
                }
                for item in items
                if item["kind"] == kind
                and item["id"] in asked
                and item["stratum_population"]
                and item["reference"].get(field) in ("yes", "no")
            ]
            if rows:
                results["questions"][field] = {
                    "items_used": len(rows),
                    f"at_{YES}": yes_no_figures(rows, YES),
                    f"at_{MAYBE}": yes_no_figures(rows, MAYBE),
                    f"wrong_at_{YES}": sorted(r["id"] for r in rows if (r["score"] >= YES) != r["reference"]),
                }
    for kind, (field, _) in CHOICE.items():
        pairs = [
            (item["id"], item["reference"][field], *asked[item["id"]][field])
            for item in items
            if item["kind"] == kind
            and item["id"] in asked
            and item["stratum_population"]
            and item["reference"].get(field) not in (None, "borderline")
        ]
        if pairs:
            sure = [(i, ref, choice) for i, ref, choice, p in pairs if p >= SURE_CHOICE or choice == "read_only"]
            agreed = sum(ref == choice for _, ref, choice in sure)
            results["questions"][field] = {
                "items_used": len(pairs),
                "answered_with_confidence": len(sure),
                "agreement_when_confident": round(agreed / len(sure), 3) if sure else None,
                "confidently_wrong": sorted(i for i, ref, choice in sure if ref != choice),
            }
    return results


def main(argv: list[str] | None = None) -> int:
    """Command-line entry point."""
    parser = argparse.ArgumentParser(prog="python -m sregym.results.run_report.check", description=__doc__)
    parser.add_argument("checkset", type=Path)
    parser.add_argument("--labeller", required=True, help="jev | jev:<model> | litellm:<model>")
    parser.add_argument("--key-file", type=Path)
    parser.add_argument("--out", type=Path, required=True, help="answers are cached here; results are written here")
    parser.add_argument("--limit", type=int)
    parser.add_argument("--labeller-effort", default=None, help="as for the report")
    parser.add_argument("--workers", type=int, default=6, help="requests out at once, as for the report")
    args = parser.parse_args(argv)

    items = [json.loads(line) for line in args.checkset.open(encoding="utf-8") if line.strip()][: args.limit]
    args.out.mkdir(parents=True, exist_ok=True)
    labeller = make_labeller(
        args.labeller, args.key_file, args.out / "label_cache.jsonl", args.workers, args.labeller_effort
    )
    if labeller is None:
        parser.error("--labeller none has nothing to measure")
    try:
        measured = measure(items, labeller)
    except LabellerUnusable as exc:
        print(f"stopped: the labeller cannot be used ({exc}); its answers are cached in {args.out}", file=sys.stderr)
        return 3
    results = {"labeller": labeller.name, **measured, "labeller_stats": labeller.stats()}
    (args.out / "check_results.json").write_text(json.dumps(results, ensure_ascii=False, indent=1), encoding="utf-8")
    for field, block in results["questions"].items():
        figures = block.get(f"at_{YES}") or {k: v for k, v in block.items() if k != "confidently_wrong"}
        print(f"{field:10s} n={block['items_used']:4d}  {figures}")
    print(results["labeller_stats"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
