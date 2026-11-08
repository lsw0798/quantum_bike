"""후보 충돌 그래프, 고전 기준선, 원래 운영 제약 검사.

그래프는 항상 같은 요청·확정 자원의 충돌을 포함한다. 수량 제약에는
다른 선택이 공급을 보충할 수 있는지까지 낙관적으로 계산한 뒤 간선을
추가한다. 따라서 간선은 타당하지만 모든 고차 제약을 표현하지는 않는다.
그 경우 metadata.representation은 pairwise_relaxation이다.
"""
from __future__ import annotations

import csv
import json
import math
import platform
import random
import time
from collections import defaultdict
from itertools import combinations
from pathlib import Path

import numpy as np
import scipy
from scipy.optimize import Bounds, LinearConstraint, milp
from scipy.sparse import coo_matrix

# 부동소수 비교에 쓰는 허용 오차
TOLERANCE = 1e-7


def _candidates(case: dict) -> dict[str, dict]:
    """후보 목록을 검증하고 문자열 ID를 키로 하는 사전으로 바꾼다."""
    result = {}
    for candidate in case.get("candidates", []):
        identifier = str(candidate["id"])

        # ID 중복과 요청 ID 누락을 거절한다.
        if identifier in result:
            raise ValueError(f"Duplicate candidate ID: {identifier}")
        if "request_id" not in candidate:
            raise ValueError(f"Missing request_id: {identifier}")

        # ID와 요청 ID를 문자열로 통일한다.
        result[identifier] = {**candidate, "id": identifier,
                              "request_id": str(candidate["request_id"])}
    return result


def _constraints(case: dict, identifiers: set[str]) -> list[dict]:
    """수량 제약은 상수항을 이미 lower/upper에 옮긴 형태로 받는다."""
    rows = case.get("constraints", case.get("operational_constraints", []))
    result = []
    seen = set()

    # 제약 행별로 검증하고 정규화한다.
    for position, row in enumerate(rows):
        # 제약 ID 중복을 거절한다.
        identifier = str(row.get("id", f"constraint_{position}"))
        if identifier in seen:
            raise ValueError(f"Duplicate constraint ID: {identifier}")
        seen.add(identifier)

        # 0이 아닌 계수만 남긴다.
        coefficients = {str(key): float(value)
                        for key, value in row["coefficients"].items()
                        if float(value) != 0}

        # 없는 후보를 가리키거나 유한하지 않은 계수를 거절한다.
        if not set(coefficients) <= identifiers:
            raise ValueError(f"Unknown candidate in constraint {identifier}")
        if not all(math.isfinite(value) for value in coefficients.values()):
            raise ValueError(f"Non-finite coefficient in {identifier}")

        # 상하한을 실수로 바꾼다. None은 한도 없음을 뜻한다.
        lower = row.get("lower")
        upper = row.get("upper")
        lower = None if lower is None else float(lower)
        upper = None if upper is None else float(upper)

        # 무한대 상하한과 뒤집힌 상하한을 거절한다.
        if any(value is not None and not math.isfinite(value)
               for value in (lower, upper)):
            raise ValueError(f"Use null for unbounded constraint {identifier}")
        if lower is not None and upper is not None and lower > upper:
            raise ValueError(f"Reversed bounds in {identifier}")

        result.append({**row, "id": identifier, "coefficients": coefficients,
                       "lower": lower, "upper": upper})
    return result


def _resource_pairs(case: dict, identifiers: set[str]) -> list[dict]:
    """실제 자료/운영 규칙에서 확정한 충돌만 외부에서 입력한다."""
    result = []
    for pair in case.get("resource_conflicts", []):
        source, target = str(pair["source"]), str(pair["target"])

        # 없는 후보와 자기 자신과의 충돌을 거절한다.
        if source not in identifiers or target not in identifiers:
            raise ValueError("Unknown candidate in resource_conflicts")
        if source == target:
            raise ValueError("Self-conflict must be an explicit linear constraint")

        result.append({"source": source, "target": target,
                       "reason": str(pair.get("reason", "confirmed_exclusive_resource"))})
    return result


