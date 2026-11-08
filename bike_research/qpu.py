"""QoolQit 비가중 MIS 준비·검증·QPU 제출.

로컬 준비와 유료 제출을 분리한다. 이 모듈을 import해도 인증/원격 호출은 없다.
공식 API 기준: qoolqit 1.5.0 / pasqal-cloud 0.24.0.
https://docs.pasqal.com/qoolqit/fundamentals/execution/execution/

기하 배치는 일반 그래프의 정확 환원이 아니다. 원래 엣지를 모두 보존하고
비엣지 상호작용의 충분조건까지 통과한 직접 배치만 제출할 수 있다.
"""
from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from hashlib import sha256
from pathlib import Path
from typing import Any
import json
import math
import time

import numpy as np


class EmbeddingRejected(ValueError):
    """직접 배치로 원래 MIS를 보존한다고 확인하지 못한 경우."""


@dataclass
class GraphInput:
    """파일에서 읽은 그래프. 노드 순서와 원본 바이트 해시를 함께 보관한다."""

    node_ids: tuple[str, ...]
    edges: frozenset[tuple[int, int]]
    source: dict[str, Any]
    sha256: str


@dataclass
class PreparedQPU:
    """로컬에서 준비한 프로그램과 배치 검증 결과."""

    graph: GraphInput
    program: Any
    coordinates: np.ndarray
    embedding_report: dict[str, Any]
    node_order: tuple[str, ...]


def load_graph(path: str | Path) -> GraphInput:
    """노드 순서를 고정한다. 고립 정점도 버리지 않는다."""
    raw = Path(path).read_bytes()
    document = json.loads(raw)

    # 노드는 비어 있지 않고 ID가 유일해야 한다.
    ids = tuple(str(node["id"]) for node in document["nodes"])
    if not ids or len(ids) != len(set(ids)):
        raise ValueError("그래프는 비어 있지 않아야 하며 노드 ID는 유일해야 합니다.")

    # 가중치가 1이 아닌 노드는 지원하지 않는다.
    for node in document["nodes"]:
        if float(node.get("weight", 1)) != 1:
            raise ValueError("이 파이프라인은 모든 노드 가중치가 1인 MIS만 지원합니다.")

    # 엣지를 노드 번호 쌍으로 바꾼다. 자기 루프는 거절한다.
    index = {node_id: i for i, node_id in enumerate(ids)}
    edges = set()
    for edge in document["edges"]:
        left, right = index[str(edge["source"])], index[str(edge["target"])]
        if left == right:
            raise ValueError("자기 루프는 허용하지 않습니다.")
        edges.add(tuple(sorted((left, right))))
    return GraphInput(ids, frozenset(edges), document, sha256(raw).hexdigest())


def load_case_bound_graph(path: str | Path) -> GraphInput:
    """현재 case.json과 같은 실행에서 생성된 그래프인지 바이트 해시로 검증한다."""
    path = Path(path)
    graph = load_graph(path)

    # 그래프 옆에 사례 파일이 있어야 한다.
    case_path = path.with_name("case.json")
    if not case_path.is_file():
        raise ValueError("그래프 옆에 현재 case.json이 없습니다.")

    # 그래프에 기록된 해시가 현재 사례 파일과 같아야 한다.
    case_hash = sha256(case_path.read_bytes()).hexdigest()
    metadata = graph.source.get("metadata", {})
    if metadata.get("case_sha256") != case_hash:
        raise ValueError("그래프의 case_sha256가 없거나 현재 case.json과 다릅니다. 다시 생성하세요.")

    # 정확 인코딩 그래프는 현재 사례의 성공 기록까지 확인한다.
    if metadata.get("representation") == "exact_operational_mis":
        status_path = path.with_name("encoding_status.json")
        if not status_path.is_file():
            raise ValueError("정확 인코딩의 현재 encoding_status.json이 없습니다.")
        status = json.loads(status_path.read_text(encoding="utf-8"))
        if status.get("status") != "encoded" or status.get("case_sha256") != case_hash:
            raise ValueError("현재 사례의 성공한 정확 인코딩 기록이 아닙니다.")
    return graph


