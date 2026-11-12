"""크기 한도를 둔 운영 모델 -> 비가중 MIS 정확 인코딩.

이것은 선택적인 정확성 검증/참조 경로이며, QPU 효율을 주장하지 않는다.
관측되지 않은 이용이나 실제 자전거를 새로 만들지 않는다. 추가되는 정점은 모두
수학적 인코딩 변수이며, 각 역할을 명시적으로 기록한다.

구성
----
이진 제약 행은 지지 집합(support) 위의 위반 배정을 하나씩 금지하여 CNF로 변환한다.
요청 배타성은 절 (not x_u or not x_v)을 추가한다. 이미 요청 배타성을 위반하는
배정에는 행 절을 더할 필요가 없다. 요청 그룹을 고려한 구간 경계로 중복 행을 없애고
금지할 배정 큐브를 줄인다. 각 단순화는 요청 절과 합쳐서 실행 가능 집합을 보존한다.
각 절에서 리터럴마다 W = 후보 수 + 1개의 서로 독립인 쌍둥이 정점을 둔다.
한 절 안의 서로 다른 리터럴은 충돌하고, 그래프 전체에서 모순되는 리터럴도 충돌한다.
x_v=true에 대응하는 별도의 보상 정점이 1단위를 기여한다.

증명 (실행 가능한 입력의 경우)
------------------------------
각 절은 최대 W개의 정점을 기여한다. 절 하나를 빠뜨리면 W를 잃는데, 이는 가능한
보상 정점 전체 N개보다 크다. 따라서 최적해는 모든 절을 대표한다. 모순되는 선택은
금지되므로 이 증인(witness)들은 만족 배정으로 확장된다. 모든 쌍둥이 정점과 호환되는
모든 참 보상 정점을 더할 수 있다. 따라서 alpha(G) = W * 절 수 + max(sum(x_v))이다.
이 최댓값 항등식을 최적이 아닌 임의의 QPU 표본에 대해 주장하지는 않는다. 아래의
복호화는 증인, 독립성, 원래 제약을 명시적으로 검사한다.

복잡도
------
CNF 컴파일은 행의 지지 크기에 대해 지수적이다. 쌍둥이 확장은 매우 커질 수 있다.
명시적 한도로 이런 경우를 그래프를 만들기 전에 거절한다.
제약, 정점, 엣지를 몰래 버리지 않는다.
"""
from __future__ import annotations

from collections import defaultdict
from fractions import Fraction
from itertools import combinations, product
import math
import time
from typing import Any


class EncodingLimitError(ValueError):
    """정확 구성이 선언된 계산 한도를 초과한 경우."""


class InfeasibleModelError(ValueError):
    """어떤 제약을 만족하는 이진 배정이 없는 경우."""


# 리터럴은 (후보 ID, 요구되는 이진값)이다.
Literal = tuple[str, int]
Clause = tuple[Literal, ...]


def _number(value: Any, label: str) -> Fraction:
    """입력된 십진수를 임의의 반올림 없이 정확하게 해석한다."""
    # 불리언은 숫자 계수로 인정하지 않는다.
    if isinstance(value, bool):
        raise ValueError(f"{label}: boolean is not a numeric coefficient")

    # 유한한 값만 분수로 변환한다.
    try:
        if not math.isfinite(float(value)):
            raise ValueError
        return Fraction(str(value))
    except (ValueError, TypeError, OverflowError, ZeroDivisionError) as exc:
        raise ValueError(f"{label}: expected a finite numeric value") from exc


def _inputs(case: dict) -> tuple[list[dict], list[dict]]:
    """사례에서 후보와 제약 행을 꺼내 기본 조건을 검증한다."""
    candidates = case.get("candidates", [])

    # 후보 ID는 유일해야 한다.
    ids = [str(candidate["id"]) for candidate in candidates]
    if len(ids) != len(set(ids)):
        raise ValueError("Candidate IDs must be unique")

    # 모든 후보에는 관측된 요청 ID가 있어야 한다.
    for candidate in candidates:
        if "request_id" not in candidate:
            raise ValueError("Every candidate needs an observed request_id")

    rows = case.get("constraints", case.get("operational_constraints", []))
    return candidates, rows


