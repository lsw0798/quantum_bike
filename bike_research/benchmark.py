"""RD-1.0의 정확 그래프와 로컬 고전 풀이 비교.

QPU 모듈을 가져오지 않는다. 보조 정점 개수와 실제 추천 개수는 별도 필드다.
`build_rd1_graphs`로 전체 사례를 등록하고, `benchmark_rd1_case`로 고정 예산
반복을 실행할 수 있다. 실패한 생성/풀이도 JSON 결과로 남긴다.
"""
from __future__ import annotations

from collections import defaultdict
import hashlib
import json
import math
import os
from pathlib import Path
import platform
import statistics
import subprocess
import time

import numpy as np

from .encoding import (EncodingLimitError, InfeasibleModelError,
                       decode_operational_mis, encode_operational_mis,
                       lift_operational_selection)
from .graph import (_adjacency, _candidates, _constraints, _request_groups,
                    _resource_pairs, build_graph, validate_selection)
from .hardness import audit_graph, verify_certificate


DEFAULTS = {
    "time_limit_seconds": 60.0,
    "seeds": [20261109, 20261110, 20261111, 20261112, 20261113],
    "structure_scan_seconds": 5.0,
    "encoding_max_support": 16,
    "encoding_max_clauses": 5000,
    "encoding_max_nodes": 12000,
    "encoding_max_edges": 500000,
    "encoding_max_seconds": 60.0,
    "graph_max_edges": 500000,
    "graph_seconds": 60.0,
    "redumis_executable": None,
}


def _config(config):
    return {**DEFAULTS, **(config or {})}


