"""정확성 테스트는 작은 논리 모델만 사용하며, 합성 연구 입력을 쓰지 않는다."""
from itertools import product

import numpy as np
import pytest
from scipy.optimize import Bounds, LinearConstraint, milp
from scipy.sparse import lil_matrix

from bike_research.encoding import (
    EncodingLimitError,
    InfeasibleModelError,
    decode_operational_mis,
    encode_operational_mis,
)


def case(coefficients, lower=None, upper=None, requests=None):
    """선형 제약 하나를 가진 작은 테스트 사례를 만든다."""
    return {
        "case_id": "unit-test-only",
        "candidates": [
            {"id": key, "request_id": requests.get(key, key) if requests else key}
            for key in sorted(coefficients)
        ],
        "constraints": [
            {"id": "inventory", "coefficients": coefficients, "lower": lower, "upper": upper}
        ],
    }


def exact_graph_solution(graph):
    """인코딩된 그래프의 최대 독립집합을 MILP로 정확히 구한다."""
    nodes = [node["id"] for node in graph["nodes"]]
    indices = {node: index for index, node in enumerate(nodes)}

    # 각 엣지를 x_u + x_v <= 1 제약으로 바꾼다.
    matrix = lil_matrix((len(graph["edges"]), len(nodes)))
    for index, edge in enumerate(graph["edges"]):
        matrix[index, indices[edge["source"]]] = 1
        matrix[index, indices[edge["target"]]] = 1

    # 선택 수를 최대화한다.
    result = milp(
        -np.ones(len(nodes)),
        integrality=np.ones(len(nodes)),
        bounds=Bounds(0, 1),
        constraints=LinearConstraint(matrix.tocsr(), -np.inf, 1),
        options={"time_limit": 20},
    )

    # 최적해가 증명되어야 테스트 기준으로 쓸 수 있다.
    assert result.status == 0
    return [node for node, value in zip(nodes, result.x) if value > 0.5]


def test_capacity_two_of_three_is_exact_not_pairwise_relaxation():
    """3개 중 2개 허용 제약을 쌍 완화 없이 정확히 인코딩한다."""
    graph = encode_operational_mis(case({"a": 1, "b": 1, "c": 1}, upper=2))
    selected = exact_graph_solution(graph)
    decoded = decode_operational_mis(graph, selected)
    assert decoded["operational_feasible"]
    assert decoded["selected_candidate_count"] == 2
    assert len(selected) == graph["encoding_map"]["objective_offset"] + 2


def test_positive_dependency_can_enable_second_candidate():
    """한 후보의 공급 덕분에 다른 후보가 가능해지는 경우를 보존한다."""
    graph = encode_operational_mis(case({"a": 1, "b": -1}, lower=0))
    selected = exact_graph_solution(graph)
    decoded = decode_operational_mis(graph, selected)
    assert decoded["operational_feasible"]
    assert decoded["selected_candidate_ids"] == ["a", "b"]
    assert len(selected) == graph["encoding_map"]["objective_offset"] + 2

    # 공급 없이 b만 고르면 불가능해야 한다.
    invalid = decode_operational_mis(graph, ["reward:b"])
    assert not invalid["operational_feasible"]


def test_same_request_is_exclusive():
    """같은 요청의 후보는 하나만 고를 수 있다."""
    graph = encode_operational_mis(
        case({"a": 1, "b": 1}, upper=2, requests={"a": "r", "b": "r"})
    )
    selected = exact_graph_solution(graph)
    assert decode_operational_mis(graph, selected)["selected_candidate_count"] == 1


def test_all_assignments_of_signed_row_match_compiled_cnf():
    """모든 배정에서 산술 판정과 CNF 판정이 일치한다."""
    source = case({"a": 2, "b": -3, "c": 1}, lower=-1, upper=1)
    graph = encode_operational_mis(source)
    clauses = graph["encoding_map"]["clauses"]

    # 8가지 배정을 전수 비교한다.
    for bits in product((0, 1), repeat=3):
        assignment = dict(zip("abc", bits))
        arithmetic = -1 <= 2 * bits[0] - 3 * bits[1] + bits[2] <= 1
        logical = all(
            any(assignment[key] == value for key, value in clause) for clause in clauses
        )
        assert arithmetic == logical