def geometry_certificate(
    graph: GraphInput, coordinates: Sequence[Sequence[float]], tolerance: float = 1e-9
) -> dict[str, Any]:
    """직접 배치의 엣지와 최종 고전 Hamiltonian 충분조건을 검사한다.

    J_ij = 1 / r_ij**6, H = -delta * sum(x) + sum(J_ij*x_i*x_j).
    tail = 모든 비엣지 J의 합, edge_min = 가장 약한 엣지 J.
    tail < delta < edge_min이면:
      (1) 충돌 정점을 제거하면 에너지가 감소하고
      (2) 독립집합끼리는 정점 수가 큰 해의 에너지가 더 낮다.
    보수적인 충분조건이며 실패가 배치 불가능성의 증명은 아니다.
    실제 노이즈·유한 진화시간의 최적해 보장은 제공하지 않는다.
    """
    # 좌표 배열의 모양과 값을 검증한다.
    coords = np.asarray(coordinates, dtype=float)
    count = len(graph.node_ids)
    if coords.shape != (count, 2) or not np.isfinite(coords).all():
        raise ValueError("좌표는 노드 순서와 일치하는 유한한 N×2 배열이어야 합니다.")

    # 모든 원자 쌍의 상호작용을 계산한다.
    interactions = {}
    for left in range(count):
        for right in range(left + 1, count):
            distance = float(np.linalg.norm(coords[left] - coords[right]))
            if distance <= tolerance:
                raise EmbeddingRejected("두 원자의 좌표가 겹칩니다.")
            interaction = distance ** -6
            if not math.isfinite(interaction):
                raise EmbeddingRejected("상호작용 계산이 유한하지 않습니다.")
            interactions[(left, right)] = interaction

    # 엣지와 비엣지의 상호작용을 나눠 요약한다.
    edge_values = [j for pair, j in interactions.items() if pair in graph.edges]
    nonedge_values = [j for pair, j in interactions.items() if pair not in graph.edges]
    tail = float(sum(nonedge_values))
    minimum_edge = min(edge_values) if edge_values else None
    maximum_nonedge = max(nonedge_values, default=0.0)

    # 가장 약한 엣지가 가장 강한 비엣지보다 강해야 한다.
    if minimum_edge is not None and minimum_edge <= maximum_nonedge + tolerance:
        raise EmbeddingRejected(
            "엣지/비엣지의 거리 구분 실패. 엣지를 삭제하지 않습니다. "
            "정확한 외부 배치 또는 별도 증명된 그래프 환원이 필요합니다."
        )

    # 가장 약한 엣지가 비엣지 상호작용의 합보다도 강해야 한다.
    if minimum_edge is not None and minimum_edge <= tail + tolerance:
        raise EmbeddingRejected(
            "거리상 엣지는 맞지만 비엣지 상호작용 합이 너무 큽니다. "
            "현재 직접 배치의 비가중 MIS 목적 보존을 인증하지 못했습니다."
        )

    # 최종 detuning을 두 경계의 중간으로 정한다. 엣지가 없으면 tail + 1이다.
    delta = (tail + minimum_edge) / 2 if minimum_edge is not None else tail + 1.0

    # detuning 기준의 상호작용 그래프가 원본과 같은지 확인한다.
    represented = {pair for pair, value in interactions.items() if value > delta}
    if represented != set(graph.edges):
        raise EmbeddingRejected("최종 detuning 기준의 상호작용 그래프가 원본과 다릅니다.")

    return {
        "native_adjacency_exact": True,
        "sufficient_final_energy_certificate": True,
        "minimum_edge_interaction": minimum_edge,
        "maximum_nonedge_interaction": maximum_nonedge,
        "total_nonedge_interaction_bound": tail,
        "final_detuning": delta,
        "n_logical_nodes": count,
        "n_physical_atoms": count,
        "auxiliary_atom_count": 0,
        "interpretation": "ideal final Hamiltonian only; not a finite-time/noise guarantee",
    }


def read_coordinates(path: str | Path, node_ids: Sequence[str]) -> np.ndarray:
    """외부 좌표 JSON: {'coordinates': {'node_id': [x,y], ...}}.

    좌표는 QoolQit 무차원 원자 좌표다. 대여소 위경도를 그대로 쓰지 않는다.
    이 함수는 노드를 추가하는 gadget 변환의 복호화를 대신하지 않는다.
    """
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    coordinates = payload["coordinates"]

    # 좌표의 노드 집합은 그래프와 정확히 같아야 한다.
    if set(coordinates) != set(node_ids):
        raise ValueError("외부 배치의 노드 집합이 그래프와 정확히 일치해야 합니다.")

    # 그래프의 노드 순서대로 배열을 만든다.
    return np.asarray([coordinates[node] for node in node_ids], dtype=float)


