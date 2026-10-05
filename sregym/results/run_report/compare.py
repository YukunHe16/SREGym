"""Two sets of run reports side by side: the same problems run twice (another model, effort, agent or day).

    python -m sregym.results.run_report.compare <reports A> <reports B> --out /tmp/cmp [--names high max]

Reads the ``run_report.json`` files and asks no model. There is one row for each problem found on both sides.
"""

from __future__ import annotations

import argparse
import csv
import json
from pathlib import Path

COLUMNS = ["diagnosis_pass", "diagnosis_score", "mitigation_pass", "first_clue_step", "steps_after_first_clue",
           "total_tokens", "benchmark_probing_sure", "on_screen_sure", "fault_switch_sure", "changes_outside"]  # fmt: skip


def load(folder: Path) -> dict[str, dict]:
    """Load reports by problem. A later attempt at the same problem is kept as ``<problem> (run <n>)``."""
    reports = {}
    for path in sorted(folder.rglob("run_report.json")):
        report = json.loads(path.read_text(encoding="utf-8"))
        header = report["header"]
        attempt = header.get("attempt")
        name = header["problem_id"] if str(attempt or 1) == "1" else f"{header['problem_id']} (run {attempt})"
        if name in reports:
            raise SystemExit(f"{folder}: two reports for {name}; give each set of runs its own folder")
        reports[name] = report
    return reports


def facts(report: dict) -> dict:
    """Return the values from one report that the comparison uses."""
    header, clues = report["header"], report["clues"]
    counts = report["command_audit"].get("counts") or {}
    screen = (report.get("benchmark_on_screen") or {}).get("counts") or {}
    return {
        "diagnosis_pass": header.get("diagnosis_pass"),
        "diagnosis_score": header.get("diagnosis_score"),
        "mitigation_pass": header.get("mitigation_pass"),
        "first_clue_step": clues.get("first_clue_step"),
        "steps_after_first_clue": clues.get("steps_after_first_clue"),
        "total_tokens": report["cost_and_time"].get("total_tokens"),
        "benchmark_probing_sure": counts.get("benchmark_probing_sure"),
        "on_screen_sure": screen.get("sure"),
        "fault_switch_sure": screen.get("fault_switch_sure"),
        "changes_outside": counts.get("changes_outside"),
        "flags": set(report.get("flags") or []),
        "traps": {t["trap"]: t for t in report.get("known_traps") or []},
    }


def compare(a: dict[str, dict], b: dict[str, dict], names: tuple[str, str]) -> tuple[list[dict], dict]:
    """Return one row for each problem on both sides, and a summary of pass and fail results."""
    rows = []
    for problem in sorted(set(a) & set(b)):
        fa, fb = facts(a[problem]), facts(b[problem])
        row = {"problem_id": problem}
        for column in COLUMNS:
            row[f"{column}_{names[0]}"], row[f"{column}_{names[1]}"] = fa[column], fb[column]
        row[f"flags_only_{names[0]}"] = ";".join(sorted(fa["flags"] - fb["flags"]))
        row[f"flags_only_{names[1]}"] = ";".join(sorted(fb["flags"] - fa["flags"]))
        row[f"trap_in_diagnosis_{names[0]}"] = ";".join(k for k, t in fa["traps"].items() if t["named_in_diagnosis"])
        row[f"trap_in_diagnosis_{names[1]}"] = ";".join(k for k, t in fb["traps"].items() if t["named_in_diagnosis"])
        rows.append(row)

    def verdicts(stage):
        """Group the problems by their result on each side for one stage."""
        pair = lambda r: (r[f"{stage}_pass_{names[0]}"], r[f"{stage}_pass_{names[1]}"])  # noqa: E731
        return {
            "both_pass": [r["problem_id"] for r in rows if pair(r) == (True, True)],
            "both_fail": [r["problem_id"] for r in rows if pair(r) == (False, False)],
            f"only_{names[0]}_passes": [r["problem_id"] for r in rows if pair(r) == (True, False)],
            f"only_{names[1]}_passes": [r["problem_id"] for r in rows if pair(r) == (False, True)],
        }

    total = lambda name: sum(r[f"total_tokens_{name}"] or 0 for r in rows)  # noqa: E731
    summary = {
        "problems_compared": len(rows),
        "only_in": {names[0]: sorted(set(a) - set(b)), names[1]: sorted(set(b) - set(a))},
        "diagnosis": verdicts("diagnosis"),
        "mitigation": verdicts("mitigation"),
        "total_tokens": {names[0]: total(names[0]), names[1]: total(names[1])},
    }
    return rows, summary


def markdown(rows: list[dict], summary: dict, names: tuple[str, str]) -> str:
    """Render the rows and the summary as Markdown."""
    a, b = names
    mark = lambda v: "-" if v is None else ("pass" if v is True else "fail" if v is False else str(v))  # noqa: E731
    lines = [f"# {a} vs {b}", "", f"{summary['problems_compared']} problems on both sides.", ""]
    for stage in ("diagnosis", "mitigation"):
        v = summary[stage]
        lines.append(
            f"- {stage}: both pass {len(v['both_pass'])}, both fail {len(v['both_fail'])}, "
            f"only {a} passes {len(v[f'only_{a}_passes'])}, only {b} passes {len(v[f'only_{b}_passes'])}"
        )
    lines += [f"- tokens: {a} {summary['total_tokens'][a]:,}, {b} {summary['total_tokens'][b]:,}", ""]
    head = ["problem", f"diagnosis {a}/{b}", f"mitigation {a}/{b}", f"first clue at step {a}/{b}",
            f"steps after it {a}/{b}", f"tokens {a}/{b}", f"benchmark material on screen {a}/{b}", f"flags only in {a}", f"flags only in {b}"]  # fmt: skip
    lines += ["| " + " | ".join(head) + " |", "|" + "---|" * len(head)]
    for r in rows:
        cells = [r["problem_id"]]
        cells += [
            f"{mark(r[f'{column}_{a}'])} / {mark(r[f'{column}_{b}'])}"
            for column in ("diagnosis_pass", "mitigation_pass", "first_clue_step", "steps_after_first_clue")
        ]
        cells.append(" / ".join(f"{(r[f'total_tokens_{n}'] or 0) / 1e6:.1f}M" for n in names))
        cells.append(f"{mark(r[f'on_screen_sure_{a}'])} / {mark(r[f'on_screen_sure_{b}'])}")
        cells += [r[f"flags_only_{a}"].replace(";", ", "), r[f"flags_only_{b}"].replace(";", ", ")]
        lines.append("| " + " | ".join(cells) + " |")
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    """Command-line entry point."""
    parser = argparse.ArgumentParser(prog="python -m sregym.results.run_report.compare", description=__doc__)
    parser.add_argument("a", type=Path)
    parser.add_argument("b", type=Path)
    parser.add_argument("--names", nargs=2, default=None, metavar=("A", "B"))
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args(argv)
    names = tuple(args.names) if args.names else (args.a.name, args.b.name)
    if names[0] == names[1]:
        names = ("a", "b")
    rows, summary = compare(load(args.a), load(args.b), names)
    if not rows:
        print("the two sides have no problem in common")
        return 2
    args.out.mkdir(parents=True, exist_ok=True)
    with (args.out / "comparison.csv").open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
    (args.out / "comparison.json").write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
    (args.out / "comparison.md").write_text(markdown(rows, summary, names), encoding="utf-8")
    print(json.dumps({k: v for k, v in summary.items() if k != "only_in"}, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