def _request_groups(candidates: dict[str, dict]) -> dict[str, list[str]]:
    """요청 ID별로 후보 ID를 묶는다."""
    groups = defaultdict(list)
    for identifier, candidate in candidates.items():
        groups[candidate["request_id"]].append(identifier)
    return dict(groups)


def _violating_pairs(values: dict[str, float], threshold: float):
    """양수 크기 두 개의 합이 threshold를 초과하는 쌍을 열거한다."""
    # 큰 값부터 정렬하고, 합이 기준 이하가 되면 뒤쪽은 볼 필요가 없다.
    ordered = sorted(values.items(), key=lambda item: (-item[1], item[0]))
    for index, (source, amount) in enumerate(ordered):
        for other_index in range(index + 1, len(ordered)):
            target, other = ordered[other_index]
            if amount + other <= threshold + TOLERANCE:
                break
            yield source, target


def _row_is_pairwise(row: dict, remaining: set[str]) -> bool:
    """충분조건만 사용한다. False는 반드시 비동치라는 증명은 아니다."""
    # 남은 후보의 계수로 이 행이 가질 수 있는 최소·최대값을 구한다.
    values = [value for key, value in row["coefficients"].items() if key in remaining]
    minimum = sum(min(value, 0) for value in values)
    maximum = sum(max(value, 0) for value in values)
    lower, upper = row["lower"], row["upper"]

    # 어떤 선택에서도 상하한을 넘지 않으면 제약이 사실상 없다.
    lower_redundant = lower is None or minimum >= lower - TOLERANCE
    upper_redundant = upper is None or maximum <= upper + TOLERANCE
    if lower_redundant and upper_redundant:
        return True

    # 모든 단독 선택은 가능하고 어떤 두 선택도 불가능한 단조 제약.
    if lower_redundant and all(value > 0 for value in values):
        amounts = sorted(values)
        return bool(len(amounts) >= 2
                    and amounts[-1] <= upper + TOLERANCE
                    and amounts[0] + amounts[1] > upper + TOLERANCE)
    if upper_redundant and all(value < 0 for value in values):
        amounts = sorted(-value for value in values)
        return bool(len(amounts) >= 2
                    and amounts[-1] <= -lower + TOLERANCE
                    and amounts[0] + amounts[1] > -lower + TOLERANCE)
    return False


def _unique_rows(rows: list[dict]) -> list[dict]:
    """동일한 계수·상하한의 행은 계산할 때 한 번만 사용한다."""
    seen = set()
    unique = []
    for row in rows:
        key = (tuple(sorted(row["coefficients"].items())), row["lower"], row["upper"])
        if key not in seen:
            unique.append(row)
            seen.add(key)
    return unique


