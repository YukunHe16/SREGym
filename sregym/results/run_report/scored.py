"""Find the scored CSV row that SREGym wrote for a trajectory."""

from __future__ import annotations

import ast
import csv
import json
import sys
from pathlib import Path
from typing import Any

csv.field_size_limit(sys.maxsize)


def _bool(value: Any) -> bool | None:
    """Parse a yes/no value. None if it is neither."""
    text = str(value if value is not None else "").strip().lower()
    if text in {"true", "1", "yes"}:
        return True
    if text in {"false", "0", "no"}:
        return False
    return None


def _number(value: Any) -> float | None:
    """Parse a number. None if the value is not one."""
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _sum_of(*values: Any) -> int | None:
    """Return the sum as an int, or None if any value is missing."""
    numbers = [_number(v) for v in values]
    return int(sum(numbers)) if all(n is not None for n in numbers) else None


def _literal(value: Any) -> Any:
    """Parse a Python or JSON literal from a CSV cell. None if it cannot be parsed."""
    if not value:
        return None
    try:
        return ast.literal_eval(value)
    except (ValueError, SyntaxError):
        try:
            return json.loads(value)
        except (TypeError, ValueError):
            return None


def find_result_row(
    run_dir: Path, problem_id: str | None, attempt=None, trajectory: Path | None = None
) -> tuple[dict | None, Path | None]:
    """Find the CSV row for this run, in the run directory or up to two levels above it.

    SREGym writes one CSV per attempt into the run directory, so that file is checked first. A CSV higher up has one row
    per attempt, and the row for ``attempt`` is used. If the rows give attempt numbers and none matches, no row is
    returned, so another attempt's scores are never used. If the rows give no attempt number, a single matching row is
    used, but not one of several. Without a problem id, only a CSV with a single row is used.

    A plain ``results.csv`` is the table written by another harness around SREGym (see ``parse_row``). If its rows name
    their trajectory file in a ``trajectory`` column, relative to the table, the row for ``trajectory`` is used, and a
    table that names only other files is skipped.
    """
    candidates: list[Path] = []
    for directory in (run_dir, run_dir.parent, run_dir.parent.parent):
        candidates += sorted(directory.glob("scored.csv")) + sorted(directory.glob("*_results.csv"))
        candidates += sorted(directory.glob("results.csv"))
    for path in candidates:
        try:
            with path.open(encoding="utf-8", errors="replace", newline="") as handle:
                rows = list(csv.DictReader(handle))
        except (OSError, csv.Error):
            continue
        if trajectory is not None and any(r.get("trajectory") for r in rows):
            own = [r for r in rows if r.get("trajectory") and _same_file(path.parent / r["trajectory"], trajectory)]
            if own:
                return own[-1], path
            continue
        if problem_id:
            matching = [r for r in rows if r.get("problem_id") == problem_id]
        else:
            matching = rows if len(rows) == 1 else []
        if attempt is None:
            if matching:
                return matching[-1], path
            continue
        numbered = [r for r in matching if _attempt(r.get("attempt")) is not None]
        same = [r for r in numbered if _attempt(r.get("attempt")) == _attempt(attempt)]
        if same:
            return same[-1], path
        if not numbered and len(matching) == 1:
            return matching[0], path
    return None, None


def _same_file(a: Path, b: Path) -> bool:
    """Check whether two paths point to the same file."""
    try:
        return a.resolve() == b.resolve()
    except OSError:
        return False


def _attempt(value) -> str | None:
    """Return an attempt number as text, so ``1.0`` and ``1`` are the same. None if none is given."""
    text = str(value if value is not None else "").strip()
    if not text:
        return None
    number = _number(text)
    return str(int(number)) if number is not None and number == int(number) else text


def _parse_harness_row(row: dict) -> dict:
    """Parse a row of the ``results.csv`` written by another harness around SREGym.

    It has one row per run, with ``diag_success``, ``mitigation_success``, ``composite_score``, ``ttl``, ``ttm`` and
    ``effort``. It has neither the diagnosis text nor the judge's checklist, so those are marked as not recorded rather
    than left empty.
    """
    return {
        "problem_id": row.get("problem_id"),
        "attempt": None,
        "run_status": None,
        "incomplete_reason": "timed_out" if _bool(row.get("timed_out")) else None,
        "judge_backend": None,
        "deployment_profile": None,
        "internet_access": None,
        "reasoning_effort": row.get("effort") or None,
        "total_tokens": _sum_of(row.get("input_tokens"), row.get("output_tokens")),
        "ttl_s": _number(row.get("ttl")),
        "ttm_s": _number(row.get("ttm")),
        "diagnosis": {
            "success": _bool(row.get("diag_success")),
            "score": _number(row.get("composite_score")),
            "submission": None,
            "checklist": [],
            "checklist_recorded": False,
        },
        "mitigation": {
            "success": _bool(row.get("mitigation_success")),
            "failure_class": None,
            "reason": None,
            "detail": None,
        },
    }


def parse_row(row: dict | None) -> dict:
    """Return the parts of the scored row that the report uses, with their types."""
    if not row:
        return {}
    if "diag_success" in row and "Diagnosis.success" not in row:
        return _parse_harness_row(row)
    checklist = _literal(row.get("Diagnosis.checklist")) or []
    return {
        "problem_id": row.get("problem_id"),
        "attempt": row.get("attempt"),
        "run_status": row.get("run_status"),
        "incomplete_reason": row.get("incomplete_reason") or None,
        "judge_backend": row.get("judge_backend") or None,
        "deployment_profile": row.get("deployment_profile") or None,
        "internet_access": row.get("internet_access") or None,
        "ttl_s": _number(row.get("TTL")),
        "ttm_s": _number(row.get("TTM")),
        "diagnosis": {
            "success": _bool(row.get("Diagnosis.success")),
            "score": _number(row.get("Diagnosis.composite_score")),
            "submission": row.get("Diagnosis.submission") or "",
            "checklist": checklist if isinstance(checklist, list) else [],
        },
        "mitigation": {
            "success": _bool(row.get("Mitigation.success")),
            "failure_class": row.get("Mitigation.failure_class") or None,
            "reason": row.get("Mitigation.reason") or None,
            "detail": row.get("Mitigation.detail") or None,
        },
    }


def load_root_causes(path: Path | None) -> dict[str, str]:
    """Load the ground-truth root cause of each problem from a JSON file.

    Accepts {"problems": {id: {"root_cause": ...}}} or {id: text}.
    """
    if not path:
        return {}
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    problems = data.get("problems", data)
    out = {}
    for key, value in problems.items():
        text = value.get("root_cause") if isinstance(value, dict) else value
        if isinstance(text, str) and text.strip():
            out[key] = text
    return out
