"""QoolQit 고정 펄스 → Pasqal QPU/EMU_FRESNEL → 원시 비트열 조회.

이 모듈을 import하거나 prepare_program을 호출해도 인증·원격 제출은 없다.
장치 제약과 최종 에너지 충분조건을 통과한 전체 그래프만 제출한다.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import gzip
from hashlib import sha256
import json
import math
import os
from pathlib import Path
import tempfile
from typing import Any, Mapping

import numpy as np

from .embedding import EmbeddedGraph, EmbeddingRejected, embed_graph, geometry_certificate


class SubmissionUncertain(RuntimeError):
    """서버 접수 여부가 불명확하므로 자동으로 재제출할 수 없는 상태."""


def atomic_json(path: Path, value: dict) -> None:
    """같은 디렉터리의 임시 파일을 원자적으로 교체해 기록 손상을 줄인다."""
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2, allow_nan=False)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
        # 프로세스/전원 중단에도 새 파일 이름을 최대한 보존한다.
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def create_connection(username: str, password: str, project_id: str, region: str = "fr") -> Any:
    """기존 .env에서 읽은 값을 받아 연결하며 쓰기 요청 자동 재시도를 금지한다.

    pasqal-cloud 0.24의 기본 연결과 HTTP 계층은 POST도 재시도하므로
    배치 생성은 단발 요청으로 바꾼다. GET 조회의 재시도는 그대로 사용한다.
    인증 정보는 파일이나 로그에 기록하지 않는다.
    """
    from pasqal_cloud import PasqalCloudConnection
    from pasqal_cloud.http_client import HTTPClient, TIMEOUT

    class SingleWriteHTTPClient(HTTPClient):
        def _authenticated_request(self, method, url, payload=None, params=None, gziped=False):
            if method.upper() == "GET":
                return super()._authenticated_request(method, url, payload, params, gziped)
            if self.authenticator is None:
                raise ValueError("Pasqal 인증 정보가 필요합니다.")
            headers = {"content-type": "application/json", "User-Agent": self.user_agent}
            if self.allow_gzip_request_body and gziped:
                headers["Content-Encoding"] = "gzip"
                body = {"data": gzip.compress(json.dumps(payload).encode("utf-8"))}
            else:
                body = {"json": payload}
            response = self._request_with_status_check(
                method, url, timeout=TIMEOUT, headers=headers, auth=self.authenticator,
                params=params, **body,
            )
            return response.json()

    connection = PasqalCloudConnection(
        username=username, password=password, project_id=project_id, region=region,
        client_class=SingleWriteHTTPClient,
    )
    # 외부에서 임의로 만든 연결을 제출에 쓰지 않도록 표시한다.
    connection._truck_mis_single_write = True
    return connection


@dataclass
class PreparedProgram:
    graph: EmbeddedGraph
    program: Any
    config: dict[str, Any]

    @property
    def node_ids(self) -> tuple[str, ...]:
        return self.graph.node_ids

    @property
    def coordinates(self) -> np.ndarray:
        return self.graph.coordinates

    @property
    def certificate(self) -> dict:
        return self.graph.certificate

    @property
    def embedding_report(self) -> dict:
        return self.graph.certificate


def prepare_program(node_ids: list[str], edges: list[tuple[int, int]], config: dict) -> PreparedProgram:
    """그래프를 배치하고 사전에 정한 전역 진폭·detuning 파형을 만든다."""
    from qoolqit import Drive, InterpolatedWaveform, QuantumProgram, Register

    graph = embed_graph(node_ids, edges, config)
    duration = float(config.get("duration", 40.0))
    amplitude_factor = config.get("amplitude_factor", 1.0)
    ratio = config.get("device_max_duration_ratio", 0.8)
    if not math.isfinite(duration) or duration <= 0:
        raise ValueError("무차원 펄스 duration은 양의 유한한 값이어야 합니다.")
    if (isinstance(amplitude_factor, bool) or not isinstance(amplitude_factor, (int, float))
            or not math.isfinite(amplitude_factor) or amplitude_factor <= 0):
        raise ValueError("amplitude_factor는 양의 유한한 숫자여야 합니다.")
    if ratio is not None and (not math.isfinite(ratio) or not 0 < ratio <= 1):
        raise ValueError("device_max_duration_ratio는 (0, 1] 또는 null이어야 합니다.")
    delta = graph.certificate["final_detuning"]
    register = Register.from_coordinates(graph.coordinates)
    drive = Drive(
        amplitude=InterpolatedWaveform(duration, [0.0, delta * amplitude_factor, 0.0]),
        detuning=InterpolatedWaveform(duration, [-delta, 0.0, delta]),
    )
    return PreparedProgram(graph, QuantumProgram(register, drive), dict(config))


def compiled_certificate(prepared: PreparedProgram, compiled: Any) -> dict[str, Any]:
    """실제 C6·원자 좌표·명령 파형으로 목적 보존 충분조건을 다시 검사한다."""
    from pulser.sampler import sample

    ids = tuple(str(i) for i in compiled.register.qubits)
    if ids != tuple(str(i) for i in range(len(prepared.node_ids))):
        raise EmbeddingRejected("컴파일 후 원자/비트 순서가 달라졌습니다.")
    channels = sample(compiled, modulation=False).channel_samples
    if len(channels) != 1:
        raise EmbeddingRejected("단일 전역 Rydberg 채널이 필요합니다.")
    name, waveform = next(iter(channels.items()))
    channel = compiled.declared_channels[name]
    if channel.addressing != "Global" or channel.basis != "ground-rydberg":
        raise EmbeddingRejected("지원하지 않는 채널 종류입니다.")
    detuning = float(waveform.det.as_array()[-1])
    amplitude = float(waveform.amp.as_array()[-1])
    if not math.isfinite(amplitude) or abs(amplitude) > max(1e-8, abs(detuning) * 1e-8):
        raise EmbeddingRejected("최종 명령 진폭이 0이 아닙니다.")
    certificate = geometry_certificate(
        prepared.node_ids, prepared.graph.edges,
        np.asarray(list(compiled.register.qubits.values())),
        c6=float(compiled.device.interaction_coeff), final_detuning=detuning,
    )
    certificate.update(
        units="distance: um; interaction/detuning: rad/us; duration: ns",
        final_amplitude=amplitude,
        peak_amplitude=float(np.max(waveform.amp.as_array())),
        mean_amplitude=float(np.mean(waveform.amp.as_array())),
        channel_min_avg_amp=channel.min_avg_amp,
        duration_ns=compiled.get_duration(),
        node_order=list(prepared.node_ids),
        commanded_hamiltonian_only=True,
    )
    return certificate


def compile_program(prepared: PreparedProgram, pulser_device: Any) -> tuple[Any, dict]:
    """지정 장치로 컴파일만 수행한다. 원격 제출을 하지 않는 검증 함수다."""
    from qoolqit.devices import Device
    from qoolqit.execution.compilation_functions import CompilerProfile

    geometry_certificate(prepared.node_ids, prepared.graph.edges, prepared.coordinates)
    if pulser_device.requires_layout and not pulser_device.accepts_new_layouts:
        raise EmbeddingRejected(
            "이 QPU는 사전에 교정한 트랩 배치만 허용합니다. 현재 직접 좌표 인코더는 "
            "자동 생성 레이아웃을 허용하는 QPU가 필요하므로 제출하지 않습니다."
        )
    wrapped = Device(pulser_device=pulser_device)
    ratio = prepared.config.get("device_max_duration_ratio", 0.8)
    if pulser_device.max_sequence_duration is None:
        ratio = None
    prepared.program.compile_to(
        device=wrapped, profile=CompilerProfile.MAX_ENERGY,
        device_max_duration_ratio=ratio,
    )
    compiled = prepared.program.compiled_sequence
    certificate = compiled_certificate(prepared, compiled)
    certificate["device_specs_dimensionless"] = wrapped.specs
    return compiled, certificate


def _graph_digest(prepared: PreparedProgram) -> str:
    document = {
        "nodes": list(prepared.node_ids), "edges": sorted(prepared.graph.edges),
        "coordinates": prepared.coordinates.tolist(), "config": prepared.config,
    }
    return sha256(json.dumps(document, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def _restore(record: dict, receipt: Path, connection: Any) -> Any:
    """저장한 batch만 조회한다. 배치 생성은 절대 호출하지 않는다."""
    from qoolqit.execution import retrieve_remote_job

    batch_id = record.get("batch_id")
    if not batch_id:
        raise SubmissionUncertain(
            "기존 제출 기록에 batch ID가 없습니다. Pasqal 포털에서 접수 여부를 확인하세요. "
            "이 실행 폴더에서는 자동 재제출하지 않습니다."
        )
    if not record.get("job_id"):
        batch = connection.cloud_client.get_batch(batch_id)
        jobs = batch.ordered_jobs
        if len(jobs) != 1:
            raise SubmissionUncertain("기존 배치의 작업 수가 1개가 아닙니다. 포털에서 확인하세요.")
        record.update(status="submitted", job_id=jobs[0].id)
        atomic_json(receipt, record)
    return retrieve_remote_job(connection, record["job_id"], batch_id=batch_id)


def submit_or_resume(
    prepared: PreparedProgram,
    connection: Any,
    device_name: str,
    shots: int,
    run_dir: Path,
    manifest_hash: str,
) -> Any:
    """검증 후 지정한 원격 장치에 한 번 제출하거나 기존 작업을 복구한다.

    동일 폴더의 receipt가 존재하면 새 배치를 만들지 않는다. 전송 중 예외가
    나면 접수 여부가 불명확한 상태를 저장하고 재시도하지 않는다.
    """
    if isinstance(shots, bool) or not isinstance(shots, int) or shots < 1:
        raise ValueError("shots는 양의 정수여야 합니다.")
    is_emulator = device_name == "EMU_FRESNEL"
    if not device_name or (device_name.upper().startswith("EMU") and not is_emulator):
        raise ValueError("실제 QPU 또는 지원하는 에뮬레이터 EMU_FRESNEL을 지정하세요.")
    if not getattr(connection, "_truck_mis_single_write", False):
        raise ValueError("자동 중복 제출을 방지하는 create_connection 연결이 필요합니다.")
    if not isinstance(manifest_hash, str) or len(manifest_hash) != 64:
        raise ValueError("manifest_hash는 SHA-256 문자열이어야 합니다.")
    run_dir = Path(run_dir)
    run_dir.mkdir(parents=True, exist_ok=True)
    receipt = run_dir / ("emulator_submission.json" if is_emulator else "qpu_submission.json")
    other_receipt = run_dir / ("qpu_submission.json" if is_emulator else "emulator_submission.json")
    if other_receipt.exists():
        raise ValueError("다른 종류의 장치 제출 기록이 있습니다. 같은 실행 폴더를 재사용하지 않습니다.")
    compilation_device_name = "FRESNEL" if is_emulator else device_name
    identity = dict(
        manifest_sha256=manifest_hash, program_sha256=_graph_digest(prepared),
        node_order=list(prepared.node_ids), device_name=device_name, shots=shots,
        backend="EMULATOR" if is_emulator else "QPU",
    )
    if is_emulator:
        identity["compilation_device_name"] = compilation_device_name
    if receipt.exists():
        record = json.loads(receipt.read_text(encoding="utf-8"))
        if any(record.get(key) != value for key, value in identity.items()):
            raise ValueError("기존 제출 기록과 현재 입력/설정이 다릅니다. 같은 기록을 덮어쓰지 않습니다.")
        if record.get("batch_id"):
            return _restore(record, receipt, connection)
        raise SubmissionUncertain(
            f"기존 제출 상태는 {record.get('status')}입니다. 원격 작업을 자동으로 다시 만들지 않습니다."
        )
    record = identity | {"status": "preparing", "created_at_utc": datetime.now(timezone.utc).isoformat()}
    # 두 종류의 영수증을 만드는 프로세스도 같은 폴더에서는 하나만 진입할 수 있다.
    try:
        with (run_dir / "submission_guard.json").open("x", encoding="utf-8") as stream:
            json.dump(identity | {"receipt_file": receipt.name}, stream, ensure_ascii=False, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
    except FileExistsError:
        raise SubmissionUncertain(
            "기존 제출 예약 기록이 있습니다. 진행 중인 프로세스나 중단된 제출을 확인하세요. "
            "영수증 없이 자동 재제출하지 않습니다."
        ) from None
    with receipt.open("x", encoding="utf-8") as stream:
        json.dump(record, stream, ensure_ascii=False, indent=2)
        stream.flush()
        os.fsync(stream.fileno())
    submission_started = False
    try:
        from pulser.backend import BackendConfig
        from pulser.backends import QPUBackend
        from pulser.devices import Device as PulserDevice
        from pasqal_cloud.device.device_types import DeviceTypeName

        devices = connection.fetch_available_devices()
        if compilation_device_name not in devices:
            raise ValueError(f"컴파일 장치 {compilation_device_name} 사양 조회 불가: {sorted(devices)}")
        actual_device = devices[compilation_device_name]
        if not isinstance(actual_device, PulserDevice):
            raise ValueError("원격에서 받은 장치가 실제 QPU Device가 아닙니다.")
        compiled, certificate = compile_program(prepared, actual_device)
        # 물리 장치 제약은 검증하되 실행 대상은 아래 create_batch의 device_type으로 고정한다.
        backend = QPUBackend(compiled, connection=connection, config=BackendConfig(default_num_shots=shots))
        backend.validate_job_params([{"runs": shots}], actual_device.max_runs)
        compiled.measure("ground-rydberg")
        serialized = compiled.to_abstract_repr()
        (run_dir / "sequence.json").write_text(serialized, encoding="utf-8")
        (run_dir / "device.json").write_text(actual_device.to_abstract_repr(), encoding="utf-8")
        atomic_json(run_dir / "compiled_certificate.json", certificate)
        record.update(
            status="submitting", sequence_sha256=sha256(serialized.encode()).hexdigest(),
            compiled_certificate=certificate, submitted_at_utc=datetime.now(timezone.utc).isoformat(),
        )
        atomic_json(receipt, record)
        # 이 지점부터 실패는 서버 접수 여부를 확정할 수 없는 것으로 취급한다.
        submission_started = True
        batch = connection.cloud_client.create_batch(
            serialized_sequence=serialized, jobs=[{"runs": shots}],
            device_type=DeviceTypeName(device_name), open=False, wait=False,
            tags=[f"truck-mis-{manifest_hash[:16]}"],
        )
        # 작업 목록 조회에 실패해도 batch ID만으로 복구할 수 있게 즉시 저장한다.
        record.update(status="batch_received", batch_id=batch.id)
        atomic_json(receipt, record)
        return _restore(record, receipt, connection)
    except BaseException as error:
        if not record.get("batch_id"):
            record["status"] = "submission_outcome_unknown" if submission_started else "not_submitted"
        # 인증 정보를 포함할 수 있는 예외 메시지/traceback은 저장하지 않는다.
        record["error_type"] = type(error).__name__
        if isinstance(error, EmbeddingRejected):
            record["local_validation_reason"] = str(error)
        atomic_json(receipt, record)
        raise


def fetch_results(job: Any, timeout: float = 30.0) -> Mapping[str, int]:
    """원시 측정 비트열을 반환한다. 비가능 표본을 제거하거나 수선하지 않는다."""
    if not math.isfinite(timeout) or timeout <= 0:
        raise ValueError("결과 조회 timeout은 양의 유한한 초 단위 값이어야 합니다.")
    result = job.results(timeout=timeout)
    counts = dict(result.final_bitstrings)
    if any(not isinstance(k, str) or not set(k) <= {"0", "1"} for k in counts):
        raise ValueError("원격 장치가 반환한 비트열 형식이 올바르지 않습니다.")
    if any(isinstance(v, bool) or not isinstance(v, (int, np.integer)) or v < 0 for v in counts.values()):
        raise ValueError("관측 횟수는 0 이상의 정수여야 합니다.")
    return {key: int(value) for key, value in counts.items()}


def recover_job(connection: Any, run_dir: Path, manifest_hash: str) -> Any:
    """원자 배치를 다시 만들지 않고 저장한 원격 작업만 조회한다."""
    receipts = [Path(run_dir) / name for name in ("qpu_submission.json", "emulator_submission.json")
                if (Path(run_dir) / name).exists()]
    if len(receipts) != 1:
        raise ValueError("실행 폴더에 원격 제출 기록이 정확히 하나 필요합니다.")
    receipt = receipts[0]
    record = json.loads(receipt.read_text(encoding="utf-8"))
    expected_backend = "EMULATOR" if receipt.name == "emulator_submission.json" else "QPU"
    if (record.get("manifest_sha256") != manifest_hash or record.get("backend") != expected_backend
            or (expected_backend == "EMULATOR" and record.get("device_name") != "EMU_FRESNEL")):
        raise ValueError("제출 기록이 현재 사례 manifest 또는 실행 장치 종류와 일치하지 않습니다.")
    if not record.get("batch_id"):
        raise SubmissionUncertain("기존 배치 ID가 없어 복구할 수 없습니다. Pasqal 포털에서 확인하세요.")
    sequence_path = Path(run_dir) / "sequence.json"
    if not sequence_path.exists() or sha256(sequence_path.read_bytes()).hexdigest() != record.get("sequence_sha256"):
        raise ValueError("제출된 sequence.json이 변경되었거나 누락되었습니다.")
    return _restore(record, receipt, connection)