def build_graph(case: dict, *, max_pair_deductions: int = 500_000,
                max_edges: int = 1_000_000, inference_seconds: float = 15.0) -> dict:
    """실제 후보 ID를 보존하는 무방향 비가중 그래프를 만든다.

    c*x <= U에서 u,v를 고른 뒤 남은 음수 계수를 전부 고르는 것이
    가능한 최소값의 하계다. 그 하계마저 U를 넘을 때만 충돌 엣지다.
    다른 후보의 반납/공급이 쌍을 구제하는 경우를 잘못 금지하지 않는다.
    """
    # 계산 예산 값을 검증한다.
    if max_pair_deductions < 0 or max_edges < 0 or inference_seconds < 0:
        raise ValueError("Graph budgets cannot be negative")
    if not math.isfinite(inference_seconds):
        raise ValueError("inference_seconds must be finite")

    # 입력을 정규화한다.
    started = time.perf_counter()
    candidates = _candidates(case)
    rows = _constraints(case, set(candidates))
    unique_rows = _unique_rows(rows)
    resource_pairs = _resource_pairs(case, set(candidates))
    excluded = defaultdict(list)

    # 단독으로 골라도 제약을 위반하는 후보를 찾아 사유와 함께 제외한다.
    for row in unique_rows:
        coefficients = row["coefficients"]
        minimum = sum(min(value, 0) for value in coefficients.values())
        maximum = sum(max(value, 0) for value in coefficients.values())
        for identifier, value in coefficients.items():
            if row["upper"] is not None and minimum + max(value, 0) > row["upper"] + TOLERANCE:
                excluded[identifier].append(f"{row['id']}:singleton_upper")
            if row["lower"] is not None and maximum + min(value, 0) < row["lower"] - TOLERANCE:
                excluded[identifier].append(f"{row['id']}:singleton_lower")

    remaining = set(candidates) - set(excluded)
    edges = defaultdict(set)

    def add_edge(source: str, target: str, reason: str):
        """남은 후보 사이에 충돌 엣지와 사유를 추가한다."""
        if source in remaining and target in remaining:
            # 예산을 넘는 새 엣지는 필수 충돌이므로 임의로 버리지 않고 중단한다.
            if len(edges) >= max_edges and tuple(sorted((source, target))) not in edges:
                raise ValueError("Graph edge budget exceeded by mandatory conflicts")
            edges[tuple(sorted((source, target)))].add(reason)

    # 같은 요청의 후보끼리는 항상 충돌한다.
    for request_id, group in _request_groups(candidates).items():
        for source, target in combinations(sorted(set(group) & remaining), 2):
            add_edge(source, target, f"same_request:{request_id}")

    # 확정된 자원 충돌을 추가한다.
    for pair in resource_pairs:
        add_edge(pair["source"], pair["target"], pair["reason"])

    unresolved = []
    inference_started = time.perf_counter()
    deductions = 0
    inference_truncated = False

    # 수량 제약에서 어떤 경우에도 함께 고를 수 없는 쌍을 추론한다.
    for row in unique_rows:
        # 추론 예산이 다 되면 중단하고 표시를 남긴다.
        if (deductions >= max_pair_deductions or len(edges) >= max_edges
                or time.perf_counter() - inference_started >= inference_seconds):
            inference_truncated = True
            break

        # 남은 후보의 계수를 양수와 음수(크기)로 나눈다.
        coefficients = {key: value for key, value in row["coefficients"].items()
                        if key in remaining}
        positive = {key: value for key, value in coefficients.items() if value > 0}
        negative = {key: -value for key, value in coefficients.items() if value < 0}

        # 상한: 음수 계수를 전부 골라도 두 양수의 합이 상한을 넘는 쌍을 찾는다.
        if row["upper"] is not None:
            threshold = row["upper"] + sum(negative.values())
            for source, target in _violating_pairs(positive, threshold):
                # 시간 확인은 1024회마다 한다.
                if (deductions >= max_pair_deductions or len(edges) >= max_edges
                        or (deductions % 1024 == 0
                            and time.perf_counter() - inference_started >= inference_seconds)):
                    inference_truncated = True
                    break
                add_edge(source, target, f"{row['id']}:upper")
                deductions += 1

        # 하한: 양수 계수를 전부 골라도 두 음수의 합이 하한 아래로 내리는 쌍을 찾는다.
        if row["lower"] is not None:
            threshold = sum(positive.values()) - row["lower"]
            for source, target in _violating_pairs(negative, threshold):
                # 시간 확인은 1024회마다 한다.
                if (deductions >= max_pair_deductions or len(edges) >= max_edges
                        or (deductions % 1024 == 0
                            and time.perf_counter() - inference_started >= inference_seconds)):
                    inference_truncated = True
                    break
                add_edge(source, target, f"{row['id']}:lower")
                deductions += 1

    # 쌍 충돌만으로 표현됐다고 확인하지 못한 제약을 모은다.
    unresolved = [row["id"] for row in unique_rows if not _row_is_pairwise(row, remaining)]

    # 그래프와 정확성 표시를 함께 반환한다.
    return {
        "schema_version": 1,
        "case_id": case.get("case_id"),
        "nodes": [candidates[key] for key in candidates if key in remaining],
        "edges": [{"source": source, "target": target, "reasons": sorted(reasons)}
                  for (source, target), reasons in sorted(edges.items())],
        "operational_constraints": rows,
        "metadata": {
            "directed": False,
            "weighted": False,
            "original_candidate_count": len(candidates),
            "node_count": len(remaining),
            "edge_count": len(edges),
            "excluded_candidates": [
                {"id": key, "reasons": reasons} for key, reasons in sorted(excluded.items())
            ],
            "representation": (
                "pairwise_relaxation" if unresolved or inference_truncated
                else "exact_declared_constraints"
            ),
            "is_exact_for_declared_model": not unresolved and not inference_truncated,
            "unresolved_constraint_ids": unresolved,
            "constraint_count": len(rows),
            "unique_constraint_count": len(unique_rows),
            "pair_inference_truncated": inference_truncated,
            "pair_deductions_attempted": deductions,
            "inference_budget": {
                "max_pair_deductions": max_pair_deductions,
                "max_edges": max_edges,
                "seconds": inference_seconds,
            },
            "interpretation": (
                "Exactness concerns declared constraints, not unobserved real operations."
            ),
            "construction_seconds": time.perf_counter() - started,
            "case_metadata": case.get("metadata", {}),
        },
    }