def _compile_clauses(
    case: dict, max_support: int, max_clauses: int, deadline: float = math.inf
) -> tuple[list[Clause], dict[str, list[str]]]:
    """요청 배타성·자원 충돌·선형 제약을 CNF 절과 절별 출처로 컴파일한다."""
    candidates, rows = _inputs(case)
    candidate_ids = {str(candidate["id"]) for candidate in candidates}
    request_of = {
        str(candidate["id"]): str(candidate["request_id"]) for candidate in candidates
    }
    clauses: dict[Clause, set[str]] = {}

    def add_clause(literals: list[Literal], reason: str) -> None:
        """절을 정규화해 추가한다. 항진식과 포섭되는 절은 합치거나 버린다."""
        if time.perf_counter() > deadline:
            raise EncodingLimitError("Exact encoding wall-clock limit exceeded")
        # 빈 절은 만족할 수 없으므로 모델이 불가능하다.
        normalized = tuple(sorted(set(literals)))
        if not normalized:
            raise InfeasibleModelError(f"Empty clause from {reason}")

        # 변수별로 등장한 값을 모은다.
        by_variable: dict[str, set[int]] = defaultdict(set)
        for variable, value in normalized:
            by_variable[variable].add(value)
        if any(len(values) == 2 for values in by_variable.values()):
            return  # 항진식: 항상 참이므로 제약이 없다.
        literal_set = set(normalized)

        # 논리적 포섭은 정확하다. 더 짧은 절은 그 상위 집합 절을 함의한다.
        # 단위 절 때문에 중복이 된 요청 절도 이 과정에서 제거될 수 있다.
        for existing in clauses:
            if set(existing) <= literal_set:
                clauses[existing].add(reason)
                return
        supersets = [existing for existing in clauses if literal_set < set(existing)]
        for existing in supersets:
            del clauses[existing]

        # 절 수 한도를 넘으면 절을 버리지 않고 중단한다.
        if normalized not in clauses and len(clauses) >= max_clauses:
            raise EncodingLimitError(
                f"More than {max_clauses} CNF clauses; exact encoding stopped"
            )
        clauses.setdefault(normalized, set()).add(reason)

    # 같은 요청의 후보는 둘 다 고를 수 없다는 절을 추가한다.
    requests: dict[str, list[str]] = defaultdict(list)
    for candidate in candidates:
        requests[str(candidate["request_id"])].append(str(candidate["id"]))
    for request_id, ids in sorted(requests.items()):
        for left, right in combinations(sorted(ids), 2):
            add_clause([(left, 0), (right, 0)], f"request:{request_id}")

    def interval(coefficients: dict, fixed: dict) -> tuple[Fraction, Fraction]:
        """요청 배타성을 지키는 완성 배정들에 대한 행 값의 최소/최대.

        다른 재고/자원 행은 일부러 무시한다. 이 낙관적 경계는 큐브 전체가 이 행을
        위반한다는 것은 증명할 수 있지만, 다른 후보의 공급 덕분에 가능해지는 완성
        배정을 잘못 배제하지는 않는다.
        """
        # 계수를 요청별로 묶는다.
        grouped: dict[str, list[str]] = defaultdict(list)
        for key in coefficients:
            grouped[request_of[key]].append(key)
        minimum = maximum = Fraction(0)

        # 요청 그룹마다 하나까지만 고를 수 있다는 점을 반영해 경계를 더한다.
        for group in grouped.values():
            chosen = [key for key in group if fixed.get(key) == 1]
            if len(chosen) > 1:
                raise ValueError("Internal cube violates request exclusivity")
            if chosen:
                # 이미 1로 고정된 후보가 있으면 그 계수로 확정된다.
                minimum += coefficients[chosen[0]]
                maximum += coefficients[chosen[0]]
            else:
                # 아직 0으로 고정되지 않은 후보 중 하나를 고르거나 아무것도 고르지 않는다.
                available = [
                    coefficients[key] for key in group if fixed.get(key) != 0
                ]
                minimum += min([Fraction(0), *available])
                maximum += max([Fraction(0), *available])
        return minimum, maximum

    # 명시된 물리/자원 충돌은 임의로 만든 엣지가 아니라 제약이다.
    for conflict in case.get("resource_conflicts", []):
        left, right = str(conflict["source"]), str(conflict["target"])
        if left not in candidate_ids or right not in candidate_ids:
            raise ValueError("Resource conflict refers to an unknown candidate")
        if left == right:
            raise ValueError("Resource conflict must involve two different candidates")
        add_clause([(left, 0), (right, 0)], "resource_conflict")

    # 선형 제약 행별 반복
    for index, row in enumerate(rows):
        if time.perf_counter() > deadline:
            raise EncodingLimitError("Exact encoding wall-clock limit exceeded")
        reason = str(row.get("id", f"row:{index}"))

        # 계수를 정확한 분수로 읽고 0인 계수는 뺀다.
        coefficients = {}
        for candidate_id, coefficient in row.get("coefficients", {}).items():
            candidate_id = str(candidate_id)
            if candidate_id not in candidate_ids:
                raise ValueError(f"{reason}: unknown candidate {candidate_id}")
            value = _number(coefficient, reason)
            if value:
                coefficients[candidate_id] = value

        # 상하한을 읽고 행 값의 전체 범위를 구한다.
        lower = None if row.get("lower") is None else _number(row["lower"], reason)
        upper = None if row.get("upper") is None else _number(row["upper"], reason)
        minimum, maximum = interval(coefficients, {})

        # 어떤 배정으로도 만족할 수 없는 행이면 중단한다.
        if (lower is not None and lower > maximum) or (
            upper is not None and upper < minimum
        ) or (lower is not None and upper is not None and lower > upper):
            raise InfeasibleModelError(f"{reason}: impossible binary bounds")

        # 어떤 배정이든 항상 만족하는 행은 건너뛴다.
        if (lower is None or lower <= minimum) and (
            upper is None or upper >= maximum
        ):
            continue  # 다른 제약을 보기 전에도 전역적으로 중복인 행이다.

        # 지지 크기가 한도를 넘으면 행을 버리지 않고 중단한다.
        support = sorted(coefficients)
        if len(support) > max_support:
            raise EncodingLimitError(
                f"{reason}: support {len(support)} exceeds max_support={max_support}"
            )

        # 요청별로 '선택 안 함 또는 후보 하나'인 배정만 열거한다.
        local_requests: dict[str, list[str]] = defaultdict(list)
        for key in support:
            local_requests[request_of[key]].append(key)
        choices = [[None, *group] for _, group in sorted(local_requests.items())]

        for selected in product(*choices):
            if time.perf_counter() > deadline:
                raise EncodingLimitError("Exact encoding wall-clock limit exceeded")
            # 선택 조합을 지지 집합 위의 0/1 배정으로 펼친다.
            assignment = {key: 0 for key in support}
            for key in selected:
                if key is not None:
                    assignment[key] = 1

            # 이 배정이 행을 위반하는지 계산한다.
            total = sum(coefficients[key] * value for key, value in assignment.items())
            violation = (lower is not None and total < lower) or (
                upper is not None and total > upper
            )

            if violation:
                # 앞선 절이 이미 금지하는 배정이면 행 절을 새로 만들 필요가 없다.
                if any(
                    all(key in assignment and assignment[key] != value for key, value in clause)
                    for clause in clauses
                ):
                    continue
                cube = dict(assignment)

                # 0인 비트부터 풀어 준다. 같은 요청의 다른 선택이 이미 0을 강제할 수 있다.
                # 제거할 때마다 구간 경계로 타당성을 인증한다.
                for key in sorted(support, key=lambda key: (assignment[key], key)):
                    value = cube.pop(key)
                    cube_min, cube_max = interval(coefficients, cube)
                    entirely_invalid = (
                        lower is not None and cube_max < lower
                    ) or (upper is not None and cube_min > upper)
                    if not entirely_invalid:
                        cube[key] = value

                # 인증된 위반 큐브를 금지한다. 비트 중 하나는 반대 값이어야 한다.
                add_clause(
                    [(key, 1 - value) for key, value in cube.items()], reason
                )

    # 절을 정렬해 순서를 고정하고, 절 번호별 출처를 함께 반환한다.
    ordered = sorted(clauses)
    provenance = {
        str(index): sorted(clauses[clause]) for index, clause in enumerate(ordered)
    }
    return ordered, provenance


