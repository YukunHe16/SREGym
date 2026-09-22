import hashlib

import pytest

from scripts import continue_jev_multitask_after_context_fix as resume


def _sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_context_amendment_accepts_only_scoped_cronjob_count_fix(tmp_path, monkeypatch):
    root = tmp_path / "root"
    batch = tmp_path / "batch"
    frozen = batch / "configfreeze" / resume.CONTEXT_PATH
    current = root / resume.CONTEXT_PATH
    frozen.parent.mkdir(parents=True)
    current.parent.mkdir(parents=True)
    old = "prefix\n" + resume.OLD_CONTEXT_SNIPPET + "suffix\n"
    frozen.write_text(old)
    current.write_text(old.replace(resume.OLD_CONTEXT_SNIPPET, resume.NEW_CONTEXT_SNIPPET))
    monkeypatch.setattr(resume, "ROOT", root)
    manifest = {"source_hashes": {resume.CONTEXT_PATH: _sha(frozen)}}
    got = resume.context_amendment(batch, manifest)
    assert got["original_sha256"] == _sha(frozen)
    assert got["amended_sha256"] == _sha(current)
    assert "+            if isinstance(value, list):" in got["diff"]
    current.write_text(current.read_text() + "extra change\n")
    with pytest.raises(ValueError, match="beyond the scoped"):
        resume.context_amendment(batch, manifest)


def test_completed_prefix_requires_frozen_schedule_order():
    rows = []
    for index, (task, arm) in enumerate(resume.pilot.SCHEDULE[:2], start=1):
        rows.append({"index": index, "task": task, "arm": arm, "status": "completed"})
    assert resume._completed_prefix(rows, 0, 2) == rows
    rows[1]["arm"] = "sol"
    with pytest.raises(ValueError, match="completed scheduled result"):
        resume._completed_prefix(rows, 0, 2)
