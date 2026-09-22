import json

import pytest

from scripts import run_jev_pilot as pilot


def build_log(config="a", manifest="b", index="c"):
    return (
        f"exporting config sha256:{config * 64} done\n"
        f"exporting manifest sha256:{manifest * 64} done\n"
        f"exporting attestation manifest sha256:{index * 64} done\n"
        f"exporting manifest list sha256:{index * 64} done\n"
    )


def test_attestation_change_does_not_change_runtime_identity(tmp_path):
    first, second = tmp_path / "first.log", tmp_path / "second.log"
    first.write_text(build_log(index="c"))
    second.write_text(build_log(index="d"))
    one, two = pilot.build_digests(first), pilot.build_digests(second)
    assert one["index"] != two["index"]
    assert pilot.runtime_identity(one) == pilot.runtime_identity(two)


def test_runtime_content_change_remains_detectable(tmp_path):
    path = tmp_path / "build.log"
    path.write_text(build_log())
    reference = pilot.runtime_identity(pilot.build_digests(path))
    for kwargs in ({"config": "e"}, {"manifest": "f"}):
        path.write_text(build_log(**kwargs))
        assert reference != pilot.runtime_identity(pilot.build_digests(path))


def test_ambiguous_build_output_is_rejected(tmp_path):
    path = tmp_path / "build.log"
    for content in ("", build_log() * 2):
        path.write_text(content)
        with pytest.raises(ValueError):
            pilot.build_digests(path)


def test_unrelated_container_is_not_a_benchmark_cleanup_failure(monkeypatch):
    rows = [
        {"ID": "unrelated", "Names": "handoff-example"},
        {"ID": "cluster", "Names": "kind-control-plane"},
        {"ID": "agent", "Names": "sregym-codex-run-example"},
        {"ID": "judge", "Names": "sregym-judge-codex-example"},
        {"ID": "proxy", "Names": "evaluation-egress-proxy-example"},
    ]
    monkeypatch.setattr(pilot, "capture", lambda *args, **kwargs: {
        "stdout": "\n".join(json.dumps(row) for row in rows), "returncode": 0,
    })
    assert pilot.container_ids({}) == {"agent", "judge", "proxy"}