def test_expansion_limit_does_not_return_truncated_graph():
    """크기 한도를 넘으면 잘린 그래프를 반환하지 않고 오류를 낸다."""
    with pytest.raises(EncodingLimitError, match="vertices"):
        encode_operational_mis(case({"a": 1, "b": 1, "c": 1}, upper=2), max_nodes=3)


def test_support_limit_and_redundant_large_row():
    """지지 크기 한도를 지키되, 항상 만족하는 큰 행은 한도와 무관하게 건너뛴다."""
    source = case({f"v{x}": 1 for x in range(13)}, upper=6)
    with pytest.raises(EncodingLimitError, match="support"):
        encode_operational_mis(source, max_support=12)

    # 상한을 13으로 올리면 중복 행이 되어 절이 생기지 않는다.
    source["constraints"][0]["upper"] = 13
    graph = encode_operational_mis(source)
    assert graph["metadata"]["clause_count"] == 0


def test_unknown_candidate_and_impossible_bounds_fail():
    """없는 후보와 만족 불가능한 상하한은 오류로 처리한다."""
    source = case({"a": 1}, upper=1)
    source["constraints"][0]["coefficients"]["unknown"] = 1
    with pytest.raises(ValueError, match="unknown candidate"):
        encode_operational_mis(source)
    with pytest.raises(InfeasibleModelError):
        encode_operational_mis(case({"a": 1}, lower=2))


def test_bad_graph_sample_is_not_silently_repaired():
    """잘못된 표본을 몰래 수선하지 않고 그대로 불가능으로 보고한다."""
    graph = encode_operational_mis(case({"a": 1, "b": 1}, upper=1))

    # 모든 노드를 고른 표본은 독립집합이 아니다.
    selected = [node["id"] for node in graph["nodes"]]
    decoded = decode_operational_mis(graph, selected)
    assert not decoded["independent"]
    assert not decoded["operational_feasible"]
    assert decoded["conflicting_edges"]

    # 없는 노드를 고른 표본도 불가능이다.
    assert not decode_operational_mis(graph, ["unknown"])["operational_feasible"]


def test_empty_case_and_resource_conflicts():
    """빈 사례와 자원 충돌을 올바르게 처리한다."""
    # 빈 사례는 빈 그래프가 되고 그대로 실행 가능하다.
    graph = encode_operational_mis({"candidates": [], "constraints": []})
    assert graph["nodes"] == [] and graph["edges"] == []
    assert decode_operational_mis(graph, [])["operational_feasible"]

    # 자원 충돌이 있으면 둘 중 하나만 고를 수 있다.
    source = case({"a": 1, "b": 1}, upper=2)
    source["resource_conflicts"] = [{"source": "a", "target": "b"}]
    graph = encode_operational_mis(source)
    assert decode_operational_mis(
        graph, exact_graph_solution(graph))["selected_candidate_count"] == 1

    # 자기 자신과의 자원 충돌은 거절한다.
    source["resource_conflicts"] = [{"source": "a", "target": "a"}]
    with pytest.raises(ValueError, match="two different"):
        encode_operational_mis(source)


def test_request_groups_prove_large_support_row_redundant():
    """요청 그룹만으로 중복임이 증명되는 행은 지지 한도에 걸리지 않는다."""
    source = case(
        {"a": 1, "b": 1, "c": 1, "d": 1},
        upper=2,
        requests={"a": "r1", "b": "r1", "c": "r2", "d": "r2"},
    )
    graph = encode_operational_mis(source, max_support=1)

    # 지지 크기 한도가 아주 작아도 이 행은 두 요청 그룹에서 따라 나온다.
    assert graph["metadata"]["clause_count"] == 2
    decoded = decode_operational_mis(graph, exact_graph_solution(graph))
    assert decoded["operational_feasible"] and decoded["selected_candidate_count"] == 2


