import csv
import json
from pathlib import Path
import tempfile
import unittest

from scripts import continue_jev_pilot as resume


def digest(char):
    return "sha256:" + char * 64


def build_log(index):
    return (f"#22 exporting manifest {digest('a')} done\n"
            f"#22 exporting config {digest('b')} done\n"
            f"#22 exporting attestation manifest {digest(index)} done\n"
            f"#22 exporting manifest list {digest(index)} done\n")


class ResumeTests(unittest.TestCase):
    def make_baseline(self, root):
        batch = root / "pilot"
        batch.mkdir()
        source = root / "source.py"
        source.write_text("frozen = True\n")
        arms = []
        for arm, index in (("jev", "c"), ("terra", "d")):
            folder = batch / arm
            folder.mkdir()
            (folder / "stdout.log").write_text(build_log(index))
            row = {"problem_id": resume.pilot.PROBLEM, "attempt": "1", "run_status": "complete", "Diagnosis.success": "False", "Mitigation.success": "True"}
            csv_path = folder / "results.csv"
            with csv_path.open("w", newline="") as handle:
                writer = csv.DictWriter(handle, fieldnames=list(row));writer.writeheader();writer.writerow(row)
            result = {"arm": arm, "returncode": 0, "interrupted": False, "unexpected_running_containers": [], "result_rows": [row], "results_csv": str(csv_path.relative_to(batch)), "image_id": digest(index), "status": "completed" if arm == "jev" else "stopped_infrastructure", "stop_reasons": [] if arm == "jev" else [resume.STOP_REASON]}
            (folder / "result.json").write_text(json.dumps(result))
            arms.append(result)
        manifest = {"status": "stopped_infrastructure", "execution_order": ["jev", "terra", "luna", "sol"], "arms": arms,
                    "source_hashes": {"source.py": resume.pilot.sha(source)}, "commands": {arm: resume.pilot.command_for(arm) for arm in ("luna", "sol")}}
        (batch / "manifest.json").write_text(json.dumps(manifest))
        return batch, manifest

    def test_metadata_only_difference_is_eligible_and_old_evidence_is_unchanged(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);batch,manifest=self.make_baseline(root)
            before={p.relative_to(batch):p.read_bytes() for p in batch.rglob('*') if p.is_file()}
            _,evidence=resume.validate_resume(batch,root)
            self.assertEqual(resume.runtime_identity(evidence['jev']),resume.runtime_identity(evidence['terra']))
            self.assertNotEqual(evidence['jev']['index'],evidence['terra']['index'])
            resolution=resume._completion(manifest,evidence,[])
            self.assertEqual(resolution['arms'][1]['original_result']['status'],'stopped_infrastructure')
            self.assertEqual(resolution['arms'][1]['resolved_status'],'completed')
            self.assertFalse(resolution['comparison_complete'])
            self.assertEqual(before,{p.relative_to(batch):p.read_bytes() for p in batch.rglob('*') if p.is_file()})

    def test_refuses_running_incomplete_error_or_leftovers(self):
        for mutation in ('running','incomplete','nonzero','leftovers','different_reason'):
            with self.subTest(mutation=mutation),tempfile.TemporaryDirectory() as directory:
                root=Path(directory);batch,manifest=self.make_baseline(root)
                terra=manifest['arms'][1]
                if mutation=='running':manifest['status']='running'
                elif mutation=='incomplete':terra['result_rows'][0]['run_status']='incomplete'
                elif mutation=='nonzero':terra['returncode']=1
                elif mutation=='leftovers':terra['unexpected_running_containers']=['leftover']
                else:terra['stop_reasons']=['some_other_failure']
                (batch/'manifest.json').write_text(json.dumps(manifest));(batch/'terra/result.json').write_text(json.dumps(terra))
                with self.assertRaises(ValueError):resume.validate_resume(batch,root)

    def test_refuses_runtime_change_duplicate_build_or_source_drift(self):
        for mutation in ('runtime','duplicate','source'):
            with self.subTest(mutation=mutation),tempfile.TemporaryDirectory() as directory:
                root=Path(directory);batch,_=self.make_baseline(root);log=batch/'terra/stdout.log'
                if mutation=='runtime':log.write_text(log.read_text().replace(digest('b'),digest('e')))
                elif mutation=='duplicate':log.write_text(log.read_text()+f'exporting config {digest("b")} done\n')
                else:(root/'source.py').write_text('changed = True\n')
                with self.assertRaises(ValueError):resume.validate_resume(batch,root)

    def test_refuses_already_started_remaining_arm_or_continuation(self):
        for name in ('luna','continuation.json'):
            with self.subTest(name=name),tempfile.TemporaryDirectory() as directory:
                root=Path(directory);batch,_=self.make_baseline(root)
                if name=='luna':(batch/name).mkdir()
                else:(batch/name).write_text('{}')
                with self.assertRaises(ValueError):resume.validate_resume(batch,root)

    def test_remaining_commands_never_route_or_inherit_credentials(self):
        env=resume.pilot.clean_environment({'TYPESAFE_API_KEY':'secret','OPENAI_API_KEY':'secret','HOME':'/home'})
        self.assertNotIn('secret',env.values())
        for arm in ('luna','sol'):
            command=resume.pilot.command_for(arm)
            self.assertEqual(command[command.index('--model-router')+1],'none')
            self.assertIn('--force-build',command)


if __name__=='__main__':
    unittest.main()
