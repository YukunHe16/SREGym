import json
from pathlib import Path

import pytest

from scripts import continue_jev_multitask as resume


def test_validator_requires_exact_pre_model_deploy_failure(tmp_path, monkeypatch):
    batch = tmp_path / "batch"
    batch.mkdir()
    source = tmp_path / "source"
    source.write_text("frozen")
    completed = []
    for index, (task, arm) in enumerate(resume.pilot.SCHEDULE[:4], start=1):
        completed.append({"index": index, "task": task, "arm": arm, "status": "completed",
                          "build_digests": {"config": "a", "runtime_manifest": "b"}})
    task, arm = resume.pilot.SCHEDULE[4]
    failed = {"index": 5, "task": task, "arm": arm, "status": "stopped_infrastructure",
              "stop_reasons": ["deploy_failed"], "returncode": 0,
              "result_rows": [{"deploy_failed": "True"}]}
    failure_dir = batch / f"05-{task}--{arm}"
    failure_dir.mkdir()
    manifest = {"status": "stopped_infrastructure", "schedule": [list(x) for x in resume.pilot.SCHEDULE],
                "runs": [*completed, failed], "source_hashes": {"source": resume.support.sha(source)}}
    (batch / "manifest.json").write_text(json.dumps(manifest))
    monkeypatch.setattr(resume, "ROOT", tmp_path)
    monkeypatch.setattr(resume.support, "clean_environment", lambda env: {})
    monkeypatch.setattr(resume.support, "capture", lambda *args, **kwargs: {"stdout": resume.EXPECTED_NESTED_APP_HEAD})
    got, effective = resume.validate(batch)
    assert got == manifest and len(effective) == 4
    (failure_dir / "routing.json").write_text("{}")
    with pytest.raises(ValueError, match="model execution evidence"):
        resume.validate(batch)


def test_remaining_schedule_does_not_repeat_completed_pairs():
    assert resume.pilot.SCHEDULE[4] == ("secret_rotation_stale_env_credentials_astronomy_shop", "luna")
    assert len(resume.pilot.SCHEDULE[4:]) == 12
    assert set(resume.pilot.SCHEDULE[:4]).isdisjoint(resume.pilot.SCHEDULE[4:])
