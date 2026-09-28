"""python -m sregym.results.run_report <results dir or trajectory.json> --out <dir> [options]

Reads saved SREGym runs (ATIF trajectory.json plus the scored CSV next to it) and writes, per run,
run_report.json and run_report.md, and for the whole directory summary.csv and summary.md. A run folder without a
trajectory.json (runs from before SREGym wrote one) is converted in memory from its session files.
Nothing here touches a cluster or re-runs an agent.
"""

from __future__ import annotations

import argparse
import contextlib
import csv
import json
import sys
import time
from pathlib import Path

from .build import SUMMARY_COLUMNS, build_report, summary_row
from .labeller import GROUPS, CacheMiss, LabellerError, LabellerUnusable, make_labellers, together
from .usage import UsageLedger
from .render import readme_markdown, run_markdown, suite_markdown
from .scored import load_root_causes
from .trajectory import convert_run_folder, find_trajectories, run_folders_without_trajectory


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m sregym.results.run_report", description=__doc__.split("\n\n")[1])
    parser.add_argument(
        "path",
        type=Path,
        help="a results directory (searched for trajectory.json, else for any ATIF file; a run_<n> folder without "
        "one is converted in memory from its session files) or one trajectory file",
    )
    parser.add_argument(
        "--out", type=Path, required=True, help="where reports are written; the input is never modified"
    )
    parser.add_argument(
        "--root-causes",
        default="auto",
        help="where the ground-truth root causes come from (the clue timeline needs them): 'auto' reads them from "
        "SREGym's problem definitions as checked out now, a path names a JSON file, 'none' skips them",
    )
    parser.add_argument("--labeller", default="none", help="none | jev | jev:<model> | litellm:<model> (an API) | codex:<model> (a ChatGPT subscription) | "
        "claudecode:<model> (a Claude subscription); see docs/run-report.md, Connecting a model")
    parser.add_argument(
        "--labeller-for",
        action="append",
        default=[],
        metavar="GROUP=SPEC",
        help=f"a labeller of its own for one group of questions ({', '.join(GROUPS)}), e.g. "
        "fix=litellm:openai/deepseek-flash; the other groups keep --labeller. Repeatable",
    )
    parser.add_argument(
        "--key-file",
        type=Path,
        default=None,
        help="KEY=value file holding the labeller credentials, if they are not in the environment",
    )
    parser.add_argument(
        "--labeller-effort",
        default=None,
        help="the reasoning effort asked of the model (low, medium, high, or what the provider accepts); not set, "
        "the provider's default applies (medium for codex:). The report records which it was",
    )
    parser.add_argument(
        "--workers",
        type=int,
        default=6,
        help="how many requests to the labeller may be out at once (1: one after the other). What is asked, and "
        "so the report, does not depend on it",
    )
    parser.add_argument(
        "--chat-workers",
        type=int,
        default=None,
        help="the same for a litellm labeller, which reasons at length on every request: deepseek-flash kept 16 "
        "at once as fast as one (81 requests in 79 s); not set, --workers",
    )
    parser.add_argument("--cache-only", action="store_true", help="replay complete v2 requests without credentials or backend calls; missing/ambiguous cache exits nonzero")
    parser.add_argument("--allow-new-requests", action="store_true", help="explicitly allow new requests despite legacy question-cache rows; may incur model cost")
    parser.add_argument("--lang", choices=("en", "zh"), default="en")
    parser.add_argument("--limit", type=int, default=0, help="only the first N runs")
    args = parser.parse_args(argv)

    # each run: its trajectory file, and the run itself when it had to be converted in memory
    runs: list[tuple[Path, dict | None]] = [(path, None) for path in find_trajectories(args.path)]
    skipped = []
    for folder in run_folders_without_trajectory(args.path):
        data, why = convert_run_folder(folder)
        if data is None:
            skipped.append({"run": str(folder), "why": why})
            print(f"skipped {folder}: {why}", file=sys.stderr)
        else:
            runs.append((folder / "trajectory.json", data))
    if args.limit:
        runs = runs[: args.limit]
    if not runs:
        print(f"no readable run under {args.path}: no trajectory.json or other ATIF file", file=sys.stderr)
        return 2
    args.out.mkdir(parents=True, exist_ok=True)
    try:
        labeller = make_labellers(
            args.labeller,
            args.labeller_for,
            args.key_file,
            args.out,
            args.workers,
            args.labeller_effort,
            chat_workers=args.chat_workers,
            cache_only=args.cache_only,
            allow_new_requests=args.allow_new_requests,
        )
    except LabellerError as exc:
        print(f"labeller unavailable: {exc}", file=sys.stderr)
        return 2
    ledger = UsageLedger(args.out / "labeller_stats.json", labeller) if labeller else None
    if labeller:
        names = ", ".join(sorted({one.name for one in labeller.distinct()}))
        offline = all(getattr(one, "cache_only", False) for one in labeller.distinct())
        print(
            f"cache-only replay ({names}): no backend requests permitted; missing complete requests stop the build."
            if offline else
            f"note: command text and command output of these runs are sent to the labeller ({names}); "
            "strings shaped like credentials are masked first, nothing else is.",
            file=sys.stderr,
        )
    if args.root_causes == "auto":
        from .root_causes import offline_root_causes
        from .scored import find_result_row
        from .trajectory import load_run

        wanted = set()
        for trajectory, data in runs:
            with contextlib.suppress(Exception):  # the same identity build_report gives the run
                run = load_run(trajectory, data)
                row = find_result_row(trajectory.parent, run.problem_id, run.sregym.get("run"), trajectory)[0] or {}
                wanted.add(run.problem_id or row.get("problem_id") or trajectory.parent.name)
        root_causes, unread = offline_root_causes(wanted)
        for problem_id, why in unread.items():
            print(f"no ground truth for {problem_id}: {why}", file=sys.stderr)
    else:
        root_causes = load_root_causes(None if args.root_causes == "none" else Path(args.root_causes))

    def build(item: tuple[Path, dict | None]) -> dict | Exception:
        try:
            return build_report(item[0], root_causes, labeller, item[1], args.lang)
        except LabellerUnusable:
            raise  # every run after this one would lack what the labeller answers
        except Exception as exc:  # one unreadable run must not stop the rest
            return exc

    # Several runs are built at a time, because most of building one is waiting for the labeller; the labeller
    # itself keeps the requests of all of them together within --workers. Reports come back in the order given.
    started, reports, written = time.monotonic(), [], set()
    earlier = _earlier_reports(args.out)  # read once: the folders an earlier build wrote, by the run they are of
    try:
        for (trajectory, _), report in zip(
            runs, together(build, runs, labeller.workers if labeller else 1), strict=True
        ):
            if ledger:
                ledger.save()
            if isinstance(report, Exception):
                # listed with the others that could not be read, so the summary says a run is missing; and a report an
                # earlier build left for it goes, or it would be read as this build's
                why = f"the report could not be built ({type(report).__name__}: {str(report)[:300]})"
                skipped.append({"run": str(trajectory), "why": why})
                removed = _drop_earlier_report(args.out, trajectory, earlier)
                gone = f"; removed the report an earlier build left for it in {', '.join(removed)}" if removed else ""
                print(f"skipped {trajectory}: {why}{gone}", file=sys.stderr)
                continue
            name = report["header"]["problem_id"] or trajectory.parent.name
            attempt = report["header"].get("attempt")
            folder = f"{name}__run{attempt}" if attempt not in (None, "", 1, "1") else name
            while folder in written:  # two runs that nothing tells apart must not share one report
                folder += "_again"
            written.add(folder)
            # what an earlier build wrote for this run goes first, wherever it is: folder names follow the order the
            # runs come in (name, name_again), and an old copy left beside the new one reads as a second run
            _drop_earlier_report(args.out, trajectory, earlier)
            target = args.out / folder
            target.mkdir(parents=True, exist_ok=True)
            (target / "run_report.json").write_text(
                json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
            )
            try:
                page = run_markdown(report, args.lang)
            except Exception as exc:  # one page that cannot be written must not stop the others and the summary
                why = f"the Markdown could not be written ({type(exc).__name__}: {str(exc)[:300]}); run_report.json was"
                skipped.append({"run": str(trajectory), "why": why})
                print(f"{trajectory}: {why}", file=sys.stderr)
                page = f"# {name}\n\n{why}.\n"
            (target / "run_report.md").write_text(page, encoding="utf-8")
            reports.append(report)
            clues = report["clues"]
            print(
                f"{name[:48]:48s} diag={report['header']['diagnosis_pass']!s:5} "
                f"first_clue={clues.get('first_clue_step')!s:>4} flags={len(report['flags'])}",
                flush=True,
            )
    except LabellerUnusable as exc:
        if ledger:
            ledger.save("cache_incomplete" if isinstance(exc, CacheMiss) else "failed")
        print(
            f"stopped: {exc}. {len(reports)} reports were written; complete v2 answers remain cached. "
            "Old per-question rows answer a request only when they hold all of it. Use --cache-only for offline replay; "
            "--allow-new-requests explicitly permits paid cache misses.",
            file=sys.stderr,
        )
        return 3
    except BaseException:
        if ledger:
            ledger.save("failed")
        raise

    with (args.out / "summary.csv").open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=SUMMARY_COLUMNS)
        writer.writeheader()
        writer.writerows(summary_row(r) for r in reports)
    (args.out / "summary.md").write_text(suite_markdown(reports, args.lang, skipped), encoding="utf-8")
    (args.out / "README.md").write_text(readme_markdown(args.lang), encoding="utf-8")
    if skipped:
        (args.out / "skipped_runs.json").write_text(json.dumps(skipped, indent=2) + "\n", encoding="utf-8")
    else:  # an earlier build's list would say runs are missing that are not
        (args.out / "skipped_runs.json").unlink(missing_ok=True)
    if ledger:
        ledger.save("completed_with_skips" if skipped else "completed")
    print(f"{len(reports)} reports -> {args.out}")
    return 0


def _earlier_reports(out: Path) -> dict[str, list[Path]]:
    """The reports an earlier build left under ``out``, by their run (``source.trajectory``)."""
    found: dict[str, list[Path]] = {}
    for path in sorted(out.glob("*/run_report.json")):
        try:
            source = json.loads(path.read_text(encoding="utf-8")).get("source") or {}
        except (OSError, ValueError, AttributeError):
            continue
        if source.get("trajectory"):
            found.setdefault(source["trajectory"], []).append(path.parent)
    return found


def _drop_earlier_report(out: Path, trajectory: Path, earlier: dict[str, list[Path]] | None = None) -> list[str]:
    """Remove the report an earlier build wrote under ``out`` for this run (its ``source.trajectory``); the folders it
    was in."""
    earlier = _earlier_reports(out) if earlier is None else earlier
    removed = []
    for folder in earlier.pop(str(trajectory), []):
        for name in ("run_report.json", "run_report.md"):
            (folder / name).unlink(missing_ok=True)
        with contextlib.suppress(OSError):  # a folder that holds nothing else goes too
            folder.rmdir()
        removed.append(folder.name)
    return removed


if __name__ == "__main__":
    raise SystemExit(main())