def _save(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    raw = json.dumps(value, ensure_ascii=False, allow_nan=False, indent=2)
    path.write_text(raw, encoding="utf-8")
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def operational_rows(case):
    """재고·요청·확인된 자원 제약을 같은 이진 선형 모형으로 구성한다."""
    candidates = _candidates(case)
    rows = _constraints(case, set(candidates))
    for request, group in _request_groups(candidates).items():
        rows.append({"id": f"request:{request}", "coefficients": dict.fromkeys(group, 1),
                     "lower": None, "upper": 1})
    for index, pair in enumerate(_resource_pairs(case, set(candidates))):
        rows.append({"id": f"resource:{index}",
                     "coefficients": {pair["source"]: 1, pair["target"]: 1},
                     "lower": None, "upper": 1})
    return list(candidates), rows


def _row_feasible(rows, chosen):
    selected = set(chosen)
    for row in rows:
        activity = sum(value for key, value in row["coefficients"].items() if key in selected)
        if row.get("lower") is not None and activity < row["lower"] - 1e-7:
            return False
        if row.get("upper") is not None and activity > row["upper"] + 1e-7:
            return False
    return True


def solve_highs(ids, rows, time_limit=60.0, seed=20261109, weights=None):
    """HiGHS 단일 스레드로 선택 수 또는 정확 축약의 정수 목적값을 최대화한다.

    행렬 생성 시간은 별도 기록하며 HiGHS 전처리와 탐색은 60초 예산 안이다.
    weights는 false-twin 축약의 묶음 크기에만 사용한다. 원모형은 모두 1이다.
    """
    import highspy
    if time_limit <= 0 or not math.isfinite(time_limit):
        raise ValueError("time_limit must be finite and positive")
    weights = dict.fromkeys(ids, 1) if weights is None else weights
    if set(weights) != set(ids) or any(isinstance(w, bool) or not isinstance(w, int) or w < 1
                                      for w in weights.values()):
        raise ValueError("Objective weights must be positive integers for every variable")
    total_weight = sum(weights.values())
    begin = time.perf_counter()
    if not ids:
        valid = _row_feasible(rows, [])
        return {"status": "optimal" if valid else "infeasible", "optimal": valid,
                "objective": 0 if valid else None, "selected_ids": [],
                "upper_bound": 0 if valid else None, "model_seconds": 0.0,
                "solver_seconds": 0.0, "proof_seconds": 0.0 if valid else None,
                "seed": seed, "threads": 1, "time_limit_seconds": time_limit,
                "budget_elapsed_seconds": 0.0, "budget_overrun": False}
    index = {key: i for i, key in enumerate(ids)}
    h = highspy.Highs()
    for key, value in {"output_flag": False, "threads": 1, "random_seed": int(seed),
                       "time_limit": float(time_limit), "mip_rel_gap": 0.0,
                       "mip_abs_gap": 0.0}.items():
        if h.setOptionValue(key, value) != highspy.HighsStatus.kOk:
            raise RuntimeError(f"HiGHS rejected option {key}")
    count = len(ids)
    h.addCols(count, -np.asarray([weights[key] for key in ids], dtype=float), np.zeros(count), np.ones(count), 0,
              np.zeros(count + 1, dtype=np.int32), np.array([], dtype=np.int32), np.array([]))
    h.changeColsIntegrality(count, np.arange(count, dtype=np.int32),
                            np.ones(count, dtype=np.uint8))
    starts, columns, coefficients, lower, upper = [0], [], [], [], []
    for row in rows:
        for key, value in row["coefficients"].items():
            columns.append(index[key])
            coefficients.append(value)
        starts.append(len(columns))
        lower.append(-highspy.kHighsInf if row.get("lower") is None else row["lower"])
        upper.append(highspy.kHighsInf if row.get("upper") is None else row["upper"])
    if rows:
        h.addRows(len(rows), np.asarray(lower, dtype=float), np.asarray(upper, dtype=float),
                  len(columns), np.asarray(starts, dtype=np.int32),
                  np.asarray(columns, dtype=np.int32), np.asarray(coefficients, dtype=float))
    model_seconds = time.perf_counter() - begin
    improvements = []
    def record_improvement(event):
        # 콜백 시각은 풀이 시작 기준이다. 증명 완료 시각과 구별한다.
        values = event.data_out.mip_solution
        selected = [key for key, value in zip(ids, values) if value >= 0.5]
        if _row_feasible(rows, selected):
            improvements.append({"seconds": float(event.data_out.running_time),
                                 "objective": sum(weights[key] for key in selected)})
    h.cbMipImprovingSolution.subscribe(record_improvement)
    solve_start = time.perf_counter()
    h.run()
    elapsed = time.perf_counter() - solve_start
    status = h.getModelStatus()
    solution, info = h.getSolution(), h.getInfo()
    chosen = [key for key, value in zip(ids, solution.col_value) if value >= 0.5]
    feasible = bool(solution.value_valid and _row_feasible(rows, chosen))
    optimal = status == highspy.HighsModelStatus.kOptimal and feasible
    names = {highspy.HighsModelStatus.kOptimal: "optimal",
             highspy.HighsModelStatus.kTimeLimit: "limit_reached",
             highspy.HighsModelStatus.kInfeasible: "infeasible"}
    dual = float(info.mip_dual_bound)
    # 전처리 이전에 끝나 primal조차 없을 때의 초기 0을 유효 상계로 쓰지 않는다.
    objective = sum(weights[key] for key in chosen)
    bound = min(total_weight, math.floor(-dual + 1e-7)) if feasible and math.isfinite(dual) else total_weight
    if optimal:
        bound = objective
    return {"status": names.get(status, "solver_error"), "optimal": optimal,
            "objective": objective if feasible else None,
            "selected_ids": sorted(chosen) if feasible else [],
            "upper_bound": bound if status != highspy.HighsModelStatus.kInfeasible else None,
            "model_seconds": model_seconds, "solver_seconds": elapsed,
            "proof_seconds": elapsed if optimal else None,
            "first_incumbent_seconds": improvements[0]["seconds"] if improvements else None,
            "incumbent_trace": improvements,
            "incumbent_time_reason": "callback_trace" if improvements else "no_callback_emitted",
            "mip_node_count": int(info.mip_node_count), "solver_version": h.version(),
            "seed": seed, "threads": 1, "time_limit_seconds": float(time_limit),
            "budget_elapsed_seconds": elapsed, "budget_overrun": elapsed > time_limit + 0.5,
            "message": h.modelStatusToString(status)}


def _graph_rows(graph):
    return [{"coefficients": {str(e["source"]): 1, str(e["target"]): 1},
             "lower": None, "upper": 1} for e in graph["edges"]]


def collapse_false_twins(graph):
    """동일 열린 이웃집합의 정점을 묶는 정확한 고전 전처리.

    같은 묶음의 정점은 인접하지 않으며 바깥 이웃이 동일하다. 한 정점을 고른
    독립집합에는 나머지도 모두 추가할 수 있으므로 어떤 최대해는 각 묶음을
    전부 고르거나 전혀 고르지 않는다. 대표 정점 가중치=묶음 크기로 두면
    최적값과 해의 복원이 정확하다. 저장된 원그래프와 QPU 입력은 바꾸지 않는다.
    """
    started = time.perf_counter()
    adjacency = _adjacency(graph)
    classes = defaultdict(list)
    for vertex in sorted(adjacency):
        classes[frozenset(adjacency[vertex])].append(vertex)
    groups, representative, weights = {}, {}, {}
    for index, members in enumerate(classes.values()):
        key = f"twin:{index:06d}"
        groups[key] = members
        weights[key] = len(members)
        for member in members:
            representative[member] = key
    edges = set()
    for vertex, neighbors in adjacency.items():
        for neighbor in neighbors:
            a, b = representative[vertex], representative[neighbor]
            if a == b:
                raise RuntimeError("Adjacent vertices cannot have equal open neighborhoods")
            if a < b:
                edges.add((a, b))
    quotient = {"nodes": [{"id": key} for key in groups],
                "edges": [{"source": a, "target": b} for a, b in sorted(edges)]}
    return {"graph": quotient, "groups": groups, "weights": weights,
            "metadata": {"method": "exact_false_twin_quotient",
                         "before_nodes": len(adjacency), "before_edges": len(graph["edges"]),
                         "after_nodes": len(groups), "after_edges": len(edges),
                         "seconds": time.perf_counter() - started,
                         "unweighted_original": True}}


def _expand_twin_result(original, reduction, result, time_limit):
    """가중 대표 해와 상계를 원래 비가중 정점 수 단위로 복원한다."""
    started = time.perf_counter()
    selected = sorted(vertex for key in result["selected_ids"]
                      for vertex in reduction["groups"][key])
    chosen = set(selected)
    if any(str(e["source"]) in chosen and str(e["target"]) in chosen for e in original["edges"]):
        raise RuntimeError("Expanded false-twin solution is not independent")
    if result["objective"] is not None and result["objective"] != len(selected):
        raise RuntimeError("Weighted objective does not match expanded cardinality")
    preprocessing_seconds = reduction["metadata"]["seconds"]
    result["selected_ids"] = selected
    result["preprocessing"] = reduction["metadata"]
    result["preprocessing_seconds"] = preprocessing_seconds
    result["expansion_validation_seconds"] = time.perf_counter() - started
    result["unweighted_original"] = True
    result["solver_formulation"] = "exact_weighted_false_twin_quotient_of_unweighted_graph"
    result["preprocessing_charged_within_budget"] = True
    result["native_time_limit_seconds"] = result["time_limit_seconds"]
    result["time_limit_seconds"] = time_limit
    result["budget_elapsed_seconds"] = preprocessing_seconds + result["solver_seconds"]
    result["budget_overrun"] = result["budget_elapsed_seconds"] > time_limit + 0.5
    if result.get("proof_seconds") is not None:
        result["proof_seconds"] += preprocessing_seconds
    if result.get("first_incumbent_seconds") is not None:
        result["first_incumbent_seconds"] += preprocessing_seconds
    for point in result.get("incumbent_trace", []):
        point["seconds"] += preprocessing_seconds
    return result


def _preprocessing_exhausted(graph, reduction, time_limit, seed):
    result = {"status": "limit_reached", "optimal": False, "objective": 0,
              "selected_ids": [], "upper_bound": len(graph["nodes"]),
              "solver_seconds": 0.0, "model_seconds": 0.0, "proof_seconds": None,
              "seed": seed, "threads": 1, "time_limit_seconds": 0.0,
              "reason": "preprocessing_exhausted_time_budget"}
    return _expand_twin_result(graph, reduction, result, time_limit)


def solve_highs_graph(graph, time_limit=60.0, seed=20261109):
    """정확 쌍둥이 축약 후 HiGHS로 풀고 원래 비가중 해를 반환한다."""
    reduction = collapse_false_twins(graph)
    remaining = time_limit - reduction["metadata"]["seconds"]
    if remaining <= 0:
        return _preprocessing_exhausted(graph, reduction, time_limit, seed)
    quotient = reduction["graph"]
    result = solve_highs([n["id"] for n in quotient["nodes"]], _graph_rows(quotient),
                         remaining, seed, reduction["weights"])
    return _expand_twin_result(graph, reduction, result, time_limit)


def solve_cpsat_graph(graph, time_limit=60.0, seed=20261109):
    """같은 정확 쌍둥이 축약을 적용한 뒤 CP-SAT 단일 스레드로 푼다."""
    import ortools
    from ortools.sat.python import cp_model
    reduction = collapse_false_twins(graph)
    remaining = time_limit - reduction["metadata"]["seconds"]
    if remaining <= 0:
        return _preprocessing_exhausted(graph, reduction, time_limit, seed)
    quotient, weights = reduction["graph"], reduction["weights"]
    begin = time.perf_counter()
    ids = [str(node["id"]) for node in quotient["nodes"]]
    model = cp_model.CpModel()
    variables = {key: model.new_bool_var(key) for key in ids}
    for edge in quotient["edges"]:
        model.add(variables[str(edge["source"])] + variables[str(edge["target"])] <= 1)
    model.maximize(sum(weights[key] * variable for key, variable in variables.items()))
    model_seconds = time.perf_counter() - begin
    solver = cp_model.CpSolver()
    solver.parameters.max_time_in_seconds = float(remaining)
    solver.parameters.num_search_workers = 1
    # 개발 입력에서 확인한 LP 선형화 설정을 평가 입력을 보기 전에 고정한다.
    # 축약 그래프의 개수 가중 목적에 대한 상계 계산을 활성화한다.
    solver.parameters.linearization_level = 2
    solver.parameters.random_seed = int(seed)
    solver.parameters.log_search_progress = False
    class Progress(cp_model.CpSolverSolutionCallback):
        def __init__(self):
            super().__init__()
            self.trace = []

        def on_solution_callback(self):
            self.trace.append({"seconds": float(self.wall_time),
                               "objective": int(round(self.objective_value))})
    progress = Progress()
    begin = time.perf_counter()
    status = solver.solve(model, progress)
    elapsed = time.perf_counter() - begin
    feasible = status in (cp_model.OPTIMAL, cp_model.FEASIBLE)
    chosen = sorted(key for key in ids if solver.value(variables[key])) if feasible else []
    adjacency = _adjacency(quotient)
    if any(adjacency[key] & set(chosen) for key in chosen):
        raise RuntimeError("CP-SAT returned an invalid independent set")
    statuses = {cp_model.OPTIMAL: "optimal", cp_model.FEASIBLE: "limit_reached",
                cp_model.UNKNOWN: "limit_reached", cp_model.INFEASIBLE: "infeasible",
                cp_model.MODEL_INVALID: "model_invalid"}
    upper = (min(sum(weights.values()), math.floor(solver.best_objective_bound + 1e-7))
             if feasible else sum(weights.values()) if status == cp_model.UNKNOWN else None)
    result = {"status": statuses.get(status, "solver_error"), "optimal": status == cp_model.OPTIMAL,
            "objective": sum(weights[key] for key in chosen) if feasible else None, "selected_ids": chosen,
            "upper_bound": upper, "model_seconds": model_seconds, "solver_seconds": elapsed,
            "proof_seconds": elapsed if status == cp_model.OPTIMAL else None,
            "first_incumbent_seconds": progress.trace[0]["seconds"] if progress.trace else None,
            "incumbent_trace": progress.trace,
            "incumbent_time_reason": "callback_trace" if progress.trace else "no_callback_emitted",
            "seed": seed, "threads": 1, "time_limit_seconds": float(remaining),
            "solver_version": ortools.__version__, "linearization_level": 2,
            "message": solver.status_name(status)}
    return _expand_twin_result(graph, reduction, result, time_limit)


def redumis_availability(config=None):
    """명시한 실행파일 또는 작업공간의 공식 빌드를 발견한다. 다운로드/설치는 하지 않는다."""
    requested = (config or {}).get("redumis_executable")
    project = Path(__file__).resolve().parents[1]
    paths = ([Path(requested)] if requested else [
        project / "outputs/tools/KaMIS/deploy/redumis.exe",
        project / "outputs/tools/KaMIS/deploy/redumis"])
    for path in paths:
        if path.is_file():
            return {"status": "available", "path": str(path.resolve()),
                    "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
    return {"status": "unavailable", "executed": False,
            "reason": "Official KaMIS requires unguarded POSIX sys/resource.h and getrlimit/setrlimit; no Windows binary or compatible wheel found",
            "source": "https://github.com/KarlsruheMIS/KaMIS",
            "evidence": "outputs/tools/redumis_availability.json"}


def solve_redumis_graph(graph, executable, folder, time_limit=60.0, seed=20261109):
    """공식 ReduMIS 명령행을 위한 어댑터. 반환한 비트열은 원그래프에서 다시 검사한다."""
    started = time.perf_counter()
    adjacency = _adjacency(graph)
    ids = sorted(adjacency)
    index = {key: i + 1 for i, key in enumerate(ids)}
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    graph_path, selection_path = folder / "input.metis", folder / f"selection_{seed}.txt"
    graph_path.write_text(f"{len(ids)} {len(graph['edges'])}\n" + "".join(
        " ".join(str(index[v]) for v in sorted(adjacency[key])) + "\n" for key in ids), encoding="ascii")
    # 이전 실행 결과를 재사용하지 않는 seed별 파일명이다. 존재하면 명시적으로 거절한다.
    if selection_path.exists():
        raise ValueError("ReduMIS output already exists; use a new run directory")
    model_seconds = time.perf_counter() - started
    environment = {**os.environ, "OMP_NUM_THREADS": "1", "OMP_DYNAMIC": "FALSE"}
    started = time.perf_counter()
    if ids:
        completed = subprocess.run([str(executable), str(graph_path.resolve()),
            f"--output={selection_path.resolve()}", f"--seed={seed}", f"--time_limit={time_limit}"],
            capture_output=True, text=True, encoding="utf-8", errors="replace", env=environment,
            timeout=time_limit + 5, check=False)
        (folder / f"log_{seed}.txt").write_text(completed.stdout + completed.stderr, encoding="utf-8")
        if completed.returncode != 0 or not selection_path.exists():
            raise RuntimeError("ReduMIS failed or did not produce a selection file")
        labels = selection_path.read_text(encoding="ascii").split()
        if len(labels) != len(ids) or any(value not in ("0", "1") for value in labels):
            raise ValueError("ReduMIS output does not contain exactly one binary label per node")
        chosen = [key for key, label in zip(ids, labels) if label == "1"]
    else:
        chosen = []
    elapsed = time.perf_counter() - started
    if any(adjacency[key] & set(chosen) for key in chosen):
        raise ValueError("ReduMIS returned a non-independent selection")
    return {"status": "heuristic_complete", "optimal": False, "objective": len(chosen),
            "selected_ids": chosen, "upper_bound": len(ids), "model_seconds": model_seconds,
            "solver_seconds": elapsed, "proof_seconds": None, "seed": seed,
            "threads": 1, "time_limit_seconds": time_limit}


def _size_bin(value):
    if value == 0:
        return "certified_zero"
    low = 2 ** int(math.floor(math.log2(value)))
    return f"{low}-{2 * low - 1}"


def build_rd1_graphs(case, folder, config=None):
    """원모형 증인을 검산한 뒤 후보 그래프와 정확 주 그래프를 저장한다."""
    cfg, folder = _config(config), Path(folder)
    case_hash = _save(folder / "case.json", case)
    baseline = validate_selection(case, [])
    result = {"case_id": case.get("case_id"), "status": "source_model_invalid",
              "case_sha256": case_hash, "baseline_validation": baseline,
              "candidate_graph": None, "exact_graph": None, "audit": None,
              "qpu_executed": False}
    started = time.perf_counter()
    if baseline["feasible"]:
        try:
            graph = build_graph(case, max_edges=cfg["graph_max_edges"],
                                inference_seconds=cfg["graph_seconds"], max_seconds=cfg["graph_seconds"])
            result["candidate_graph"] = graph
            result["candidate_graph_sha256"] = _save(folder / "graph.json", graph)
            if graph["metadata"]["is_exact_for_declared_model"]:
                exact = graph
                result["equivalence_certificate"] = "pairwise_sufficient_conditions_for_this_case"
            else:
                remaining_seconds = min(cfg["encoding_max_seconds"],
                                        cfg["graph_seconds"] - (time.perf_counter() - started))
                if remaining_seconds <= 0:
                    raise EncodingLimitError("Combined graph construction wall-clock limit exceeded")
                exact = encode_operational_mis(
                    case, max_support=cfg["encoding_max_support"],
                    max_clauses=cfg["encoding_max_clauses"], max_nodes=cfg["encoding_max_nodes"],
                    max_edges=cfg["encoding_max_edges"], max_seconds=remaining_seconds)
                # 빈 운영 선택의 lift/decode는 항등식 적용을 위한 실행 가능 증인이다.
                witness = lift_operational_selection(exact, [])
                if not decode_operational_mis(exact, witness)["operational_feasible"]:
                    raise RuntimeError("Source feasibility witness did not survive exact encoding")
                result["equivalence_certificate"] = "bounded_cnf_theorem_and_validated_source_witness"
                exact["metadata"]["feasibility"] = "validated_zero_source_witness"
            result["exact_graph"] = exact
            exact["metadata"]["source_case_sha256"] = case_hash
            result["exact_graph_sha256"] = _save(folder / "exact_graph.json", exact)
            result["graph_construction_seconds"] = time.perf_counter() - started
            auxiliary = "encoding_map" in exact
            audit_started = time.perf_counter()
            audit = audit_graph(exact, scan_seconds=cfg["structure_scan_seconds"],
                                require_cross_request_conflicts=not auxiliary)
            # 보조 정점에는 request_id가 없다. 후보 전용 누락/교차요청 gate를 적용하지 않는다.
            audit["representation"] = exact["metadata"]["representation"]
            audit["candidate_only_gates_applied"] = not auxiliary
            audit["residual_size_bin"] = _size_bin(audit["reduction"]["largest_residual_component"])
            if auxiliary:
                audit["structural_candidate_eligible"] = not audit["certificate_verified"]
            result.update(status="ready", audit=audit)
            result["structure_audit_seconds"] = time.perf_counter() - audit_started
            _save(folder / "structure_audit.json", audit)
        except EncodingLimitError as exc:
            result.update(status="encoding_limit_exceeded", reason=str(exc))
        except InfeasibleModelError as exc:
            result.update(status="encoding_inconsistent_with_source_witness", reason=str(exc))
        except (ValueError, MemoryError) as exc:
            result.update(status="graph_generation_failed", reason=f"{type(exc).__name__}: {exc}")
    result["construction_seconds"] = time.perf_counter() - started
    _save(folder / "build_summary.json", {key: value for key, value in result.items()
                                          if key not in ("candidate_graph", "exact_graph", "audit")})
    return result


def _decode(case, graph, selected):
    if "encoding_map" in graph:
        return decode_operational_mis(graph, selected)
    check = validate_selection(case, selected)
    adjacency = _adjacency(graph)
    independent = set(selected) <= adjacency.keys() and not any(
        adjacency[key] & set(selected) for key in selected)
    return {"operational_feasible": check["feasible"] and independent,
            "selected_candidate_ids": sorted(selected), "selected_candidate_count": len(selected),
            "independent": independent, "clause_complete": None}


def greedy_operational(case, by_benefit=False):
    """영향받는 희소 제약만 갱신하는 실행 가능 탐욕 기준선.

    빈 선택이 가능한 주 모형에서 시작한다. 후보를 추가할 때 변하는 행만
    검사하므로 매번 전체 후보·제약을 복사하지 않는다. 공급을 먼저 선택해야
    가능한 조합은 여전히 순서에 따라 놓칠 수 있으며, 재탐색하지 않는다.
    """
    candidates = case["candidates"]
    ids, rows = operational_rows(case)
    if not _row_feasible(rows, []):
        return []
    if by_benefit:
        ordered = sorted(candidates, key=lambda c: (
            -float(c.get("individual_benefit", c.get("benefit", 0))), c["id"]))
    else:
        ordered = sorted(candidates, key=lambda c: c["id"])

    # 후보 -> 그 후보가 계수를 가진 행의 역색인을 한 번 만든다.
    incidence = {identifier: [] for identifier in ids}
    for row_index, row in enumerate(rows):
        for identifier, coefficient in row["coefficients"].items():
            if coefficient:
                incidence[identifier].append((row_index, coefficient))
    activity = [0.0] * len(rows)
    chosen = []
    for candidate in ordered:
        identifier = str(candidate["id"])
        affected = incidence[identifier]
        feasible = True
        for row_index, coefficient in affected:
            row = rows[row_index]
            value = activity[row_index] + coefficient
            if ((row["lower"] is not None and value < row["lower"] - 1e-7)
                    or (row["upper"] is not None and value > row["upper"] + 1e-7)):
                feasible = False
                break
        if feasible:
            chosen.append(identifier)
            for row_index, coefficient in affected:
                activity[row_index] += coefficient
    return sorted(chosen)


def benchmark_rd1_case(case, graph, folder, config=None, audit=None):
    """60초×5회 고전 비교를 수행하고 같은 추천 수 단위의 상하계를 합친다.

    graph=None인 인코딩 실패도 원모형 측정은 남기지만 완전한 비교로 표시하지 않는다.
    ReduMIS 실행파일이 없으면 누락을 남기며 다른 알고리즘으로 대체하지 않는다.
    """
    cfg, folder = _config(config), Path(folder)
    if graph is not None:
        case_hash = hashlib.sha256(json.dumps(case, ensure_ascii=False, allow_nan=False,
                                             indent=2).encode("utf-8")).hexdigest()
        if graph.get("metadata", {}).get("source_case_sha256") != case_hash:
            raise ValueError("Exact graph is not bound to the supplied source case")
    ids, rows = operational_rows(case)
    runs = []
    redumis = redumis_availability(cfg)
    offset = graph.get("encoding_map", {}).get("objective_offset", 0) if graph else 0
    baseline_valid = validate_selection(case, [])["feasible"]
    feasible_solutions = [[]] if baseline_valid else []
    upper_bounds = [len({c["request_id"] for c in case["candidates"]})]
    if graph is not None and audit and audit.get("certificate_verified"):
        certificate = audit["certificate"]
        if not verify_certificate(graph, certificate):
            raise ValueError("Structural certificate does not match the exact graph")
        decoded = _decode(case, graph, certificate["selected_ids"])
        if not decoded["operational_feasible"]:
            raise RuntimeError("Exact graph optimum certificate did not decode to a feasible selection")
        feasible_solutions.append(decoded["selected_candidate_ids"])
        upper_bounds.append(certificate["objective"] - offset)
    for seed in cfg["seeds"]:
        algorithms = [("highs_operational", lambda: solve_highs(ids, rows, cfg["time_limit_seconds"], seed))]
        if graph is not None:
            algorithms.extend([
                ("highs_graph", lambda: solve_highs_graph(graph, cfg["time_limit_seconds"], seed)),
                ("cpsat_graph", lambda: solve_cpsat_graph(graph, cfg["time_limit_seconds"], seed))])
            if redumis["status"] == "available":
                algorithms.append(("redumis", lambda: solve_redumis_graph(
                    graph, redumis["path"], folder / "redumis", cfg["time_limit_seconds"], seed)))
        for name, execute in algorithms:
            run_started = time.perf_counter()
            try:
                run = execute()
            except (ImportError, OSError, RuntimeError, ValueError, subprocess.TimeoutExpired) as exc:
                run = {"status": "solver_unavailable" if isinstance(exc, (ImportError, OSError)) else "solver_error",
                       "error": f"{type(exc).__name__}: {exc}", "objective": None,
                       "upper_bound": None, "selected_ids": [], "optimal": False,
                       "solver_seconds": time.perf_counter() - run_started, "model_seconds": 0.0,
                       "failed_run_timing_includes_preparation": True, "seed": seed, "threads": 1}
            run["algorithm"] = name
            run["fixed_budget_eligible"] = not run.get("budget_overrun", False)
            run["objective_unit"] = "recommendations" if name == "highs_operational" else "graph_nodes"
            run["incumbent_trace_unit"] = run["objective_unit"]
            run["recommendation_count"] = None
            run["recommendation_upper_bound"] = None
            if name == "highs_operational":
                run["validation"] = validate_selection(case, run["selected_ids"]) if run["objective"] is not None else None
                valid = run["objective"] is not None and run["validation"]["feasible"]
                if valid:
                    run["recommendation_count"] = len(run["selected_ids"])
                    if run["fixed_budget_eligible"]:
                        feasible_solutions.append(run["selected_ids"])
                    if graph is not None:
                        begin = time.perf_counter()
                        lifted = lift_operational_selection(graph, run["selected_ids"]) if "encoding_map" in graph else run["selected_ids"]
                        run["lift_seconds"] = time.perf_counter() - begin
                        run["lifted_graph_objective"] = len(lifted)
                        run["lift_validated"] = _decode(case, graph, lifted)["operational_feasible"]
                run["recommendation_upper_bound"] = run["upper_bound"]
            else:
                run["raw_graph_objective"] = run["objective"]
                run["raw_graph_upper_bound"] = run["upper_bound"]
                if run["objective"] is not None:
                    begin = time.perf_counter()
                    decoded = _decode(case, graph, run["selected_ids"])
                    run["decode_seconds"] = time.perf_counter() - begin
                    run["decode"] = decoded
                    run["validation"] = {"feasible": decoded["operational_feasible"],
                                         "scope": "graph_and_declared_operational_model"}
                    if decoded["operational_feasible"]:
                        run["recommendation_count"] = decoded["selected_candidate_count"]
                        if run["fixed_budget_eligible"]:
                            feasible_solutions.append(decoded["selected_candidate_ids"])
                if run["upper_bound"] is not None and baseline_valid:
                    run["recommendation_upper_bound"] = run["upper_bound"] - offset
            if run["recommendation_upper_bound"] is not None and run["fixed_budget_eligible"]:
                upper_bounds.append(run["recommendation_upper_bound"])
            runs.append(run)
            # 각 실행 후 저장하므로 중단된 캠페인도 완료 실행은 복구할 수 있다.
            _save(folder / "classical_runs.json", runs)
    best = min(feasible_solutions, key=lambda chosen: (-len(chosen), tuple(sorted(chosen)))) if feasible_solutions else None
    lower, upper = (len(best) if best is not None else None), min(upper_bounds)
    if lower is not None and upper < lower:
        raise RuntimeError("Inconsistent recommendation-count lower/upper bounds")
    required_count = len(cfg["seeds"]) * 3
    exact_runs = [r for r in runs if r["algorithm"] != "redumis"]
    normal = len(exact_runs) == required_count and all(
        r["status"] in ("optimal", "limit_reached") and not r.get("budget_overrun", False)
        for r in exact_runs)
    proof_medians = []
    for algorithm in ("highs_operational", "highs_graph", "cpsat_graph"):
        group = [r for r in runs if r["algorithm"] == algorithm]
        if len(group) == len(cfg["seeds"]) and all(r["optimal"] and not r.get("budget_overrun", False) for r in group):
            proof_medians.append(statistics.median(r["proof_seconds"] for r in group))
    result = {
        "case_id": case.get("case_id"), "runs": runs, "protocol": cfg,
        "lower_bound": lower, "upper_bound": upper,
        "relative_gap": (upper - lower) / max(1, upper) if lower is not None else None,
        "optimality_proven": lower is not None and lower == upper,
        "selected_candidate_ids": sorted(best) if best is not None else [],
        "all_required_exact_runs_normal": normal,
        "all_required_exact_runs_unproven": normal and all(not r["optimal"] for r in exact_runs),
        "fastest_exact_median_seconds": min(proof_medians) if proof_medians else None,
        "residual_size_bin": audit.get("residual_size_bin") if audit else None,
        "certificate_easy": bool(audit and audit.get("certificate_verified")),
        "portfolio_solver_seconds": sum(r["solver_seconds"] for r in runs),
        "portfolio_preprocessing_seconds": sum(r.get("preprocessing_seconds", 0) for r in runs),
        "portfolio_expansion_validation_seconds": sum(r.get("expansion_validation_seconds", 0) for r in runs),
        "budget_overrun_count": sum(bool(r.get("budget_overrun")) for r in runs),
        "portfolio_model_seconds": sum(r["model_seconds"] for r in runs),
        "portfolio_lift_decode_seconds": sum(r.get("lift_seconds", 0) + r.get("decode_seconds", 0) for r in runs),
        "missing_exact_graph_comparison": graph is None,
        "redumis": {**redumis, "executed": any(r["algorithm"] == "redumis" for r in runs)},
        "platform": platform.platform(), "qpu_executed": False,
        "primary_budget_compliant": cfg["time_limit_seconds"] == 60.0 and cfg["seeds"] == DEFAULTS["seeds"]
                                    and not any(r.get("budget_overrun", False) for r in runs),
    }
    _save(folder / "classical_benchmark.json", result)
    return result


def classify_hardness(results):
    """같은 잔여 규모군 20건 이상에서 사전 규칙으로 상대 난도를 판정한다."""
    groups = defaultdict(list)
    for row in results:
        seconds = row.get("fastest_exact_median_seconds")
        if row.get("all_required_exact_runs_normal") and seconds is not None:
            groups[row.get("residual_size_bin")].append(seconds)
    classified = []
    for original in results:
        row = dict(original)
        values = groups[row.get("residual_size_bin")]
        threshold = float(np.quantile(values, 0.9)) if len(values) >= 20 else None
        seconds = row.get("fastest_exact_median_seconds")
        if row.get("primary_budget_compliant") is False or not row.get("all_required_exact_runs_normal"):
            label = "incomplete_benchmark"
        elif row.get("all_required_exact_runs_unproven") and (row.get("relative_gap") or 0) > 0:
            label = "unresolved_within_budget"
        elif row.get("certificate_easy") or row.get("residual_size_bin") == "certified_zero":
            label = "certificate_easy"
        elif threshold is not None and seconds is not None and seconds >= max(1.0, threshold):
            label = "relative_hard"
        else:
            label = "not_hard_under_registered_rule"
        row.update(hardness_class=label, size_bin_sample_count=len(values), size_bin_p90_seconds=threshold)
        classified.append(row)
    return classified


def process_rd1_case(case, folder, config=None):
    """사례 저장 → 그래프 생성 → 고전 비교를 한 번에 실행하는 진입점."""
    built = build_rd1_graphs(case, folder, config)
    summary = {key: value for key, value in built.items()
               if key not in ("candidate_graph", "exact_graph", "audit")}
    if built["baseline_validation"]["feasible"]:
        summary["benchmark"] = benchmark_rd1_case(case, built["exact_graph"], folder,
                                                   config, built["audit"])
    return summary