def _selection_violations(case: dict, selected_ids: list[str]) -> list[dict]:
    """선택한 후보가 어기는 규칙을 모두 찾아 목록으로 반환한다."""
    candidates = _candidates(case)
    rows = _constraints(case, set(candidates))
    selected = set(selected_ids)
    violations = []

    # 중복 선택을 검사한다.
    if len(selected_ids) != len(selected):
        violations.append({"kind": "duplicate_selection"})

    # 없는 후보의 선택을 검사한다.
    unknown = sorted(selected - set(candidates))
    if unknown:
        violations.append({"kind": "unknown_candidate", "ids": unknown})

    # 한 요청에서 후보를 둘 이상 골랐는지 검사한다.
    for request_id, group in _request_groups(candidates).items():
        chosen = sorted(selected.intersection(group))
        if len(chosen) > 1:
            violations.append({"kind": "same_request", "request_id": request_id, "ids": chosen})

    # 자원 충돌을 검사한다.
    for pair in _resource_pairs(case, set(candidates)):
        if pair["source"] in selected and pair["target"] in selected:
            violations.append({"kind": "exclusive_resource", **pair})

    # 선형 제약의 상하한을 검사한다.
    for row in rows:
        value = sum(coefficient for key, coefficient in row["coefficients"].items()
                    if key in selected)
        if ((row["lower"] is not None and value < row["lower"] - TOLERANCE)
                or (row["upper"] is not None and value > row["upper"] + TOLERANCE)):
            violations.append({"kind": row.get("kind", "linear"), "id": row["id"],
                               "value": value, "lower": row["lower"], "upper": row["upper"]})
    return violations


def validate_selection(case: dict, selected_ids) -> dict:
    """원래 전체 제약으로 검사한다. 미관측 이용자 수락까지 검증하지 않는다."""
    chosen = [str(identifier) for identifier in selected_ids]
    violations = _selection_violations(case, chosen)
    return {
        "feasible": not violations,
        "selected_count": len(set(chosen)),
        "violations": violations,
        "scope": "declared_requests_resources_and_inventory_constraints",
    }


