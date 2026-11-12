"""저장된 후보 그래프의 구조 인증과 이력 감사. QPU나 풀이기를 실행하지 않는다.

큰 그래프와 고난도 그래프를 구별한다. 잔여 그래프는 후속 고전 비교의
후보일 뿐이며, 이 모듈은 어떤 사례에도 고전적 난해성을 부여하지 않는다.
"""
from __future__ import annotations

from collections import defaultdict
import hashlib
import json
import math
from pathlib import Path
import time

from bike_research.graph import validate_selection


def sha256_bytes(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _finite_json_number(token):
    value = float(token)
    if not math.isfinite(value):
        raise ValueError(f"Non-finite JSON number: {token}")
    return value


def read_json(raw):
    """NaN·무한대·실수 오버플로를 읽을 때 거절해 배치 출력 실패를 막는다."""
    return json.loads(raw, parse_float=_finite_json_number, parse_constant=_finite_json_number)


def _graph_data(graph):
    """정점·간선 오류를 조용히 합치지 않고 입력 오류로 남긴다."""
    if not isinstance(graph, dict) or not isinstance(graph.get("nodes"), list):
        raise ValueError("graph.nodes must be a list")
    if not isinstance(graph.get("edges"), list):
        raise ValueError("graph.edges must be a list")
    if not isinstance(graph.get("metadata", {}), dict):
        raise ValueError("graph.metadata must be an object")
    if graph.get("metadata", {}).get("directed") or graph.get("metadata", {}).get("weighted"):
        raise ValueError("Only undirected unweighted candidate graphs are supported")
    adjacency, requests = {}, {}
    for node in graph["nodes"]:
        if not isinstance(node, dict) or node.get("id") is None:
            raise ValueError("Missing graph node ID")
        identifier = str(node["id"])
        if identifier in adjacency:
            raise ValueError(f"Duplicate graph node: {identifier}")
        adjacency[identifier] = set()
        request = node.get("request_id")
        requests[identifier] = str(request) if request is not None and str(request) else None
    for edge in graph["edges"]:
        if not isinstance(edge, dict) or "source" not in edge or "target" not in edge:
            raise ValueError("Malformed graph edge")
        source, target = str(edge["source"]), str(edge["target"])
        if source == target or source not in adjacency or target not in adjacency:
            raise ValueError("Unknown endpoint or self-loop")
        if target in adjacency[source]:
            raise ValueError("Duplicate undirected graph edge")
        adjacency[source].add(target)
        adjacency[target].add(source)
    return adjacency, requests


def _components(adjacency):
    unseen, components = set(adjacency), []
    while unseen:
        start = min(unseen)
        unseen.remove(start)
        stack, component = [start], []
        while stack:
            vertex = stack.pop()
            component.append(vertex)
            added = adjacency[vertex] & unseen
            unseen.difference_update(added)
            stack.extend(added)
        components.append(component)
    return components


def _is_bipartite(adjacency):
    colors = {}
    for start in adjacency:
        if start in colors:
            continue
        colors[start], stack = 0, [start]
        while stack:
            vertex = stack.pop()
            for neighbor in adjacency[vertex]:
                if neighbor in colors:
                    if colors[neighbor] == colors[vertex]:
                        return False
                else:
                    colors[neighbor] = 1 - colors[vertex]
                    stack.append(neighbor)
    return True


def _metrics(adjacency, requests):
    components = _components(adjacency)
    node_count = len(adjacency)
    edge_count = sum(map(len, adjacency.values())) // 2
    groups = defaultdict(list)
    request_ids_complete = all(value is not None for value in requests.values())
    for vertex, request in requests.items():
        if request is not None:
            groups[request].append(vertex)
    same = sum(requests[v] is not None and requests[v] == requests[w]
               for v in adjacency for w in adjacency[v] if v < w)
    # 모든 정점이 요청별 클리크로 덮인 경우에만 요청 수가 MIS 상한이다.
    cover_valid = request_ids_complete and same == sum(len(g) * (len(g) - 1) // 2
                                                      for g in groups.values())
    interaction = {request: set() for request in groups}
    for vertex in adjacency:
        for neighbor in adjacency[vertex]:
            a, b = requests[vertex], requests[neighbor]
            if a is not None and b is not None and a != b:
                interaction[a].add(b)
    interaction_components = _components(interaction)
    return {
        "node_count": node_count, "edge_count": edge_count,
        "density": 2 * edge_count / (node_count * (node_count - 1)) if node_count > 1 else 0.0,
        "component_count": len(components),
        "largest_component": max(map(len, components), default=0),
        "is_disjoint_union_of_cliques": all(
            all(len(adjacency[v]) == len(component) - 1 for v in component)
            for component in components),
        "is_bipartite": _is_bipartite(adjacency),
        "request_ids_complete": request_ids_complete,
        "request_clique_cover_valid": cover_valid,
        "request_upper_bound": len(groups) if cover_valid else None,
        "same_request_edges": same if request_ids_complete else None,
        "cross_request_edges": edge_count - same if request_ids_complete else None,
        "request_interaction_components": len(interaction_components) if request_ids_complete else None,
        "largest_request_interaction_component": max(map(len, interaction_components), default=0)
        if request_ids_complete else None,
    }


def _valid_independent_set(adjacency, selection):
    if not isinstance(selection, list) or any(v is None for v in selection):
        return False
    chosen = [str(v) for v in selection]
    selected = set(chosen)
    return (len(selected) == len(chosen) and selected <= adjacency.keys()
            and not any(adjacency[v] & selected for v in selected))


def _finite_number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _saved_graph_evidence(adjacency, saved):
    """과거 sidecar의 해만 재검증한다. 시간과 상계는 새 측정값이 아니다."""
    saved = saved if isinstance(saved, dict) else {}
    valid = _valid_independent_set(adjacency, saved.get("selected_ids"))
    objective, upper = saved.get("objective"), saved.get("upper_bound")
    matching = (valid and _finite_number(objective) and _finite_number(upper)
                and objective == len(saved["selected_ids"]) and upper == objective)
    return {
        "evidence_scope": "historical_unhashed_sidecar",
        "status": saved.get("status"), "objective": objective, "upper_bound": upper,
        "solver_seconds": saved.get("solver_seconds"),
        "incumbent_validated": valid,
        "consistent_saved_optimality": bool(matching and saved.get("status") == "optimal"
                                             and saved.get("optimal") is True),
    }


def _simplicial_reduction(adjacency, scan_seconds):
    """N(v)가 클리크면 v를 포함한 최적해가 존재한다. 매 단계 증거를 보존한다."""
    remaining = {v: set(neighbors) for v, neighbors in adjacency.items()}
    started, steps, stopped = time.perf_counter(), [], False
    while remaining:
        progress = False
        for vertex in sorted(remaining):
            if vertex not in remaining:
                continue
            if time.perf_counter() - started >= scan_seconds:
                stopped = True
                break
            neighbors = remaining[vertex]
            simplicial = True
            for neighbor in neighbors:
                if time.perf_counter() - started >= scan_seconds:
                    stopped, simplicial = True, False
                    break
                if not neighbors - {neighbor} <= remaining[neighbor]:
                    simplicial = False
                    break
            if stopped:
                break
            if not simplicial:
                continue
            removed = neighbors | {vertex}
            steps.append({"selected_id": vertex, "removed_ids": sorted(removed)})
            for member in removed:
                for other in remaining[member] - removed:
                    remaining[other].discard(member)
            for member in removed:
                del remaining[member]
            progress = True
        if stopped or not progress:
            break
    residual_ids = sorted(remaining)
    proof = {"type": "simplicial_elimination", "steps": steps,
             "selected_ids": [step["selected_id"] for step in steps],
             "objective_offset": len(steps), "residual_ids": residual_ids,
             "complete": not remaining, "objective": len(steps) if not remaining else None}
    reduction = {
        "residual_nodes": len(remaining),
        "residual_edges": sum(map(len, remaining.values())) // 2,
        "largest_residual_component": max(map(len, _components(remaining)), default=0),
        "objective_offset": len(steps), "scan_seconds": time.perf_counter() - started,
        "scan_limit_seconds": scan_seconds, "scan_truncated": stopped,
    }
    return proof, reduction


def verify_certificate(graph, certificate):
    """인증서를 원그래프부터 재생한다. 시간·풀이기 상태를 신뢰하지 않는다."""
    try:
        adjacency, requests = _graph_data(graph)
        if certificate["type"] == "request_clique_cover":
            metrics = _metrics(adjacency, requests)
            selected = certificate["selected_ids"]
            groups = defaultdict(list)
            for vertex, request in requests.items():
                groups[request].append(vertex)
            expected_groups = {k: sorted(v) for k, v in groups.items()}
            return bool(metrics["request_clique_cover_valid"]
                        and _valid_independent_set(adjacency, selected)
                        and len(selected) == metrics["request_upper_bound"]
                        and certificate["objective"] == len(selected)
                        and certificate["clique_cover"] == expected_groups)
        if certificate["type"] != "simplicial_elimination":
            return False
        remaining = {v: set(neighbors) for v, neighbors in adjacency.items()}
        selected = []
        for step in certificate["steps"]:
            vertex = step["selected_id"]
            if vertex not in remaining:
                return False
            neighbors = remaining[vertex]
            if not all(neighbors - {other} <= remaining[other] for other in neighbors):
                return False
            removed = neighbors | {vertex}
            if step["removed_ids"] != sorted(removed):
                return False
            selected.append(vertex)
            for member in removed:
                for other in remaining[member] - removed:
                    remaining[other].discard(member)
            for member in removed:
                del remaining[member]
        # 원그래프에서도 독립집합인지 확인하며 잔여 정점·offset 변조를 거절한다.
        return bool(_valid_independent_set(adjacency, selected)
                    and certificate["selected_ids"] == selected
                    and certificate["objective_offset"] == len(selected)
                    and certificate["residual_ids"] == sorted(remaining)
                    and certificate["complete"] == (not remaining)
                    and certificate["objective"] == (len(selected) if not remaining else None))
    except (KeyError, TypeError, ValueError):
        return False


def audit_graph(graph, saved_result=None, scan_seconds=5.0, *, require_cross_request_conflicts=True):
    if not _finite_number(scan_seconds) or scan_seconds < 0:
        raise ValueError("structure_scan_seconds must be finite and nonnegative")
    adjacency, requests = _graph_data(graph)
    metrics = _metrics(adjacency, requests)
    historical = _saved_graph_evidence(adjacency, saved_result)
    certificate, reduction_proof = None, None
    if historical["incumbent_validated"] and metrics["request_clique_cover_valid"]:
        selected = [str(v) for v in saved_result["selected_ids"]]
        if len(selected) == metrics["request_upper_bound"]:
            groups = defaultdict(list)
            for vertex, request in requests.items():
                groups[request].append(vertex)
            certificate = {"type": "request_clique_cover", "objective": len(selected),
                           "selected_ids": selected,
                           "clique_cover": {k: sorted(v) for k, v in groups.items()}}
    if certificate is None:
        reduction_proof, reduction = _simplicial_reduction(adjacency, float(scan_seconds))
        if not verify_certificate(graph, reduction_proof):
            raise RuntimeError("Simplicial reduction failed independent verification")
        if reduction_proof["complete"]:
            certificate = reduction_proof
    else:
        # 상계 일치로 해결됐다. 잔여 0은 실제 정점 삭제를 뜻하지 않는다.
        reduction = {"residual_nodes": 0, "residual_edges": 0,
                     "largest_residual_component": 0, "objective_offset": 0,
                     "scan_seconds": 0.0, "scan_limit_seconds": scan_seconds,
                     "scan_truncated": False, "not_needed_due_to_bound_certificate": True}
    if certificate is not None and not verify_certificate(graph, certificate):
        raise RuntimeError("Graph certificate failed independent verification")
    # 입력 해시에 묶이지 않은 과거 상계는 현재 최적성 증명으로 사용하지 않는다.
    classification = "certificate_easy" if certificate is not None else "requires_classical_confirmation"
    return {"classification": classification, "metrics": metrics,
            "certificate": certificate, "certificate_verified": certificate is not None,
            "reduction": reduction, "reduction_certificate": reduction_proof,
            "historical_graph": historical,
            "structural_evidence_complete": metrics["request_ids_complete"],
            "structural_candidate_eligible": classification == "requires_classical_confirmation"
            and reduction["residual_nodes"] > 0 and not metrics["is_bipartite"]
            and metrics["request_ids_complete"]
            and (not require_cross_request_conflicts or (metrics["cross_request_edges"] or 0) > 0),
            "interpretation": "Residual structure is not evidence of classical hardness."}


def _read_sidecar(folder, filename, warnings):
    path = folder / filename
    if not path.exists():
        warnings.append(f"missing_optional_sidecar:{filename}")
        return None
    try:
        result = read_json(path.read_text(encoding="utf-8-sig"))
        if not isinstance(result, dict):
            raise ValueError("sidecar must be an object")
        return result
    except (ValueError, OSError) as error:
        warnings.append(f"invalid_optional_sidecar:{filename}:{error}")
        return None


def audit_case(folder, summary_row, policy):
    """요약 한 행을 감사한다. 오류는 행을 지우지 않고 invalid_input으로 반환한다."""
    folder = Path(folder)
    result = {"case_id": summary_row.get("case_id"), "classification": "invalid_input",
              "case_directory": str(folder.resolve()), "warnings": [], "errors": [],
              "full_operational_model_eligible": False, "structural_candidate_eligible": False}
    try:
        case_raw, graph_raw = (folder / "case.json").read_bytes(), (folder / "graph.json").read_bytes()
        case, graph = read_json(case_raw), read_json(graph_raw)
        if not isinstance(case, dict) or not isinstance(graph, dict):
            raise ValueError("case.json and graph.json must be objects")
        if any(not isinstance(document.get(key, {}), dict) for document, key in
               ((graph, "metadata"), (case, "metadata"), (case, "inventory_audit"))):
            raise ValueError("metadata and inventory_audit must be objects")
        case_hash, graph_hash = sha256_bytes(case_raw), sha256_bytes(graph_raw)
        result.update(case_sha256=case_hash, graph_sha256=graph_hash)
        if summary_row.get("input_sha256") != case_hash:
            raise ValueError("Summary input_sha256 does not match case.json")
        if graph.get("metadata", {}).get("case_sha256") != case_hash:
            raise ValueError("Graph case_sha256 does not match case.json")
        if case.get("case_id") != result["case_id"] or graph.get("case_id") != result["case_id"]:
            raise ValueError("Case ID differs between summary, case and graph")
        adjacency, requests = _graph_data(graph)
        # 전체 운영 입력의 ID·계수도 검사한다. 빈 선택의 불가능은 입력 오류가 아니다.
        baseline_validated = validate_selection(case, [])["feasible"]
        candidates = {str(c["id"]): str(c["request_id"]) for c in case["candidates"]}
        if not adjacency.keys() <= candidates.keys():
            raise ValueError("Graph node is not a case candidate")
        if any(request is not None and request != candidates[v] for v, request in requests.items()):
            raise ValueError("Graph request ID differs from case candidate")
        saved = _read_sidecar(folder, "graph_benchmark.json", result["warnings"])
        graph_audit = audit_graph(graph, saved, policy.get("structure_scan_seconds", 5),
            require_cross_request_conflicts=policy.get("structural_screen", {}).get(
                "require_nonzero_cross_request_conflicts_for_graph_hardness_search", True))
        result.update(graph_audit)
        representation = graph.get("metadata", {}).get("representation", "unknown")
        result.update(representation=representation,
                      baseline_feasible=case.get("inventory_audit", {}).get("baseline_feasible"),
                      baseline_selection_validated=baseline_validated,
                      inventory_model=case.get("metadata", {}).get("inventory_model", "event_reconstruction"),
                      full_operational_model_eligible=representation == "exact_declared_constraints"
                      and graph.get("metadata", {}).get("is_exact_for_declared_model") is True
                      and baseline_validated
                      and case.get("inventory_audit", {}).get("baseline_feasible") is True,
                      input_hash_verified=True)
        if saved is not None and graph_audit["historical_graph"]["incumbent_validated"]:
            result["historical_graph"]["operational_feasible"] = validate_selection(
                case, saved["selected_ids"])["feasible"]
        operating = _read_sidecar(folder, "operational_benchmark.json", result["warnings"])
        operational = {"evidence_scope": "historical_unhashed_sidecar", "status": None,
                       "incumbent_validated": False, "consistent_saved_optimality": False}
        if operating is not None:
            selected = operating.get("selected_ids")
            valid = isinstance(selected, list) and validate_selection(case, selected)["feasible"]
            objective, upper = operating.get("objective"), operating.get("upper_bound")
            operational.update(status=operating.get("status"), objective=objective, upper_bound=upper,
                               solver_seconds=operating.get("solver_seconds"), incumbent_validated=valid,
                               consistent_saved_optimality=bool(valid and _finite_number(objective)
                                   and _finite_number(upper) and objective == len(selected) == upper
                                   and operating.get("status") == "optimal" and operating.get("optimal") is True))
        result["operational"] = operational
        result["graph_vs_operational_scope"] = (
            "Graph certificate concerns candidate MIS only; the full declared operational model "
            "must be evaluated separately. Historical timings are not a new repeated benchmark.")
    except (OSError, ValueError, KeyError, TypeError, AttributeError, RuntimeError) as error:
        result["classification"] = "invalid_input"
        result["structural_candidate_eligible"] = False
        result["full_operational_model_eligible"] = False
        result["errors"].append(f"{type(error).__name__}: {error}")
    return result