def encode_operational_mis(
    case: dict,
    *,
    max_support: int = 16,
    max_clauses: int = 5000,
    max_nodes: int = 12000,
    max_edges: int = 500000,
    max_seconds: float = 60.0,
) -> dict:
    """정확한 비가중 그래프를 반환하거나, 크기/불가능 오류를 명시적으로 낸다.

    후보 보상 정점은 추천을 나타낸다. 절/쌍둥이 정점은 보조 수학 객체이며
    절대 이용자로 세어서는 안 된다.
    """
    # 한도 값을 검증한다.
    if min(max_support, max_clauses, max_nodes, max_edges) < 1:
        raise ValueError("Encoding limits must all be positive")
    if not math.isfinite(max_seconds) or max_seconds <= 0:
        raise ValueError("Encoding wall-clock limit must be finite and positive")
    started = time.perf_counter()
    deadline = started + max_seconds

    # 절을 컴파일하고, 절 하나를 빠뜨린 손해가 보상 전체보다 크도록 가중치 W를 정한다.
    candidates, rows = _inputs(case)
    clauses, provenance = _compile_clauses(case, max_support, max_clauses, deadline)
    candidate_ids = sorted(str(candidate["id"]) for candidate in candidates)
    weight = len(candidate_ids) + 1

    # 사전 크기 계산은 정확하다. 절에는 반복되거나 상보적인 변수가 없으므로
    # 절 내부 엣지와 모순 엣지는 겹치지 않는다.
    positive = defaultdict(int, {candidate_id: 1 for candidate_id in candidate_ids})
    negative: dict[str, int] = defaultdict(int)
    node_count = len(candidate_ids)
    edge_count = 0
    for clause in clauses:
        node_count += len(clause) * weight
        edge_count += len(clause) * (len(clause) - 1) // 2 * weight**2
        for variable, value in clause:
            (positive if value else negative)[variable] += weight
    edge_count += sum(positive[key] * negative[key] for key in candidate_ids)

    # 한도를 넘으면 그래프를 만들기 전에 중단한다.
    if node_count > max_nodes or edge_count > max_edges:
        raise EncodingLimitError(
            f"Exact encoding needs {node_count} vertices and {edge_count} edges; "
            f"limits are {max_nodes} and {max_edges}"
        )

    nodes = []
    edges = []
    literal_nodes: dict[Literal, list[str]] = defaultdict(list)

    def add_edge(left: str, right: str, reason: str) -> None:
        """사유와 함께 엣지를 추가한다."""
        if len(edges) % 1024 == 0 and time.perf_counter() > deadline:
            raise EncodingLimitError("Exact encoding wall-clock limit exceeded")
        edges.append({"source": left, "target": right, "reasons": [reason]})

    # 후보마다 x_v=1에 대응하는 보상 정점을 만든다.
    for candidate_id in candidate_ids:
        node_id = f"reward:{candidate_id}"
        nodes.append({
            "id": node_id,
            "role": "candidate_reward",
            "candidate_id": candidate_id,
            "literal_value": 1,
        })
        literal_nodes[(candidate_id, 1)].append(node_id)

    # 절별 반복
    for clause_index, clause in enumerate(clauses):
        groups = []

        # 리터럴마다 W개의 쌍둥이 보조 정점을 만든다.
        for literal_index, (candidate_id, value) in enumerate(clause):
            group = []
            for copy_index in range(weight):
                node_id = f"clause:{clause_index}:literal:{literal_index}:copy:{copy_index}"
                nodes.append({
                    "id": node_id,
                    "role": "clause_auxiliary",
                    "candidate_id": candidate_id,
                    "literal_value": value,
                    "clause_index": clause_index,
                })
                group.append(node_id)
                literal_nodes[(candidate_id, value)].append(node_id)
            groups.append(group)

        # 한 절 안의 서로 다른 리터럴 정점끼리 연결한다.
        for left_group, right_group in combinations(groups, 2):
            for left in left_group:
                for right in right_group:
                    add_edge(left, right, "clause_literal_exclusivity")

    # 같은 변수의 참 정점과 거짓 정점을 연결한다.
    for candidate_id in candidate_ids:
        for positive_node in literal_nodes[(candidate_id, 1)]:
            for negative_node in literal_nodes[(candidate_id, 0)]:
                add_edge(positive_node, negative_node, "contradictory_assignment")

    # 실제 크기가 사전 계산과 일치하는지 확인한다.
    assert len(nodes) == node_count and len(edges) == edge_count

    # 그래프와 복호화에 필요한 정보를 함께 반환한다.
    return {
        "case_id": case.get("case_id"),
        "nodes": nodes,
        "edges": edges,
        "operational_constraints": rows,
        "source_case": case,
        "encoding_map": {
            "kind": "bounded_cnf_independent_twins",
            "clause_weight": weight,
            "objective_offset": weight * len(clauses),
            "candidate_ids": candidate_ids,
            "clauses": [[list(literal) for literal in clause] for clause in clauses],
            "clause_provenance": provenance,
        },
        "metadata": {
            "representation": "exact_operational_mis",
            "graph_is_unweighted": True,
            "candidate_count": len(candidate_ids),
            "auxiliary_node_count": node_count - len(candidate_ids),
            "node_count": node_count,
            "edge_count": edge_count,
            "clause_count": len(clauses),
            "encoding_limits": {
                "max_support": max_support,
                "max_clauses": max_clauses,
                "max_nodes": max_nodes,
                "max_edges": max_edges,
                "max_seconds": max_seconds,
            },
            "feasibility": "requires_clause_complete_decoding_or_exact_certificate",
            "scope": "exact for supplied request, resource and linear constraints",
            "construction_seconds": time.perf_counter() - started,
        },
    }


