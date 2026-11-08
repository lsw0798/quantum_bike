"""QPU 과금 없이 실행하는 계약 테스트. 작은 그래프는 검증용 fixture이다."""
import json
import ast
from hashlib import sha256
from pathlib import Path

import numpy as np
import pytest

from bike_research.qpu import (
    EmbeddingRejected,
    compiled_energy_certificate,
    evaluate_bitstrings,
    geometry_certificate,
    load_graph,
    load_case_bound_graph,
    load_submission_receipt,
    recover_qpu_job,
    prepare_qpu_program,
    read_coordinates,
    submit_qpu,
)


@pytest.fixture
def path_graph(tmp_path):
    """a-b-c 경로 그래프와, 해시로 묶인 case.json을 임시 폴더에 만든다."""
    case_path = tmp_path / "case.json"
    case_path.write_text(json.dumps({"case_id": "test_case"}), encoding="utf-8")

    payload = {
        "nodes": [{"id": "a"}, {"id": "b"}, {"id": "c"}],
        "edges": [{"source": "a", "target": "b"}, {"source": "b", "target": "c"}],
        "metadata": {"representation": "exact_declared_constraints",
                     "case_sha256": sha256(case_path.read_bytes()).hexdigest()},
    }
    path = tmp_path / "graph.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def test_ideal_energy_certificate_preserves_path_mis(path_graph):
    """일직선 배치의 detuning이 비엣지 합과 최소 엣지 사이에 놓인다."""
    graph = load_graph(path_graph)
    result = geometry_certificate(graph, [[-1, 0], [0, 0], [1, 0]])
    assert 1 / 64 < result["final_detuning"] < 1
    assert result["native_adjacency_exact"]


def test_wrong_geometry_rejected_without_changing_edges(path_graph):
    """엣지와 맞지 않는 배치는 엣지를 고치지 않고 거절한다."""
    graph = load_graph(path_graph)
    with pytest.raises(EmbeddingRejected):
        geometry_certificate(graph, [[0, 0], [3, 0], [0.1, 0]])


def test_nonfinite_or_colliding_atoms_rejected(path_graph):
    """유한하지 않은 좌표와 겹치는 원자를 거절한다."""
    graph = load_graph(path_graph)
    with pytest.raises(ValueError):
        geometry_certificate(graph, [[0, 0], [1, 0], [np.nan, 0]])
    with pytest.raises(EmbeddingRejected):
        geometry_certificate(graph, [[0, 0], [0, 0], [1, 0]])


def test_external_coordinates_preserve_node_identity(path_graph, tmp_path):
    """외부 좌표를 파일 순서가 아니라 그래프 노드 순서대로 읽는다."""
    path = tmp_path / "coords.json"
    path.write_text(json.dumps({"coordinates": {"c": [1, 0], "a": [-1, 0], "b": [0, 0]}}))
    assert read_coordinates(
        path, load_graph(path_graph).node_ids).tolist() == [[-1, 0], [0, 0], [1, 0]]


def test_decode_uses_explicit_bit_order_and_checks_conflicts(path_graph):
    """지정한 비트 순서로 복호화하고 충돌을 검사한다."""
    graph = load_graph(path_graph)
    result = evaluate_bitstrings(
        graph, {"101": 4, "110": 2}, node_order=["c", "b", "a"],
        operational_validator=lambda ids: {"feasible": set(ids) == {"a", "c"}},
    )

    # 빈도가 높은 "101"은 c와 a를 고른 독립집합이다.
    assert result[0]["selected_nodes"] == ["c", "a"]
    assert result[0]["graph_independent_set_size"] == 2

    # "110"은 c와 b를 함께 골라 독립집합이 아니다.
    assert result[1]["graph_independent"] is False
    assert result[1]["graph_independent_set_size"] is None


def test_disabled_submit_never_touches_connection(tmp_path):
    """제출이 꺼져 있으면 연결 객체에 접근하지 않고 기록도 만들지 않는다."""

    class PoisonConnection:
        """속성에 접근하기만 해도 실패하는 연결 객체."""

        # 어떤 속성 접근이든 네트워크 사용 시도로 보고 테스트를 실패시킨다.
        def __getattribute__(self, name):
            raise AssertionError("Network-capable connection was accessed")

    with pytest.raises(PermissionError):
        submit_qpu(None, PoisonConnection(), device_name="FRESNEL",
                   receipt_path=tmp_path / "receipt.json")
    assert not (tmp_path / "receipt.json").exists()


def test_stale_graph_blocked_before_sdk_preparation(path_graph):
    """사례가 바뀐 그래프는 SDK 준비 전에 막는다."""
    path_graph.with_name("case.json").write_text('{"case_id":"new_case"}')
    with pytest.raises(ValueError, match="case_sha256"):
        prepare_qpu_program(path_graph)


