"""Find what SREGym recorded next to a trajectory: the scored CSV row."""

from __future__ import annotations

import ast
import csv
import json
import sys
from pathlib import Path
from typing import Any

csv.field_size_limit(sys.maxsize)


def _bool(value: Any) -> bool | None:
    text = str(value if value is not None else "").strip().lower()
    if text in {"true", "1", "yes"}:
        return True
    if text in {"false", "0", "no"}:
        return False
    return None


def _number(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _sum_of(*values: Any) -> int | None:
    numbers = [_number(v) for v in values]
    return int(sum(numbers)) if all(n is not None for n in numbers) else None


def _literal(value: Any) -> Any:
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
    """The CSV row about this run, searched in the run directory and up to two levels above it.

    SREGym writes one CSV per attempt into the run directory, so that one is found first. A CSV higher up holds a row
    per attempt: there the row of this ``attempt`` is taken. Where the rows say which attempt they are and none is
    this one, the search goes on and ends with nothing rather than another attempt's scores (it used to take the last
    row, so a second attempt could be given the first one's verdict and diagnosis); where no row says, one row only
    is taken, several are not. Without a problem id only a CSV with a single row can be trusted to be about this run.
    A plain ``results.csv`` is the table another harness around SREGym writes (see ``parse_row``); where its rows name
    their trajectory file (a ``trajectory`` column, relative to the table), the row of this ``trajectory`` is the one,
    and a table that names others only is not about this run (2026-09-28 review: two runs of one problem there, with
    no attempt number, had matched no row or shared one)."""
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
    try:
        return a.resolve() == b.resolve()
    except OSError:
        return False


def _attempt(value) -> str | None:
    """An attempt number as text, ``1.0`` and ``1`` alike; None where none is given."""
    text = str(value if value is not None else "").strip()
    if not text:
        return None
    number = _number(text)
    return str(int(number)) if number is not None and number == int(number) else text


def _parse_harness_row(row: dict) -> dict:
    """A row of the ``results.csv`` another harness around SREGym writes: one row per run, ``diag_success``,
    ``mitigation_success``, ``composite_score``, ``ttl``, ``ttm``, ``effort``. It keeps neither the text of the
    diagnosis nor the judge's checklist, so those are marked as not recorded rather than as empty."""
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
    """The parts of the scored row the report uses, with types."""
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
    """problem_id -> ground-truth root cause text. Accepts {"problems": {id: {"root_cause": ...}}} or {id: text}."""
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