def _solve_binary_model(identifiers: list[str], rows: list[dict], time_limit: float) -> dict:
    """선택 수 Σx를 최대화하는 이진 선형계획을 HiGHS로 풀고 결과를 검산한다."""
    # 제한시간을 검증한다.
    if time_limit <= 0 or not math.isfinite(time_limit):
        raise ValueError("time_limit must be finite and positive")
    started = time.perf_counter()

    # 변수가 없으면 풀이기 없이 제약만 확인한다.
    if not identifiers:
        feasible = all((row["lower"] is None or row["lower"] <= TOLERANCE)
                       and (row["upper"] is None or row["upper"] >= -TOLERANCE)
                       for row in rows)
        return {
            "status": "optimal" if feasible else "infeasible",
            "optimal": feasible,
            "objective": 0 if feasible else None,
            "selected_ids": [],
            "upper_bound": 0 if feasible else None,
            "gap": 0.0 if feasible else None,
            "solver_seconds": 0.0,
            "mip_node_count": 0,
            "time_limit_seconds": time_limit,
        }

    # 제약 행을 희소 행렬과 상하한 배열로 바꾼다.
    index = {identifier: position for position, identifier in enumerate(identifiers)}
    row_indices, columns, values = [], [], []
    lower, upper = [], []
    rows = _unique_rows(rows)
    for row_number, row in enumerate(rows):
        for identifier, coefficient in row["coefficients"].items():
            row_indices.append(row_number)
            columns.append(index[identifier])
            values.append(coefficient)
        lower.append(-np.inf if row["lower"] is None else row["lower"])
        upper.append(np.inf if row["upper"] is None else row["upper"])
    matrix = coo_matrix((values, (row_indices, columns)),
                        shape=(len(rows), len(identifiers))).tocsc()
    constraints = LinearConstraint(matrix, np.array(lower), np.array(upper)) if rows else None

    # milp는 최소화 문제이므로 목적계수를 -1로 둔다.
    result = milp(
        c=-np.ones(len(identifiers)),
        integrality=np.ones(len(identifiers)),
        bounds=Bounds(np.zeros(len(identifiers)), np.ones(len(identifiers))),
        constraints=constraints,
        options={"time_limit": float(time_limit), "mip_rel_gap": 0.0, "disp": False},
    )
    elapsed = time.perf_counter() - started

    # 풀이기 상태 코드를 이름으로 바꾼다.
    statuses = {
        0: "optimal",
        1: "limit_reached",
        2: "infeasible",
        3: "unbounded",
        4: "solver_error",
    }

    # 해를 0/1로 반올림한다.
    selected = [] if result.x is None else [
        identifier for identifier, value in zip(identifiers, result.x) if value >= 0.5
    ]
    selected_set = set(selected)
    rounded = np.array([float(identifier in selected_set) for identifier in identifiers])

    # 반올림한 해가 제약을 실제로 만족하는지 다시 계산한다.
    activity = matrix @ rounded
    feasible = result.x is not None and bool(
        np.all(activity >= np.array(lower) - TOLERANCE)
        and np.all(activity <= np.array(upper) + TOLERANCE)
    )

    # 쌍대 경계로 정수 상한을 구한다. 값이 없거나 유한하지 않으면 None이다.
    dual_bound = getattr(result, "mip_dual_bound", None)
    bound = None
    if dual_bound is not None and math.isfinite(float(dual_bound)):
        bound = min(len(identifiers), int(math.floor(-float(dual_bound) + TOLERANCE)))

    # 최적성 격차를 구한다. 값이 없거나 유한하지 않으면 None이다.
    gap = getattr(result, "mip_gap", None)
    gap = float(gap) if gap is not None and math.isfinite(float(gap)) else None

    return {
        "status": statuses.get(result.status, "unknown"),
        "optimal": result.status == 0 and feasible,
        "objective": len(selected) if feasible else None,
        "selected_ids": selected if feasible else [],
        "upper_bound": bound,
        "gap": gap,
        "solver_seconds": elapsed,
        "time_limit_seconds": float(time_limit),
        "mip_node_count": int(getattr(result, "mip_node_count", 0) or 0),
        "solver": "scipy.optimize.milp / HiGHS",
        "scipy_version": scipy.__version__,
        "platform": platform.platform(),
        "message": str(result.message),
    }


