from pathlib import Path

import pytest

from scripts import recover_jev_multitask_archive as recovery


def test_duplicate_identical_digest_lines_are_metadata_not_runtime_change(tmp_path):
    value = "sha256:" + "a" * 64
    config = "sha256:" + "b" * 64
    runtime = "sha256:" + "c" * 64
    log = tmp_path / "build.log"
    log.write_text(f"exporting config {config} done\nexporting manifest {runtime} done\n"
                   f"exporting manifest list {value} done\nexporting manifest list {value} done\n")
    got = recovery.build_digests(log)
    assert got["index"] == value and got["index_occurrences"] == 2
    assert recovery.support.runtime_identity(got) == (config, runtime)


def test_distinct_duplicate_digest_lines_are_rejected(tmp_path):
    log = tmp_path / "build.log"
    log.write_text("exporting config sha256:" + "a" * 64 + " done\n"
                   "exporting config sha256:" + "b" * 64 + " done\n"
                   "exporting manifest sha256:" + "c" * 64 + " done\n"
                   "exporting manifest list sha256:" + "d" * 64 + " done\n")
    with pytest.raises(ValueError, match="one unique config"):
        recovery.build_digests(log)