def _original_violations(case: dict, chosen: set[str]) -> list[str]:
    """컴파일된 CNF만이 아니라 원본 모델을 독립적으로 검사한다."""
    candidates, rows = _inputs(case)

    # 없는 후보의 선택을 찾는다.
    known = {str(candidate["id"]) for candidate in candidates}
    violations = [f"unknown:{key}" for key in sorted(chosen - known)]

    # 한 요청에서 후보를 둘 이상 고른 경우를 찾는다.
    requests: dict[str, int] = defaultdict(int)
    for candidate in candidates:
        if str(candidate["id"]) in chosen:
            requests[str(candidate["request_id"])] += 1
    violations.extend(f"request:{key}" for key, count in requests.items() if count > 1)

    # 자원 충돌을 찾는다.
    for index, pair in enumerate(case.get("resource_conflicts", [])):
        if str(pair["source"]) in chosen and str(pair["target"]) in chosen:
            violations.append(f"resource:{index}")

    # 선형 제약의 상하한 위반을 찾는다.
    for index, row in enumerate(rows):
        label = str(row.get("id", f"row:{index}"))
        activity = sum(
            _number(value, label) for key, value in row["coefficients"].items()
            if str(key) in chosen
        )
        if (row.get("lower") is not None and activity < _number(row["lower"], label)) or (
            row.get("upper") is not None and activity > _number(row["upper"], label)
        ):
            violations.append(label)
    return violations