def solve_operational(case: dict, time_limit: float = 10.0) -> dict:
    """같은 후보·목적 Σx에서 그래프 밖의 고차 제약까지 함께 푼다."""
    candidates = _candidates(case)
    rows = _constraints(case, set(candidates))

    # 요청마다 후보를 최대 하나만 고르는 제약을 추가한다.
    for request_id, group in _request_groups(candidates).items():
        rows.append({"id": f"request:{request_id}", "coefficients": dict.fromkeys(group, 1.0),
                     "lower": None, "upper": 1.0})

    # 자원 충돌도 선형 제약으로 추가한다.
    for position, pair in enumerate(_resource_pairs(case, set(candidates))):
        rows.append({"id": f"resource:{position}",
                     "coefficients": {pair["source"]: 1.0, pair["target"]: 1.0},
                     "lower": None, "upper": 1.0})

    result = _solve_binary_model(list(candidates), rows, time_limit)

    # 해가 있으면 풀이기와 별개의 검증 함수로 다시 확인한다.
    if result["objective"] is not None:
        result["validation"] = validate_selection(case, result["selected_ids"])
        if not result["validation"]["feasible"]:
            raise RuntimeError("MILP incumbent failed independent operational validation")

    result["problem"] = "full_declared_operational_model"
    return result


def _adjacency(graph: dict) -> dict[str, set[str]]:
    """그래프 문서를 인접 집합으로 바꾼다. 잘못된 엣지는 거절한다."""
    adjacency = {str(node["id"]): set() for node in graph["nodes"]}
    for edge in graph["edges"]:
        source, target = str(edge["source"]), str(edge["target"])
        if source == target or source not in adjacency or target not in adjacency:
            raise ValueError("Malformed graph edge")
        adjacency[source].add(target)
        adjacency[target].add(source)
    return adjacency


def _greedy_independent_set(adjacency: dict, seed: int) -> list[str]:
    """주어진 시드의 정점 순서로 탐욕적 독립집합을 만든다."""
    rng = random.Random(seed)
    vertices = list(adjacency)
    rng.shuffle(vertices)

    # 시드 0은 차수가 낮은 정점부터 고르는 결정론적 기준선이고, 다른 시드는 여러 순서를 탐색한다.
    if seed == 0:
        vertices.sort(key=lambda identifier: len(adjacency[identifier]))

    # 이웃이 아직 선택되지 않은 정점을 차례로 고른다.
    forbidden = set()
    selected = []
    for identifier in vertices:
        if identifier not in forbidden:
            selected.append(identifier)
            forbidden.update(adjacency[identifier])
    return selected


def _structure(adjacency: dict) -> dict:
    """연결 요소·밀도와, 다항 시간에 풀리는 그래프 형태인지를 조사한다."""
    unseen = set(adjacency)
    sizes = []
    all_cliques = True
    bipartite = True
    colors = {}

    # 연결 요소별로 탐색하며 이분 그래프 색칠을 시도한다.
    while unseen:
        first = min(unseen)
        stack, component = [first], []
        unseen.remove(first)
        colors[first] = 0

        while stack:
            current = stack.pop()
            component.append(current)
            for neighbor in adjacency[current]:
                # 이웃끼리 같은 색이면 이분 그래프가 아니다.
                if neighbor in colors and colors[neighbor] == colors[current]:
                    bipartite = False

                # 처음 만난 이웃은 반대 색을 칠해 스택에 넣는다.
                if neighbor in unseen:
                    unseen.remove(neighbor)
                    colors[neighbor] = 1 - colors[current]
                    stack.append(neighbor)

        # 요소 크기를 기록하고, 요소가 완전 그래프인지 확인한다.
        sizes.append(len(component))
        all_cliques &= all(len(adjacency[key]) == len(component) - 1 for key in component)

    count = len(adjacency)
    edge_count = sum(map(len, adjacency.values())) // 2
    return {
        "node_count": count,
        "edge_count": edge_count,
        "density": 2 * edge_count / (count * (count - 1)) if count > 1 else 0.0,
        "component_count": len(sizes),
        "largest_component": max(sizes, default=0),
        "is_disjoint_union_of_cliques": all_cliques,
        "is_bipartite": bipartite,
        "polynomial_family_detected": "cluster_graph" if all_cliques else
                                     ("bipartite" if bipartite else None),
    }