def test_group_pruning_and_cube_reduction_preserve_entire_feasible_set():
    """그룹 가지치기와 큐브 축소 후에도 실행 가능 집합 전체가 보존된다."""
    source = case(
        {"a": 2, "b": -2, "c": 1, "d": -1},
        lower=-1, upper=1,
        requests={"a": "r1", "b": "r1", "c": "r2", "d": "r2"},
    )
    source["constraints"].append({
        "id": "second", "coefficients": {"a": -1, "b": 1, "c": -2, "d": 2},
        "lower": -1, "upper": 2,
    })
    source["resource_conflicts"] = [{"source": "b", "target": "d"}]
    graph = encode_operational_mis(source)
    best = 0

    # 16가지 배정을 전수 비교하고, 그중 최대 선택 수를 구한다.
    for bits in product((0, 1), repeat=4):
        assignment = dict(zip("abcd", bits))

        # 요청 배타성과 자원 충돌을 직접 계산한다.
        feasible = bits[0] + bits[1] <= 1 and bits[2] + bits[3] <= 1
        feasible &= bits[1] + bits[3] <= 1

        # 선형 제약을 직접 계산한다.
        for row in source["constraints"]:
            activity = sum(value * assignment[key] for key, value in row["coefficients"].items())
            feasible &= (row["lower"] is None or activity >= row["lower"])
            feasible &= (row["upper"] is None or activity <= row["upper"])

        # CNF 판정과 비교한다.
        cnf = all(
            any(assignment[key] == value for key, value in clause)
            for clause in graph["encoding_map"]["clauses"]
        )
        assert feasible == cnf
        if feasible:
            best = max(best, sum(bits))

    # 그래프 최적해가 전수 조사의 최대값과 일치해야 한다.
    selected = exact_graph_solution(graph)
    decoded = decode_operational_mis(graph, selected)
    assert decoded["operational_feasible"] and decoded["selected_candidate_count"] == best
    assert len(selected) == graph["encoding_map"]["objective_offset"] + best


def test_lift_is_bidirectional_and_carries_objective():
    """운영 해를 그래프로 옮겼다가 복원해도 같은 해와 목적값을 얻는다."""
    from bike_research.encoding import lift_operational_selection

    graph = encode_operational_mis(case({"a": 1, "b": 1, "c": 1}, upper=2))

    # 실행 가능한 선택은 옮긴 뒤 복원해도 그대로여야 한다.
    for chosen in ([], ["a"], ["a", "c"]):
        lifted = lift_operational_selection(graph, chosen)
        decoded = decode_operational_mis(graph, lifted)
        assert decoded["independent"] and decoded["clause_complete"]
        assert decoded["operational_feasible"]
        assert decoded["selected_candidate_ids"] == sorted(chosen)
        assert len(lifted) == graph["encoding_map"]["objective_offset"] + len(chosen)

    # 실행 불가능한 선택은 옮길 수 없다.
    with pytest.raises(ValueError, match="infeasible"):
        lift_operational_selection(graph, ["a", "b", "c"])


def test_decoder_checks_original_constraints_in_addition_to_cnf():
    """복호화는 CNF뿐 아니라 원래 제약도 따로 검사한다."""
    from bike_research.encoding import lift_operational_selection

    graph = encode_operational_mis(case({"a": 1, "b": 1}, upper=1))
    lifted = lift_operational_selection(graph, ["a"])

    # 인코딩 후 원본 제약만 바꾸면 CNF는 그대로여도 위반이 잡혀야 한다.
    graph["source_case"]["constraints"][0]["upper"] = 0
    decoded = decode_operational_mis(graph, lifted)
    assert not decoded["operational_feasible"]
    assert decoded["source_constraint_violations"] == ["inventory"]