def lift_operational_selection(graph: dict, selected_candidate_ids: list[str]) -> list[str]:
    """실행 가능한 운영 추천 집합으로부터 그래프 해를 구성한다.

    인코딩을 아는 고전 기준선이다. 원본 최적해가 증명되어 있으면 옮긴 해는
    offset+최적값에 도달하며, 확장 그래프를 처음부터 풀지 않고도 그래프 최적을 인증한다.
    """
    # 이 인코딩으로 만든 그래프인지 확인한다.
    encoding = graph.get("encoding_map", {})
    if encoding.get("kind") != "bounded_cnf_independent_twins":
        raise ValueError("Graph does not use this exact encoding")

    # 원본 모델에서 실행 가능한 선택인지 확인한다.
    chosen = {str(key) for key in selected_candidate_ids}
    violations = _original_violations(graph["source_case"], chosen)
    if violations:
        raise ValueError(f"Source selection is infeasible: {violations[:5]}")

    # 절마다 참이 되는 리터럴 하나를 증인으로 고른다.
    witnesses = {}
    for index, clause in enumerate(encoding["clauses"]):
        witness = next(
            ((key, value) for key, value in clause if (key in chosen) == bool(value)),
            None,
        )
        if witness is None:
            raise ValueError("Source selection does not satisfy the encoded CNF")
        witnesses[index] = witness

    # 선택된 후보의 보상 정점과 증인 리터럴의 쌍둥이 정점을 모은다.
    selected = []
    for node in graph["nodes"]:
        if node["role"] == "candidate_reward":
            keep = node["candidate_id"] in chosen
        else:
            keep = witnesses[node["clause_index"]] == (
                node["candidate_id"], node["literal_value"]
            )
        if keep:
            selected.append(node["id"])
    return selected