def benchmark_graph(graph: dict, time_limit: float = 10.0, greedy_repeats: int = 16) -> dict:
    """난도는 이 구현·제한시간에 대한 측정값이다. 일반 NP-hard성 주장이 아니다."""
    # 반복 횟수를 검증한다.
    if greedy_repeats < 1:
        raise ValueError("greedy_repeats must be at least 1")
    adjacency = _adjacency(graph)

    # 시드별 탐욕법을 실행한다.
    started = time.perf_counter()
    greedy = [_greedy_independent_set(adjacency, seed) for seed in range(greedy_repeats)]
    greedy_seconds = time.perf_counter() - started
    best_greedy = max(greedy, key=len)

    # 각 엣지를 x_u + x_v <= 1 제약으로 바꿔 MILP로 푼다.
    rows = [{"coefficients": {str(edge["source"]): 1.0, str(edge["target"]): 1.0},
             "lower": None, "upper": 1.0} for edge in graph["edges"]]
    result = _solve_binary_model(list(adjacency), rows, time_limit)
    incumbent = result["selected_ids"]

    # MILP 해가 없거나 탐욕법 해가 더 크면 탐욕법 해를 채택한다.
    if result["objective"] is None or len(best_greedy) > len(incumbent):
        result["selected_ids"] = best_greedy
        result["objective"] = len(best_greedy)
        result["incumbent_source"] = "greedy"
    else:
        result["incumbent_source"] = "milp"

    # 채택한 해가 독립집합인지 확인한다.
    chosen = set(result["selected_ids"])
    if any(chosen.intersection(adjacency[identifier]) for identifier in chosen):
        raise RuntimeError("Invalid independent set returned by benchmark")

    # 상한은 원래 후보 그래프에 대한 값이며, 하드웨어용 보조 노드에 대한 값이 아니다.
    result["upper_bound"] = (
        result["upper_bound"] if result["upper_bound"] is not None else len(adjacency)
    )
    result["absolute_gap"] = max(0, result["upper_bound"] - result["objective"])
    result["gap"] = result["absolute_gap"] / max(1, result["objective"])

    # 탐욕법 요약을 기록한다.
    result["greedy"] = {
        "best": len(best_greedy),
        "mean": float(np.mean(list(map(len, greedy)))),
        "repeats": greedy_repeats,
        "seconds": greedy_seconds,
        "selected_ids": best_greedy,
        "seeds": list(range(greedy_repeats)),
    }

    # 구조 정보와 난도 판정 범위를 기록한다.
    result["structure"] = _structure(adjacency)
    result["problem"] = "candidate_graph_unweighted_mis"
    result["hardness_status"] = (
        "optimality_proven" if result["optimal"]
        else "not_proven_within_budget" if result["status"] == "limit_reached"
        else "solver_failure"
    )
    result["hardness_scope"] = (
        "This HiGHS run and greedy baselines only; not every classical algorithm."
    )
    return result


def save_graph(graph: dict, directory: str | Path) -> None:
    """JSON은 전체 속성을, CSV는 검토하기 쉬운 노드/엣지 목록을 보존한다."""
    destination = Path(directory)
    destination.mkdir(parents=True, exist_ok=True)

    # 전체 그래프를 JSON으로 저장한다.
    (destination / "graph.json").write_text(
        json.dumps(graph, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8"
    )

    # 노드 목록을 CSV로 저장한다. 지정한 열 외의 속성은 생략한다.
    node_fields = ["id", "request_id", "origin", "destination", "start", "end"]
    with (destination / "nodes.csv").open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=node_fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(graph["nodes"])

    # 엣지 목록을 CSV로 저장한다. 사유 목록은 세미콜론으로 잇는다.
    with (destination / "edges.csv").open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=["source", "target", "reasons"])
        writer.writeheader()
        writer.writerows({**edge, "reasons": ";".join(edge["reasons"])} for edge in graph["edges"])