def _disjoint_clique_coordinates(graph: GraphInput) -> np.ndarray | None:
    """서로 분리된 요청 clique는 직접 배치한다. 그래프 자체는 바꾸지 않는다."""
    # 인접 집합을 만든다.
    neighbors = {i: set() for i in range(len(graph.node_ids))}
    for left, right in graph.edges:
        neighbors[left].add(right)
        neighbors[right].add(left)

    unseen = set(neighbors)
    components = []

    # 연결 요소를 찾으면서 각 요소가 clique인지 확인한다.
    while unseen:
        component, queue = set(), [min(unseen)]
        while queue:
            node = queue.pop()
            if node in component:
                continue
            component.add(node)
            queue.extend(neighbors[node] - component)
        unseen -= component

        # clique가 아닌 요소가 있으면 이 배치를 쓸 수 없다.
        if any(neighbors[node] != component - {node} for node in component):
            return None
        components.append(sorted(component))

    # 요소들을 격자에 놓고, 각 요소의 원자를 작은 원 위에 놓는다.
    coords = np.zeros((len(graph.node_ids), 2))
    spacing = 3.0
    columns = math.ceil(math.sqrt(len(components)))
    for group, component in enumerate(components):
        center = np.array([(group % columns) * spacing, (group // columns) * spacing])
        for position, node in enumerate(component):
            angle = 2 * math.pi * position / len(component)
            offset = np.array([math.cos(angle), math.sin(angle)]) * 0.4
            coords[node] = center + (offset if len(component) > 1 else 0)

    # 전체 무게중심을 원점으로 옮긴다.
    return coords - coords.mean(axis=0)


def prepare_qpu_program(
    graph_path: str | Path,
    *,
    coordinates_path: str | Path | None = None,
    allow_pairwise_relaxation: bool = False,
    seed: int = 42,
    maxiter: int = 5000,
    max_embedding_nodes: int = 80,
    duration: float = 40.0,
) -> PreparedQPU:
    """로컬에서만 배치·펄스 객체를 만든다. 인증·QPU·에뮬레이터 호출 없음.

    max_embedding_nodes는 로컬 탐색 비용의 한도이며 장치 용량 주장이 아니다.
    duration은 무차원 실험 파라미터이며 최적 schedule이라는 보장은 없다.
    """
    graph = load_case_bound_graph(graph_path)

    # 운영 제약과 동치인 그래프가 아니면 명시적 허용이 있어야 한다.
    representation = graph.source.get("metadata", {}).get("representation")
    exact_representations = {"exact_operational_mis", "exact_declared_constraints"}
    if representation not in exact_representations and not allow_pairwise_relaxation:
        raise ValueError(
            "전체 운영 제약과 동치라고 확인된 그래프가 아닙니다. "
            "그래프 자체의 MIS 실험이라면 allow_pairwise_relaxation=True를 명시하세요."
        )

    # 외부 좌표 없이 큰 그래프를 배치하려 하면 중단한다.
    if len(graph.node_ids) > max_embedding_nodes and coordinates_path is None:
        raise EmbeddingRejected(
            f"로컬 직접 배치 탐색 한도 {max_embedding_nodes}개를 초과했습니다. "
            "원본을 임의 절단하지 않습니다. 외부 검증 좌표를 제공하세요."
        )

    # 실행 시간을 검증한다.
    if not math.isfinite(duration) or duration <= 0:
        raise ValueError("duration은 양의 유한한 값이어야 합니다.")

    # 선택적 SDK 의존성은 로컬 준비를 실제 호출할 때만 로드한다.
    from qoolqit import Drive, InterpolatedWaveform, QuantumProgram, Register
    from qoolqit.embedding import InteractionEmbedder, InteractionEmbedderConfig

    started = time.perf_counter()

    # 원자 좌표를 정한다. 외부 좌표, 단일 원자, clique 배치, 수치 임베딩 순으로 시도한다.
    if coordinates_path is not None:
        coords = read_coordinates(coordinates_path, graph.node_ids)
        method = "external_same_node_coordinates"
    elif len(graph.node_ids) == 1:
        coords = np.zeros((1, 2))
        method = "single_atom"
    elif (clique_coords := _disjoint_clique_coordinates(graph)) is not None:
        coords = clique_coords
        method = "exact_disjoint_cliques_then_energy_checks"
    else:
        # 인접 행렬을 목표로 수치 임베딩을 실행한다.
        target = np.zeros((len(graph.node_ids), len(graph.node_ids)))
        for left, right in graph.edges:
            target[left, right] = target[right, left] = 1.0
        embedder = InteractionEmbedder(
            InteractionEmbedderConfig(seed=seed, maxiter=maxiter)
        )
        embedded = embedder.embed(target)

        # 행렬 임베더는 행 번호를 0..N-1로 매긴다. 그 대응을 그대로 유지한다.
        coords = np.asarray([embedded.coords[i] for i in range(len(graph.node_ids))])
        method = "qoolqit_interaction_fit_then_exact_checks"

    # 배치를 검증하고 준비 과정을 인증서에 기록한다.
    certificate = geometry_certificate(graph, coords)
    certificate.update(
        method=method,
        seed=seed,
        maxiter=maxiter,
        preparation_seconds=time.perf_counter() - started,
        graph_sha256=graph.sha256,
        graph_representation=representation,
    )

    # 인증된 detuning으로 레지스터와 펄스를 만든다.
    delta = certificate["final_detuning"]
    register = Register.from_coordinates(coords)
    drive = Drive(
        amplitude=InterpolatedWaveform(duration, [0.0, delta, 0.0]),
        detuning=InterpolatedWaveform(duration, [-delta, 0.0, delta]),
    )
    program = QuantumProgram(register, drive)
    return PreparedQPU(graph, program, coords, certificate, graph.node_ids)


def compiled_energy_certificate(prepared: PreparedQPU, compiled: Any) -> dict[str, Any]:
    """컴파일된 실제 C6·좌표·최종 detuning으로 충분조건을 다시 검사한다.

    PulsER 명령 파형을 검사한다. 장치 전달함수/노이즈의 실측 검증은 아니다.
    """
    from pulser.sampler import sample

    # 컴파일 후에도 비트 순서가 같아야 한다.
    ids = list(compiled.register.qubits)
    if [str(node) for node in ids] != [str(i) for i in range(len(prepared.node_order))]:
        raise EmbeddingRejected("컴파일 후 비트 순서가 바뀌었습니다.")

    # 단일 전역 Rydberg 채널만 지원한다.
    channels = sample(compiled, modulation=False).channel_samples
    if len(channels) != 1:
        raise EmbeddingRejected("단일 전역 Rydberg 채널만 검증할 수 있습니다.")
    name, values = next(iter(channels.items()))
    channel = compiled.declared_channels[name]
    if channel.addressing != "Global" or channel.basis != "ground-rydberg":
        raise EmbeddingRejected("지원하지 않는 채널 종류입니다.")

    # 펄스 끝에서 amplitude는 0이고 detuning은 양수여야 한다.
    final_delta = float(values.det.as_array()[-1])
    final_amplitude = float(values.amp.as_array()[-1])
    if final_delta <= 0 or abs(final_amplitude) > max(1e-8, abs(final_delta) * 1e-8):
        raise EmbeddingRejected("최종 amplitude=0, detuning>0 조건을 만족하지 않습니다.")

    # 실제 C6와 좌표로 엣지·비엣지 상호작용을 다시 계산한다.
    coordinates = np.asarray(list(compiled.register.qubits.values()))
    c6 = float(compiled.device.interaction_coeff)
    edge_values, nonedge_values = [], []
    for left in range(len(ids)):
        for right in range(left + 1, len(ids)):
            distance = float(np.linalg.norm(coordinates[left] - coordinates[right]))
            interaction = c6 / distance**6
            (edge_values if (left, right) in prepared.graph.edges
             else nonedge_values).append(interaction)

    # 최종 detuning이 비엣지 합과 최소 엣지 사이에 있어야 한다.
    tail = float(sum(nonedge_values))
    minimum_edge = min(edge_values) if edge_values else None
    if not tail < final_delta or (minimum_edge is not None and not final_delta < minimum_edge):
        raise EmbeddingRejected("실제 컴파일된 detuning이 비엣지 합과 최소 엣지 사이에 있지 않습니다.")

    return {
        "physical_c6_rad_us_um6": c6,
        "final_detuning_rad_per_us": final_delta,
        "final_amplitude_rad_per_us": final_amplitude,
        "nonedge_sum_rad_per_us": tail,
        "minimum_edge_rad_per_us": minimum_edge,
        "compiled_duration_ns": compiled.get_duration(),
        "commanded_final_energy_certificate": True,
    }


def submit_qpu(
    prepared: PreparedQPU,
    connection: Any,
    *,
    device_name: str,
    num_shots: int = 100,
    authorize_paid_submission: bool = False,
    receipt_path: str | Path,
    device_max_duration_ratio: float | None = None,
) -> Any:
    """명시적 활성화 후에만 실제 Pasqal QPU로 1회 제출한다.

    이 함수를 개발/검증 과정에서 호출하지 않는다. 실패 후 자동 재시도하지
    않는다. receipt가 존재하면 중복 제출을 방지하기 위해 중단한다.
    """
    # 명시적으로 허용하지 않으면 연결 객체를 건드리기 전에 중단한다.
    if authorize_paid_submission is not True:
        raise PermissionError("QPU 제출이 꺼져 있습니다. 원격 호출은 수행하지 않았습니다.")

    # 연결과 제출 인자를 검증한다.
    if connection is None:
        raise ValueError("명시적으로 생성한 Pasqal 연결이 필요합니다.")
    if not device_name or num_shots < 1 or isinstance(num_shots, bool):
        raise ValueError("실제 장치 이름과 양의 정수 num_shots가 필요합니다.")
    if int(num_shots) != num_shots:
        raise ValueError("num_shots는 정수여야 합니다.")

    # 제출 직전에 배치를 다시 검증한다.
    geometry_certificate(prepared.graph, prepared.coordinates)
    from qoolqit.devices import Device
    from qoolqit.execution import QPU, get_batch_id

    # 제출 기록의 초기값을 만든다.
    receipt = Path(receipt_path)
    receipt.parent.mkdir(parents=True, exist_ok=True)
    record = {
        "status": "preparing",
        "submitted_at_utc": None,
        "graph_sha256": prepared.graph.sha256,
        "device_name": device_name,
        "case_sha256": prepared.graph.source.get("metadata", {}).get("case_sha256"),
        "num_shots": int(num_shots),
        "backend": "QPU",
        "device_max_duration_ratio": device_max_duration_ratio,
        "node_order": list(prepared.node_order),
        "embedding": prepared.embedding_report,
    }

    # 존재 여부 검사와 생성은 하나의 배타적 파일 연산으로 처리한다.
    with receipt.open("x", encoding="utf-8") as handle:
        json.dump(record, handle, ensure_ascii=False, indent=2)
    submission_started = False

    try:
        # 실제 장치 사양으로 컴파일한다.
        device = Device.from_connection(connection, device_name)
        prepared.program.compile_to(
            device=device,
            profile="max_energy",
            device_max_duration_ratio=device_max_duration_ratio,
        )
        compiled = prepared.program.compiled_sequence

        # 컴파일 후 원자 순서가 바뀌면 복호화할 수 없으므로 중단한다.
        compiled_ids = list(compiled.register.qubits)
        if ([str(node) for node in compiled_ids]
                != [str(i) for i in range(len(prepared.node_order))]):
            raise ValueError("컴파일 후 원자 순서가 달라져 안전하게 복호화할 수 없습니다.")

        # 컴파일된 좌표와 에너지 조건을 다시 검증한다.
        physical_coords = np.asarray(list(compiled.register.qubits.values()))
        geometry_certificate(prepared.graph, physical_coords, tolerance=1e-15)
        record["compiled_energy_certificate"] = compiled_energy_certificate(prepared, compiled)
        record["device_specs"] = device.specs()

        # 제출 직전 상태를 먼저 기록해 둔다.
        record["status"] = "submitting"
        record["submitted_at_utc"] = datetime.now(timezone.utc).isoformat()
        receipt.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
        submission_started = True

        # 실제 QPU에 제출하고 작업 ID를 기록한다.
        job = QPU(connection=connection, num_shots=int(num_shots)).run(prepared.program)
        record.update(status="submitted", job_id=job.job_id(), batch_id=get_batch_id(job))
        return job
    except Exception:
        # 인증 정보가 포함될 수 있는 원격 예외 문자열을 보고서에 저장하지 않는다.
        record["status"] = "submission_outcome_unknown" if submission_started else "not_submitted"
        raise
    finally:
        # 성공·실패와 무관하게 최종 상태를 기록한다.
        receipt.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")


def load_submission_receipt(path: str | Path, graph: GraphInput) -> dict[str, Any]:
    """원래 제출 입력과 현재 입력·비트 순서가 일치하는지 로컬에서 검증한다."""
    record = json.loads(Path(path).read_text(encoding="utf-8"))

    # 제출에 성공한 QPU 기록이어야 한다.
    if record.get("backend") != "QPU" or record.get("status") != "submitted":
        raise ValueError("제출 성공과 job ID가 확인된 QPU 기록이 아닙니다.")

    # 그래프·사례 해시와 비트 순서가 제출 당시와 같아야 한다.
    if record.get("graph_sha256") != graph.sha256:
        raise ValueError("제출 당시 그래프와 현재 그래프의 해시가 다릅니다.")
    if record.get("case_sha256") != graph.source.get("metadata", {}).get("case_sha256"):
        raise ValueError("제출 당시 사례와 현재 사례의 해시가 다릅니다.")
    if record.get("node_order") != list(graph.node_ids):
        raise ValueError("제출 당시 비트 순서가 현재 그래프와 다릅니다.")

    # 작업 복구에 필요한 ID가 있어야 한다.
    if not record.get("job_id") or not record.get("batch_id"):
        raise ValueError("기존 작업 복구에 필요한 job_id 또는 batch_id가 없습니다.")
    return record


def recover_qpu_job(
    graph_path: str | Path, receipt_path: str | Path, connection: Any, *,
    authorize_remote_lookup: bool = False,
) -> Any:
    """커널 재시작 후 기존 작업을 조회한다. 새 제출·자동 재시도를 하지 않는다."""
    # 명시적으로 허용했는지와 연결이 있는지 확인한다.
    if authorize_remote_lookup is not True:
        raise PermissionError("기존 QPU 작업 조회가 꺼져 있습니다.")
    if connection is None:
        raise ValueError("명시적으로 생성한 Pasqal 연결이 필요합니다.")

    # 원격 조회 전에 로컬 입력과 제출 기록을 대조한다.
    graph = load_case_bound_graph(graph_path)
    record = load_submission_receipt(receipt_path, graph)

    from qoolqit.execution import retrieve_remote_job
    return retrieve_remote_job(connection, record["job_id"], batch_id=record["batch_id"])


def evaluate_bitstrings(
    graph: GraphInput,
    bitstrings: Mapping[str, int],
    *,
    node_order: Sequence[str] | None = None,
    operational_validator: Callable[[list[str]], dict[str, Any]] | None = None,
    candidate_decoder: Callable[[list[str]], list[str]] | None = None,
) -> list[dict[str, Any]]:
    """원시 QPU 표본을 수선하지 않고 그래프·운영 가능성을 각각 평가한다."""
    # 측정 비트 순서가 그래프 노드와 일치해야 한다.
    order = tuple(node_order) if node_order is not None else graph.node_ids
    if set(order) != set(graph.node_ids) or len(order) != len(graph.node_ids):
        raise ValueError("측정 비트 순서와 노드 집합이 일치하지 않습니다.")

    edge_ids = [(graph.node_ids[left], graph.node_ids[right]) for left, right in graph.edges]
    output = []

    # 비트열별 반복
    for bits, count in bitstrings.items():
        # 비트열 형식과 샷 빈도를 검증한다.
        if len(bits) != len(order) or set(bits) - {"0", "1"}:
            raise ValueError("비트열 길이 또는 문자가 잘못되었습니다.")
        if isinstance(count, bool) or int(count) != count or count < 0:
            raise ValueError("샷 빈도는 0 이상의 정수여야 합니다.")

        # 선택된 노드와, 함께 선택된 엣지를 찾는다.
        selected = [node for node, bit in zip(order, bits) if bit == "1"]
        selected_set = set(selected)
        conflicts = [(left, right) for left, right in edge_ids
                     if left in selected_set and right in selected_set]

        # 후보로 복호화하고 운영 가능성을 검사한다. 함수가 없으면 미검사로 표시한다.
        candidates = candidate_decoder(selected) if candidate_decoder else selected
        operational = (
            operational_validator(candidates) if operational_validator else
            {"checked": False, "reason": "operational validator not supplied"}
        )

        output.append({
            "bitstring": bits,
            "count": int(count),
            "selected_nodes": selected,
            "graph_independent": not conflicts,
            "conflict_count": len(conflicts),
            "graph_independent_set_size": len(selected) if not conflicts else None,
            "decoded_candidates": candidates,
            "operational_validation": operational,
        })

    # 빈도 내림차순, 동률이면 비트열 순으로 정렬한다.
    return sorted(output, key=lambda row: (-row["count"], row["bitstring"]))
