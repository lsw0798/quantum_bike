"""인증·네트워크·QPU 없이 입력 무결성과 재실행 동작을 확인한다."""
from copy import deepcopy
from pathlib import Path
import sys
import tempfile
from types import ModuleType, SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch

from truck_mis import cli
from truck_mis.domain import build_case
from truck_mis.settings import load_settings
from truck_mis.storage import read_json, write_json


class CliTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        model = {"origin_station_id": "1", "radius_km": 1, "bikes_per_task": 2,
                 "truck_capacity": 2, "truck_speed_kmh": 60,
                 "handling_min_per_bike": 0.25, "stop_overhead_min": 0,
                 "horizon_min": 4, "start_step_min": 2, "max_tasks": 8, "candidate_seed": 7}
        rows = [{"station_id": str(index), "name": str(index), "latitude": latitude,
                 "longitude": 126.91, "bikes": bikes, "target": 10}
                for index, latitude, bikes in [(1, 37.55, 10), (2, 37.5501, 12), (3, 37.5499, 8)]]
        self.case = build_case(rows, model, {"fixture": "independent_test_input"})
        self.settings = {
            "paths": {}, "data": {}, "model": model,
            "qpu": {"device_name": "FRESNEL", "region": "fr", "shots": 10,
                    "poll_timeout_seconds": 0.01, "coordinates_path": ""},
            "resolved_paths": {"project_root": self.root, "data_dir": self.root / "data",
                               "env_file": self.root / "unused.env", "output_dir": self.root / "outputs"},
            "config_path": self.root / "config.toml",
        }
        self.backend = ModuleType("truck_mis.backend")
        self.prepared = SimpleNamespace(
            embedding_report={"valid": True},
            node_ids=[task["task_id"] for task in self.case["tasks"]],
            coordinates=[[index * 5, 0] for index in range(len(self.case["tasks"]))],
        )
        self.backend.prepare_program = MagicMock(return_value=self.prepared)
        self.job = object()
        self.backend.submit_or_resume = MagicMock(side_effect=self.record_submission)
        self.backend.recover_job = MagicMock(return_value=self.job)
        self.backend.fetch_results = MagicMock()
        self.patches = [
            patch.dict(sys.modules, {"truck_mis.backend": self.backend}),
            patch.object(cli, "load_stations", return_value=(rows, {"fixture": "independent_test_input"})),
            patch.object(cli, "_connection", return_value=object()),
        ]
        for patcher in self.patches:
            patcher.start()

    def tearDown(self):
        for patcher in reversed(self.patches):
            patcher.stop()
        self.temporary.cleanup()

    def record_submission(self, prepared, connection, device_name, shots, directory, manifest_hash):
        # 실제 서버 호출 대신 영수증만 기록한다. 테스트 중 인증정보는 필요하지 않다.
        emulator = device_name == "EMU_FRESNEL"
        filename = "emulator_submission.json" if emulator else "qpu_submission.json"
        write_json(directory / filename, {
            "backend": "EMULATOR" if emulator else "QPU", "manifest_sha256": manifest_hash,
            "status": "submitted", "batch_id": "mock-batch-only",
        })
        return self.job

    def test_prepare_is_idempotent_and_never_submits(self):
        first_case, _, first_dir, first_hash = cli.prepare(self.settings)
        original_manifest = read_json(first_dir / "manifest.json")
        second_case, _, second_dir, second_hash = cli.prepare(self.settings)
        self.assertEqual(first_dir, second_dir)
        self.assertEqual(first_hash, second_hash)
        self.assertEqual(first_case, second_case)
        self.assertEqual(read_json(second_dir / "manifest.json"), original_manifest)
        self.backend.submit_or_resume.assert_not_called()
        cli._connection.assert_not_called()

    def test_prepare_rejects_tampered_case_before_embedding(self):
        _, _, directory, _ = cli.prepare(self.settings)
        changed = read_json(directory / "case.json")
        changed["tasks"][0]["quantity"] += 1
        write_json(directory / "case.json", changed)
        with self.assertRaises(ValueError):
            cli.prepare(self.settings)
        self.assertEqual(self.backend.prepare_program.call_count, 1)
        self.backend.submit_or_resume.assert_not_called()

    def test_run_submits_once_then_recovers_without_reembedding(self):
        with patch.object(cli, "_collect") as collect:
            cli.run(self.settings)
            cli.run(self.settings)
        self.assertEqual(self.backend.submit_or_resume.call_count, 1)
        self.assertEqual(self.backend.recover_job.call_count, 1)
        self.assertEqual(self.backend.prepare_program.call_count, 1)
        self.assertEqual(collect.call_count, 2)

    def test_emulator_run_resumes_existing_receipt_without_new_submission(self):
        self.settings["qpu"]["device_name"] = "EMU_FRESNEL"
        with patch.object(cli, "_collect"):
            cli.run(self.settings)
            cli.run(self.settings)
        self.assertEqual(self.backend.submit_or_resume.call_count, 1)
        self.assertEqual(self.backend.recover_job.call_count, 1)
        self.assertEqual(self.backend.prepare_program.call_count, 1)

    def test_emulator_evaluation_identifies_simulated_results(self):
        self.settings["qpu"]["device_name"] = "EMU_FRESNEL"
        _, _, directory, _ = cli.prepare(self.settings)
        cli._export(self.case, {"0" * len(self.case["tasks"]): 10}, directory)
        evaluation = read_json(directory / "evaluation.json")
        self.assertEqual(evaluation["backend"], "EMULATOR")
        self.assertEqual(evaluation["device_name"], "EMU_FRESNEL")
        self.assertTrue(evaluation["shot_count_matches_request"])

    def test_fetch_rejects_manifest_hash_mismatch_before_connection(self):
        _, _, directory, _ = cli.prepare(self.settings)
        changed = read_json(directory / "manifest.json")
        changed["manifest_hash"] = "0" * 64
        write_json(directory / "manifest.json", changed)
        with self.assertRaises(ValueError):
            cli.fetch(self.settings, directory)
        cli._connection.assert_not_called()
        self.backend.recover_job.assert_not_called()

    def test_fetch_rejects_modified_saved_qpu_settings(self):
        _, _, directory, _ = cli.prepare(self.settings)
        changed = read_json(directory / "manifest.json")
        changed["settings"]["qpu"]["shots"] += 1
        write_json(directory / "manifest.json", changed)
        with self.assertRaises(ValueError):
            cli.fetch(self.settings, directory)
        self.backend.recover_job.assert_not_called()

    def test_export_rejects_noninteger_counts_without_silent_coercion(self):
        bits = "0" * len(self.case["tasks"])
        for count in (1.9, True, "2"):
            with self.subTest(count=count), self.assertRaises(ValueError):
                cli._export(self.case, {bits: count}, self.root / "bad-counts")

    def test_missing_fetch_directory_is_cli_argument_error(self):
        with patch.object(cli, "load_settings", return_value=deepcopy(self.settings)):
            with self.assertRaises(SystemExit) as error:
                cli.main(["fetch"])
        self.assertEqual(error.exception.code, 2)

    def test_run_directory_argument_is_rejected_for_submission(self):
        with self.assertRaises(SystemExit) as error:
            cli.main(["run", "--run-dir", str(self.root)])
        self.assertEqual(error.exception.code, 2)
        self.backend.submit_or_resume.assert_not_called()

    def test_credentials_in_toml_are_rejected_without_echoing_value(self):
        path = self.root / "secret-test.toml"
        path.write_text('[qpu]\npassword = "fake-sensitive-test-value"\n', encoding="utf-8")
        with self.assertRaises(ValueError) as error:
            load_settings(path)
        self.assertNotIn("fake-sensitive-test-value", str(error.exception))

    def test_manual_coordinates_content_changes_run_identity(self):
        template = (Path(__file__).resolve().parents[1] / "config.toml").read_text(encoding="utf-8")
        template = template.replace('coordinates_path = ""', 'coordinates_path = "coords.json"')
        path = self.root / "coordinate-test.toml"
        path.write_text(template, encoding="utf-8")
        coordinates = self.root / "coords.json"
        coordinates.write_text('{"T000": [0, 0]}', encoding="utf-8")
        first = load_settings(path)["qpu"]["coordinates_sha256"]
        coordinates.write_text('{"T000": [1, 0]}', encoding="utf-8")
        second = load_settings(path)["qpu"]["coordinates_sha256"]
        self.assertNotEqual(first, second)


if __name__ == "__main__":
    unittest.main()
