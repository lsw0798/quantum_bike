"""원래 MIS와 에너지 목적의 대응 및 중복 제출 방지 회귀 검증."""
import itertools
import json
from dataclasses import replace
from hashlib import sha256
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import numpy as np

from truck_mis.backend import PreparedProgram, SubmissionUncertain, submit_or_resume
from truck_mis.embedding import EmbeddingRejected, embed_graph, geometry_certificate


class EmbeddingTests(unittest.TestCase):
    def test_amplitude_factor_meets_device_minimum_without_changing_final_mis(self):
        from truck_mis.backend import compile_program, prepare_program
        from pulser.devices import AnalogDevice
        from pulser.sampler import sample

        edges = [(0, 5), (0, 7), (1, 2), (2, 3), (2, 4), (2, 6),
                 (3, 4), (3, 6), (4, 6), (5, 7)]
        # 2026-11-11에 조회한 FRESNEL 및 FRESNEL_CAN1의 최소 평균 진폭.
        for minimum, factor in ((0.5654866776461628, 6.5), (0.3141592653589793, 4.0)):
            with self.subTest(minimum=minimum, factor=factor):
                channel = replace(AnalogDevice.channels["rydberg_global"], min_avg_amp=minimum)
                device = replace(AnalogDevice, channel_objects=(channel,))
                prepared = prepare_program([f"p{i}" for i in range(8)], edges,
                                           {"amplitude_factor": factor})
                sequence, certificate = compile_program(prepared, device)
                waveform = sample(sequence, modulation=False).channel_samples["rydberg"]
                self.assertGreaterEqual(float(np.mean(waveform.amp.as_array())), minimum)
                self.assertAlmostEqual(certificate["final_amplitude"], 0.0, places=12)
                self.assertLess(certificate["total_nonedge_interaction_bound"], certificate["final_detuning"])
                self.assertLess(certificate["final_detuning"], certificate["minimum_edge_interaction"])

    def test_invalid_amplitude_factor_is_rejected(self):
        from truck_mis.backend import prepare_program

        for value in (0, -1, float("nan"), float("inf"), True):
            with self.subTest(value=value), self.assertRaises(ValueError):
                prepare_program(["a", "b"], [(0, 1)], {"amplitude_factor": value})

    def test_compiled_sdk_sequence_preserves_hamiltonian_and_measurement_order(self):
        from truck_mis.backend import compile_program, prepare_program
        from pulser.devices import AnalogDevice

        # 클릭과 pendant vertex를 포함하는 실제 파일럿 그래프 형태를 그대로 검사한다.
        edges = [(0, 5), (0, 7), (1, 2), (2, 3), (2, 4), (2, 6),
                 (3, 4), (3, 6), (4, 6), (5, 7)]
        prepared = prepare_program([f"p{i}" for i in range(8)], edges, {})
        sequence, certificate = compile_program(prepared, AnalogDevice)
        self.assertEqual(list(sequence.register.qubits), [str(i) for i in range(8)])
        self.assertLess(certificate["total_nonedge_interaction_bound"], certificate["final_detuning"])
        self.assertLess(certificate["final_detuning"], certificate["minimum_edge_interaction"])
        self.assertLessEqual(sequence.get_duration(), AnalogDevice.max_sequence_duration)

    def test_cycle_energy_ground_states_have_maximum_cardinality(self):
        # 독립적인 전수검사는 테스트 오라클일 뿐 QPU 입력·해 선택에는 사용하지 않는다.
        nodes = [f"p{i}" for i in range(5)]
        edges = [(0, 1), (1, 2), (2, 3), (3, 4), (0, 4)]
        graph = embed_graph(nodes, edges, {})
        delta = graph.certificate["final_detuning"]
        states = []
        for bits in itertools.product((0, 1), repeat=5):
            energy = -delta * sum(bits)
            for a, b in itertools.combinations(range(5), 2):
                r = np.linalg.norm(graph.coordinates[a] - graph.coordinates[b])
                energy += bits[a] * bits[b] / r**6
            valid = all(not (bits[a] and bits[b]) for a, b in edges)
            states.append((energy, sum(bits), valid))
        optimum = max(size for _, size, valid in states if valid)
        ground = min(row[0] for row in states)
        self.assertTrue(all(valid and size == optimum for energy, size, valid in states
                            if abs(energy - ground) < 1e-9))

    def test_nonedge_tails_can_invalidate_adjacency_correct_geometry(self):
        # 거리 기준 엣지가 구분돼도 비엣지 합 조건을 실패하는 C4 배치다.
        coords = [(0, 0), (1, 0), (1, 1), (0, 1)]
        with self.assertRaises(EmbeddingRejected):
            geometry_certificate(list("abcd"), [(0, 1), (1, 2), (2, 3), (0, 3)],
                                 coords, final_detuning=0.1)

    def test_missing_edge_is_rejected(self):
        with self.assertRaises(EmbeddingRejected):
            geometry_certificate(list("abc"), [(0, 2)], [(0, 0), (1, 0), (2, 0)])

    def test_isolated_vertices_preserved(self):
        graph = embed_graph(list("abcde"), [(0, 1), (1, 2)], {})
        self.assertEqual(graph.node_ids, tuple("abcde"))
        self.assertEqual(len(graph.coordinates), 5)
        self.assertEqual(len(graph.edges), 2)

    def test_invalid_indices_rejected(self):
        for edges in [[(0, 0)], [(0, 3)], [(0, True)]]:
            with self.assertRaises(ValueError):
                embed_graph(list("abc"), edges, {})


class ReceiptTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name)
        self.graph = embed_graph(list("abc"), [(0, 1), (1, 2)], {})
        self.prepared = PreparedProgram(self.graph, None, {})
        self.connection = SimpleNamespace(_truck_mis_single_write=True)

    def tearDown(self):
        self.tmp.cleanup()

    def test_ambiguous_receipt_never_calls_connection(self):
        from truck_mis.backend import _graph_digest
        receipt = {
            "status": "submission_outcome_unknown", "manifest_sha256": "a" * 64,
            "program_sha256": _graph_digest(self.prepared), "node_order": list("abc"),
            "device_name": "FRESNEL", "shots": 100, "backend": "QPU",
        }
        (self.path / "qpu_submission.json").write_text(json.dumps(receipt))
        with self.assertRaises(SubmissionUncertain):
            submit_or_resume(self.prepared, self.connection, "FRESNEL", 100, self.path, "a" * 64)

    def test_existing_batch_only_restores_and_rejects_changed_identity(self):
        from truck_mis.backend import _graph_digest
        receipt = {
            "status": "batch_received", "batch_id": "previous-batch", "manifest_sha256": "a" * 64,
            "program_sha256": _graph_digest(self.prepared), "node_order": list("abc"),
            "device_name": "FRESNEL", "shots": 100, "backend": "QPU",
        }
        (self.path / "qpu_submission.json").write_text(json.dumps(receipt))
        with patch("truck_mis.backend._restore", return_value="existing-job") as restore:
            self.assertEqual(submit_or_resume(self.prepared, self.connection, "FRESNEL", 100,
                                             self.path, "a" * 64), "existing-job")
            restore.assert_called_once()
        with self.assertRaises(ValueError):
            submit_or_resume(self.prepared, self.connection, "FRESNEL", 101, self.path, "a" * 64)

    def test_emulator_not_accepted(self):
        with self.assertRaises(ValueError):
            submit_or_resume(self.prepared, self.connection, "EMU_FREE", 100, self.path, "a" * 64)

    def test_fresnel_emulator_submits_once_with_distinct_receipt_and_backend(self):
        from pasqal_cloud import PasqalCloudConnection
        from pasqal_cloud.device.device_types import DeviceTypeName
        from pulser.devices import AnalogDevice
        from truck_mis.backend import prepare_program

        connection = object.__new__(PasqalCloudConnection)
        connection._truck_mis_single_write = True
        connection.fetch_available_devices = lambda: {"FRESNEL": AnalogDevice}
        connection.cloud_client = SimpleNamespace(
            create_batch=Mock(return_value=SimpleNamespace(id="emulator-batch")),
            get_batch=Mock(return_value=SimpleNamespace(
                ordered_jobs=[SimpleNamespace(id="emulator-job")]))
        )
        prepared = prepare_program(list("abc"), [(0, 1), (1, 2)], {"amplitude_factor": 6.5})
        with patch("qoolqit.execution.retrieve_remote_job", return_value="restored-emulator-job"):
            for _ in range(2):
                self.assertEqual(submit_or_resume(prepared, connection, "EMU_FRESNEL", 100,
                                                  self.path, "a" * 64), "restored-emulator-job")
        self.assertEqual(connection.cloud_client.create_batch.call_count, 1)
        sent = connection.cloud_client.create_batch.call_args.kwargs
        self.assertEqual(sent["device_type"], DeviceTypeName.EMU_FRESNEL)
        self.assertEqual(sent["jobs"], [{"runs": 100}])
        record = json.loads((self.path / "emulator_submission.json").read_text())
        self.assertEqual(record["backend"], "EMULATOR")
        self.assertEqual(record["device_name"], "EMU_FRESNEL")
        self.assertEqual(record["compilation_device_name"], "FRESNEL")
        self.assertEqual(record["job_id"], "emulator-job")
        self.assertFalse((self.path / "qpu_submission.json").exists())

    def test_fresnel_emulator_recovery_rejects_changed_saved_sequence(self):
        from truck_mis.backend import recover_job

        sequence = '{"sequence":"saved-emulator-input"}'
        (self.path / "sequence.json").write_text(sequence)
        (self.path / "emulator_submission.json").write_text(json.dumps({
            "backend": "EMULATOR", "device_name": "EMU_FRESNEL",
            "manifest_sha256": "a" * 64, "batch_id": "emulator-batch",
            "job_id": "emulator-job", "sequence_sha256": sha256(sequence.encode()).hexdigest(),
        }))
        with patch("truck_mis.backend._restore", return_value="existing-emulator-job"):
            self.assertEqual(recover_job(self.connection, self.path, "a" * 64), "existing-emulator-job")
            (self.path / "sequence.json").write_text(sequence + "changed")
            with self.assertRaises(ValueError):
                recover_job(self.connection, self.path, "a" * 64)

    def test_fresnel_emulator_ambiguous_submission_is_not_retried(self):
        from pulser.devices import AnalogDevice

        client = SimpleNamespace(create_batch=Mock(side_effect=TimeoutError("test timeout")))
        self.connection.cloud_client = client
        self.connection.fetch_available_devices = lambda: {"FRESNEL": AnalogDevice}
        sequence = Mock()
        sequence.to_abstract_repr.return_value = '{"sequence":"test"}'
        with patch("truck_mis.backend.compile_program", return_value=(sequence, {})), \
             patch("pulser.backends.QPUBackend"):
            with self.assertRaises(TimeoutError):
                submit_or_resume(self.prepared, self.connection, "EMU_FRESNEL", 100,
                                 self.path, "a" * 64)
        record = json.loads((self.path / "emulator_submission.json").read_text())
        self.assertEqual(record["status"], "submission_outcome_unknown")
        with self.assertRaises(SubmissionUncertain):
            submit_or_resume(self.prepared, self.connection, "EMU_FRESNEL", 100,
                             self.path, "a" * 64)
        self.assertEqual(client.create_batch.call_count, 1)

    def test_cross_backend_receipt_blocks_submission_without_overwriting_input(self):
        from pulser.devices import AnalogDevice

        for filename, target in (("qpu_submission.json", "EMU_FRESNEL"),
                                 ("emulator_submission.json", "FRESNEL")):
            with self.subTest(target=target), tempfile.TemporaryDirectory(dir=self.path) as name:
                directory = Path(name)
                original_sequence = '{"sequence":"original-submitted-input"}'
                (directory / "sequence.json").write_text(original_sequence)
                original_receipt = '{"status":"submitted","batch_id":"existing-batch"}'
                (directory / filename).write_text(original_receipt)
                client = SimpleNamespace(create_batch=Mock(return_value=SimpleNamespace(id="new-batch")))
                self.connection.cloud_client = client
                self.connection.fetch_available_devices = Mock(return_value={"FRESNEL": AnalogDevice})
                sequence = Mock()
                sequence.to_abstract_repr.return_value = '{"sequence":"new-input"}'
                with patch("truck_mis.backend.compile_program", return_value=(sequence, {})), \
                     patch("pulser.backends.QPUBackend"), \
                     patch("truck_mis.backend._restore", return_value="new-job"):
                    with self.assertRaises(ValueError):
                        submit_or_resume(self.prepared, self.connection, target, 100,
                                         directory, "a" * 64)
                self.connection.fetch_available_devices.assert_not_called()
                client.create_batch.assert_not_called()
                self.assertEqual((directory / "sequence.json").read_text(), original_sequence)
                self.assertEqual((directory / filename).read_text(), original_receipt)
                self.assertEqual({p.name for p in directory.iterdir()}, {"sequence.json", filename})

    def test_common_reservation_blocks_submission_before_other_receipt_exists(self):
        from pulser.devices import AnalogDevice

        (self.path / "submission_guard.json").write_text('{"owner":"other-process"}')
        client = SimpleNamespace(create_batch=Mock(return_value=SimpleNamespace(id="new-batch")))
        self.connection.cloud_client = client
        self.connection.fetch_available_devices = Mock(return_value={"FRESNEL": AnalogDevice})
        sequence = Mock()
        sequence.to_abstract_repr.return_value = '{"sequence":"new-input"}'
        with patch("truck_mis.backend.compile_program", return_value=(sequence, {})), \
             patch("pulser.backends.QPUBackend"), \
             patch("truck_mis.backend._restore", return_value="new-job"):
            with self.assertRaises(SubmissionUncertain):
                submit_or_resume(self.prepared, self.connection, "EMU_FRESNEL", 100,
                                 self.path, "a" * 64)
        self.connection.fetch_available_devices.assert_not_called()
        client.create_batch.assert_not_called()
        self.assertEqual({p.name for p in self.path.iterdir()}, {"submission_guard.json"})

    def test_write_transport_does_not_retry_http_error(self):
        from truck_mis.backend import create_connection

        # 실제 인증 대신 연결 생성자를 대체해 사용되는 HTTP 클래스만 확인한다.
        with patch("pasqal_cloud.PasqalCloudConnection") as constructor:
            create_connection("dummy-user", "dummy-password", "dummy-project")
        client_type = constructor.call_args.kwargs["client_class"]
        client = object.__new__(client_type)
        client.authenticator = object()
        client.user_agent = "unit-test"
        client.allow_gzip_request_body = False
        client._request_with_status_check = Mock(side_effect=TimeoutError("test"))
        with self.assertRaises(TimeoutError):
            client._authenticated_request("POST", "https://example.invalid", {"jobs": []})
        self.assertEqual(client._request_with_status_check.call_count, 1)

    def test_remote_error_persists_unknown_and_does_not_retry(self):
        try:
            from pulser.devices import AnalogDevice
        except ImportError:
            self.skipTest("Pulser 설치 후 실행하는 SDK 통합 회귀 검사")
        client = SimpleNamespace(create_batch=Mock(side_effect=TimeoutError("test timeout")))
        self.connection.cloud_client = client
        self.connection.fetch_available_devices = lambda: {"FRESNEL": AnalogDevice}
        sequence = Mock()
        sequence.to_abstract_repr.return_value = '{"sequence":"test"}'
        with patch("truck_mis.backend.compile_program", return_value=(sequence, {})), \
             patch("pulser.backends.QPUBackend"):
            with self.assertRaises(TimeoutError):
                submit_or_resume(self.prepared, self.connection, "FRESNEL", 100,
                                 self.path, "a" * 64)
        self.assertEqual(client.create_batch.call_count, 1)
        receipt = json.loads((self.path / "qpu_submission.json").read_text())
        self.assertEqual(receipt["status"], "submission_outcome_unknown")
        with self.assertRaises(SubmissionUncertain):
            submit_or_resume(self.prepared, self.connection, "FRESNEL", 100,
                             self.path, "a" * 64)
        self.assertEqual(client.create_batch.call_count, 1)


if __name__ == "__main__":
    unittest.main()