def test_unbound_graph_is_rejected(path_graph):
    """사례 해시가 없는 그래프를 거절한다."""
    document = json.loads(path_graph.read_text())
    del document["metadata"]["case_sha256"]
    path_graph.write_text(json.dumps(document))
    with pytest.raises(ValueError, match="case_sha256"):
        load_case_bound_graph(path_graph)


def test_stale_exact_encoding_status_rejected(path_graph):
    """인코딩에 성공한 기록이 아니면 정확 인코딩 그래프를 거절한다."""
    document = json.loads(path_graph.read_text())
    document["metadata"]["representation"] = "exact_operational_mis"
    path_graph.write_text(json.dumps(document))
    path_graph.with_name("encoding_status.json").write_text(json.dumps({
        "status": "limit_exceeded",
        "case_sha256": document["metadata"]["case_sha256"],
    }))
    with pytest.raises(ValueError, match="인코딩"):
        load_case_bound_graph(path_graph)


def test_receipt_rejects_mismatched_graph_without_remote_access(path_graph, tmp_path):
    """그래프가 다른 제출 기록은 원격 접근 없이 거절한다."""
    graph = load_case_bound_graph(path_graph)
    receipt = tmp_path / "receipt.json"
    receipt.write_text(json.dumps({"backend": "QPU", "status": "submitted",
                                   "graph_sha256": "stale_hash"}))
    with pytest.raises(ValueError, match="해시"):
        load_submission_receipt(receipt, graph)

    # 조회를 허용하지 않으면 복구도 막힌다.
    with pytest.raises(PermissionError):
        recover_qpu_job(path_graph, receipt, object())


def test_notebook_has_no_executed_cells_and_remote_flags_default_off():
    """노트북에 실행 결과가 없고 원격 플래그가 모두 꺼져 있는지 확인한다."""
    # 노트북을 읽기만 한다. 자격 증명 값은 출력/복사하지 않는다.
    notebook = json.loads(
        (Path(__file__).parents[1] / "quantum_bike_1105.ipynb").read_text(encoding="utf-8"))
    assignments = {}

    # 코드 셀별로 실행 흔적을 검사하고 플래그 대입을 수집한다.
    for cell in notebook["cells"]:
        if cell["cell_type"] != "code":
            continue
        assert cell.get("execution_count") is None
        assert not cell.get("outputs")
        tree = ast.parse("".join(cell["source"]))
        for node in tree.body:
            if isinstance(node, ast.Assign) and isinstance(node.value, ast.Constant):
                for target in node.targets:
                    if (isinstance(target, ast.Name)
                            and target.id.startswith(("ALLOW_", "ENABLE_", "FETCH_", "RECOVER_"))):
                        assignments[target.id] = node.value.value

    # 원격 동작 플래그는 모두 False여야 한다.
    for flag in ["ALLOW_PASQAL_CONNECTION", "ENABLE_PAID_QPU_SUBMISSION",
                 "FETCH_EXISTING_QPU_RESULTS", "RECOVER_EXISTING_QPU_JOB"]:
        assert assignments[flag] is False


def test_relaxation_requires_explicit_flag(path_graph):
    """쌍 완화 그래프는 명시적 허용 없이 준비할 수 없다."""
    payload = json.loads(path_graph.read_text())
    payload["metadata"]["representation"] = "pairwise_relaxation"
    path_graph.write_text(json.dumps(payload))
    with pytest.raises(ValueError, match="allow_pairwise_relaxation"):
        prepare_qpu_program(path_graph)


def test_local_qoolqit_program_and_compile_only(path_graph, tmp_path):
    """오프라인 장치로 프로그램을 만들고 컴파일까지만 수행한다."""
    pytest.importorskip("qoolqit")
    from qoolqit import AnalogDevice

    # 외부 좌표를 지정해 프로그램을 준비한다.
    coordinates = tmp_path / "coordinates.json"
    coordinates.write_text(json.dumps({"coordinates": {
        "a": [-1, 0], "b": [0, 0], "c": [1, 0],
    }}))
    prepared = prepare_qpu_program(path_graph, coordinates_path=coordinates)
    assert prepared.program.register.n_qubits == 3
    assert not prepared.program.is_compiled

    # 컴파일 후 비트 순서와 에너지 조건을 확인한다.
    prepared.program.compile_to(device=AnalogDevice(), profile="max_energy")
    assert prepared.program.is_compiled
    assert list(prepared.program.compiled_sequence.register.qubits) == ["0", "1", "2"]
    assert compiled_energy_certificate(
        prepared, prepared.program.compiled_sequence)["commanded_final_energy_certificate"]


def test_qoolqit_interaction_embedder_api_locally(path_graph):
    """QoolQit 수치 임베더를 로컬에서 실행해 배치를 인증한다."""
    pytest.importorskip("qoolqit")
    prepared = prepare_qpu_program(path_graph, seed=42, maxiter=3000)
    assert prepared.embedding_report["native_adjacency_exact"]
    assert prepared.embedding_report["method"] == "qoolqit_interaction_fit_then_exact_checks"