def decode_operational_mis(graph: dict, selected_node_ids: list[str]) -> dict:
    """표본이 최적이거나 독립집합이라고 가정하지 않고 복호화한다."""
    # 이 인코딩으로 만든 그래프인지 확인한다.
    encoding = graph.get("encoding_map", {})
    if encoding.get("kind") != "bounded_cnf_independent_twins":
        raise ValueError("Graph does not use this exact encoding")

    # 없는 노드와, 함께 선택된 엣지 양끝을 찾는다.
    selected = set(selected_node_ids)
    node_lookup = {node["id"]: node for node in graph["nodes"]}
    unknown = sorted(selected - node_lookup.keys())
    conflicting_edges = [
        [edge["source"], edge["target"]]
        for edge in graph["edges"]
        if edge["source"] in selected and edge["target"] in selected
    ]

    # 선택된 노드에서 변수 배정과 대표된 절을 모은다.
    assignments: dict[str, set[int]] = defaultdict(set)
    represented_clauses = set()
    for node_id in selected & node_lookup.keys():
        node = node_lookup[node_id]
        assignments[node["candidate_id"]].add(node["literal_value"])
        if node["role"] == "clause_auxiliary":
            represented_clauses.add(node["clause_index"])

    # 모순된 변수, 선택된 후보, 빠진 절, CNF 만족 여부를 계산한다.
    contradictions = sorted(key for key, values in assignments.items() if len(values) > 1)
    chosen = sorted(key for key, values in assignments.items() if values == {1})
    missing_clauses = sorted(set(range(len(encoding["clauses"]))) - represented_clauses)
    satisfied = all(
        any((variable in chosen) == bool(value) for variable, value in clause)
        for clause in encoding["clauses"]
    )

    # CNF는 원본 모델과 정확히 같다. 증인이 빠져도 실행 가능한 배정일 수 있지만,
    # 완전한 그래프 인증서로 보지는 않는다.
    independent = not unknown and not conflicting_edges and not contradictions
    source_violations = _original_violations(graph["source_case"], set(chosen))

    return {
        "independent": independent,
        "clause_complete": not missing_clauses,
        "operational_feasible": independent and satisfied and not source_violations,
        "source_constraint_violations": source_violations,
        "selected_candidate_ids": chosen,
        "selected_candidate_count": len(chosen),
        "selected_graph_node_count": len(selected),
        "reward_vertices_selected": sum(
            node_lookup[key]["role"] == "candidate_reward"
            for key in selected & node_lookup.keys()
        ),
        "missing_clauses": missing_clauses,
        "unknown_nodes": unknown,
        "conflicting_edges": conflicting_edges,
        "contradictory_variables": contradictions,
        "note": "Use decoded candidate count; subtracting the offset is valid only at an optimum.",
    }
