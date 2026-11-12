"""인증 논리용 작은 그래프이며 합성 연구 사례로 사용하지 않는다."""
import copy
import csv
import hashlib
import itertools
import json

import pytest


def graph(nodes, edges, requests=None):
    return {
        "nodes": [dict(id=v, **({"request_id": requests[v]} if requests else {}))
                  for v in nodes],
        "edges": [{"source": a, "target": b} for a, b in edges],
        "metadata": {"representation": "pairwise_relaxation"},
    }


def cycle_five():
    return graph(list("abcde"), [("a", "b"), ("b", "c"), ("c", "d"),
                                   ("d", "e"), ("e", "a")])


def test_large_disconnected_cliques_have_exact_certificate():
    # 정점 수만으로 고난도로 분류하는 회귀를 막는다.
    from bike_research.hardness import audit_graph, verify_certificate
    groups = [[f"{i}:{j}" for j in range(8)] for i in range(1000)]
    g = graph([v for group in groups for v in group],
              [pair for group in groups for pair in itertools.combinations(group, 2)])
    result = audit_graph(g)
    assert result["classification"] == "certificate_easy"
    assert result["metrics"]["largest_component"] == 8
    assert result["certificate"]["objective"] == 1000
    assert verify_certificate(g, result["certificate"])


def test_connected_request_clique_bound_certifies_optimum():
    from bike_research.hardness import audit_graph, verify_certificate
    g = graph(list("abcd"), [("a", "b"), ("b", "c"), ("c", "d")],
              {"a": "r1", "b": "r1", "c": "r2", "d": "r2"})
    result = audit_graph(g, {"selected_ids": ["a", "d"]})
    assert result["classification"] == "certificate_easy"
    assert result["certificate"]["type"] == "request_clique_cover"
    assert result["certificate"]["objective"] == 2
    assert result["metrics"]["cross_request_edges"] == 1
    assert result["metrics"]["largest_request_interaction_component"] == 2
    assert verify_certificate(g, result["certificate"])


def test_simplicial_elimination_and_tampered_certificate():
    from bike_research.hardness import audit_graph, verify_certificate
    g = graph(list("abcde"), [("a", "b"), ("b", "c"), ("c", "d"), ("d", "e")])
    result = audit_graph(g)
    assert result["certificate"]["type"] == "simplicial_elimination"
    assert result["certificate"]["objective"] == 3
    assert result["reduction"]["residual_nodes"] == 0
    assert verify_certificate(g, result["certificate"])
    tampered = copy.deepcopy(result["certificate"])
    tampered["steps"][0]["removed_ids"] = ["a"]
    assert not verify_certificate(g, tampered)


def test_non_simplicial_graph_is_only_structural_candidate():
    from bike_research.hardness import audit_graph
    result = audit_graph(cycle_five())
    assert result["classification"] == "requires_classical_confirmation"
    assert result["certificate"] is None
    assert result["reduction"]["residual_nodes"] == 5
    assert result["reduction"]["largest_residual_component"] == 5


def test_unhashed_saved_optimality_never_replaces_new_certificate():
    from bike_research.hardness import audit_graph
    proof = {"status": "optimal", "optimal": True, "selected_ids": ["a", "c"],
             "objective": 2, "upper_bound": 2}
    # C7의 진짜 최적값은 3이다. 유효한 2개 해와 위조 상계 2를 믿으면 안 된다.
    g = graph(list("abcdefg"), [("a", "b"), ("b", "c"), ("c", "d"),
                                 ("d", "e"), ("e", "f"), ("f", "g"), ("g", "a")])
    for node in g["nodes"]:
        node["request_id"] = node["id"]
    result = audit_graph(g, proof)
    assert result["classification"] == "requires_classical_confirmation"
    assert result["structural_candidate_eligible"] is True
    assert result["historical_graph"]["consistent_saved_optimality"] is True
    for change in ({"status": "solver_error"}, {"upper_bound": 3},
                   {"selected_ids": ["a", "a"]}, {"selected_ids": ["a", "b"]}):
        assert audit_graph(cycle_five(), {**proof, **change})["classification"] == "requires_classical_confirmation"


def test_missing_request_id_and_missing_clique_edge_do_not_create_bound():
    from bike_research.hardness import audit_graph
    g = cycle_five()
    for node in g["nodes"]:
        node["request_id"] = "same"
    result = audit_graph(g, {"selected_ids": ["a", "c"]})
    assert result["metrics"]["request_clique_cover_valid"] is False
    assert result["classification"] == "requires_classical_confirmation"
    del g["nodes"][0]["request_id"]
    result = audit_graph(g, {"selected_ids": ["a", "c"]})
    assert result["metrics"]["request_upper_bound"] is None


@pytest.mark.parametrize("malformed", [
    graph(["a", "a"], []), graph(["a"], [("a", "missing")]),
    graph(["a"], [("a", "a")]), graph(["a", "b"], [("a", "b"), ("b", "a")]),
])
def test_malformed_graph_is_rejected(malformed):
    from bike_research.hardness import audit_graph
    with pytest.raises(ValueError):
        audit_graph(malformed)


def save_case(folder, case_id, stale=False, representation="pairwise_relaxation"):
    folder.mkdir(parents=True)
    case = {"case_id": case_id, "candidates": [{"id": v, "request_id": v} for v in "abcde"],
            "constraints": [], "inventory_audit": {"baseline_feasible": True}, "metadata": {}}
    raw = json.dumps(case).encode()
    (folder / "case.json").write_bytes(raw)
    digest = hashlib.sha256(raw).hexdigest()
    g = cycle_five()
    g["case_id"] = case_id
    for node in g["nodes"]:
        node["request_id"] = node["id"]
    g["metadata"].update(case_sha256="stale" if stale else digest,
                         representation=representation)
    (folder / "graph.json").write_text(json.dumps(g), encoding="utf-8")
    return {"case_id": case_id, "input_sha256": digest}


@pytest.mark.parametrize("bad_kind", ["hash", "metadata", "root"])
def test_stale_hash_is_retained_as_error_and_other_case_is_processed(tmp_path, bad_kind):
    from select_hard_cases import run_selection
    rows = [save_case(tmp_path / "cases" / name, name, stale=(name == "bad"))
            for name in ("bad", "good")]
    bad_path = tmp_path / "cases/bad/graph.json"
    if bad_kind == "metadata":
        bad_graph = json.loads(bad_path.read_text())
        bad_graph["metadata"] = []
        bad_path.write_text(json.dumps(bad_graph), encoding="utf-8")
    elif bad_kind == "root":
        bad_path.write_text("[]", encoding="utf-8")
    summary = tmp_path / "summary.csv"
    with summary.open("w", newline="", encoding="utf-8") as stream:
        writer = csv.DictWriter(stream, fieldnames=["case_id", "input_sha256"])
        writer.writeheader()
        writer.writerows(rows)
    policy = tmp_path / "policy.json"
    policy.write_text(json.dumps({"structure_scan_seconds": 5, "shortlist_per_size_bin": 3,
                                 "size_bin": "powers_of_two", "seed": 20261109}), encoding="utf-8")
    out = tmp_path / "audit"
    manifest = run_selection([summary], out, policy)
    assert manifest["classification_counts"] == {"invalid_input": 1, "requires_classical_confirmation": 1}
    assert manifest["qpu_executed"] is False
    saved = json.loads((out / "selection.json").read_text(encoding="utf-8"))
    assert len(saved["structural_candidates"]) == 1
    assert saved["structural_candidates"][0]["case_id"] == "good"
    assert saved["structural_candidates"][0]["full_operational_model_eligible"] is False
    report = list(csv.DictReader((out / "report.csv").open(encoding="utf-8-sig")))
    assert len(report) == 2


def test_operational_saved_incumbent_is_validated_and_kept_historical(tmp_path):
    from bike_research.hardness import audit_case
    folder = tmp_path / "case"
    row = save_case(folder, "case")
    (folder / "operational_benchmark.json").write_text(json.dumps({
        "status": "optimal", "optimal": True, "selected_ids": ["a", "a"],
        "objective": 2, "upper_bound": 2, "solver_seconds": 0.1}), encoding="utf-8")
    result = audit_case(folder, row, {"structure_scan_seconds": 5})
    assert result["operational"]["incumbent_validated"] is False
    assert result["operational"]["evidence_scope"] == "historical_unhashed_sidecar"
    assert result["full_operational_model_eligible"] is False


def test_scan_budget_and_bipartite_flag_never_imply_hardness():
    from bike_research.hardness import audit_graph, verify_certificate
    g = graph(list("abcd"), [("a", "b"), ("b", "c"), ("c", "d"), ("d", "a")])
    result = audit_graph(g, scan_seconds=0)
    assert result["classification"] == "requires_classical_confirmation"
    assert result["reduction"]["scan_truncated"] is True
    assert result["metrics"]["is_bipartite"] is True
    assert result["structural_candidate_eligible"] is False
    assert verify_certificate(g, result["reduction_certificate"])


def test_unknown_request_structure_is_not_shortlist_eligible():
    from bike_research.hardness import audit_graph
    assert audit_graph(cycle_five())["structural_candidate_eligible"] is False


def test_malformed_json_document_is_recorded_without_crashing(tmp_path):
    from bike_research.hardness import audit_case
    folder = tmp_path / "case"
    row = save_case(folder, "case")
    (folder / "graph.json").write_text("[]", encoding="utf-8")
    assert audit_case(folder, row, {})["classification"] == "invalid_input"


def test_size_bin_uses_residual_component_not_raw_node_count(tmp_path):
    from select_hard_cases import run_selection
    folder = tmp_path / "cases" / "case"
    row = save_case(folder, "case")
    case = json.loads((folder / "case.json").read_text())
    g = json.loads((folder / "graph.json").read_text())
    # C5 + 고립 정점 20개: 원그래프 25개, 남는 연결 성분 5개.
    for i in range(20):
        node = {"id": f"isolated:{i}", "request_id": f"isolated:{i}"}
        case["candidates"].append(node)
        g["nodes"].append(node)
    raw = json.dumps(case).encode()
    row["input_sha256"] = hashlib.sha256(raw).hexdigest()
    g["metadata"]["case_sha256"] = row["input_sha256"]
    (folder / "case.json").write_bytes(raw)
    (folder / "graph.json").write_text(json.dumps(g), encoding="utf-8")
    summary = tmp_path / "summary.csv"
    with summary.open("w", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=list(row))
        writer.writeheader()
        writer.writerow(row)
    policy = tmp_path / "policy.json"
    policy.write_text("{}", encoding="utf-8")
    run_selection([summary], tmp_path / "audit", policy)
    result = json.loads((tmp_path / "audit/selection.json").read_text())
    assert result["structural_candidates"][0]["size_bin"] == "4-7"


def test_full_model_eligibility_requires_two_baseline_checks(tmp_path):
    from bike_research.hardness import audit_case
    folder = tmp_path / "case"
    row = save_case(folder, "case", representation="exact_declared_constraints")
    graph_path, case_path = folder / "graph.json", folder / "case.json"
    g = json.loads(graph_path.read_text())
    g["metadata"]["is_exact_for_declared_model"] = True
    graph_path.write_text(json.dumps(g), encoding="utf-8")
    assert audit_case(folder, row, {})["full_operational_model_eligible"] is True
    case = json.loads(case_path.read_text())
    case["constraints"] = [{"id": "positive_requirement", "coefficients": {"a": 1}, "lower": 1}]
    raw = json.dumps(case).encode()
    case_path.write_bytes(raw)
    row["input_sha256"] = hashlib.sha256(raw).hexdigest()
    g["metadata"]["case_sha256"] = row["input_sha256"]
    graph_path.write_text(json.dumps(g), encoding="utf-8")
    result = audit_case(folder, row, {})
    assert result["baseline_feasible"] is True
    assert result["baseline_selection_validated"] is False
    assert result["full_operational_model_eligible"] is False


def test_cross_request_policy_is_explicit_and_missing_ids_remain_incomplete():
    from bike_research.hardness import audit_graph
    g = cycle_five()
    for node in g["nodes"]:
        node["request_id"] = "same"
    result = audit_graph(g, require_cross_request_conflicts=True)
    assert result["structural_candidate_eligible"] is False
    assert result["structural_evidence_complete"] is True
    assert audit_graph(g, require_cross_request_conflicts=False)["structural_candidate_eligible"] is True
    del g["nodes"][0]["request_id"]
    result = audit_graph(g, require_cross_request_conflicts=False)
    assert result["structural_evidence_complete"] is False
    assert result["metrics"]["cross_request_edges"] is None
    assert result["structural_candidate_eligible"] is False


def test_duplicate_summary_row_never_inherits_shortlist_membership(tmp_path):
    from select_hard_cases import run_selection
    row = save_case(tmp_path / "cases/case", "case")
    summary = tmp_path / "summary.csv"
    with summary.open("w", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=list(row))
        writer.writeheader()
        writer.writerows([row, row])
    policy = tmp_path / "policy.json"
    policy.write_text("{}", encoding="utf-8")
    run_selection([summary], tmp_path / "audit", policy)
    report = list(csv.DictReader((tmp_path / "audit/report.csv").open(encoding="utf-8-sig")))
    assert report[0]["shortlisted"] == "True"
    assert report[1]["classification"] == "invalid_input"
    assert report[1]["shortlisted"] == "False"


def test_nonfinite_sidecar_and_missing_case_id_do_not_abort_batch(tmp_path):
    from select_hard_cases import run_selection
    row = save_case(tmp_path / "cases/case", "case")
    (tmp_path / "cases/case/graph_benchmark.json").write_text(
        '{"solver_seconds": NaN, "status": "optimal"}', encoding="utf-8")
    summary = tmp_path / "summary.csv"
    with summary.open("w", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=["input_sha256", "case_id"])
        writer.writeheader()
        writer.writerow(row)
        stream.write("missing_id_value\n")
    policy = tmp_path / "policy.json"
    policy.write_text("{}", encoding="utf-8")
    manifest = run_selection([summary], tmp_path / "audit", policy)
    assert manifest["classification_counts"] == {"requires_classical_confirmation": 1, "invalid_input": 1}
    report = list(csv.DictReader((tmp_path / "audit/report.csv").open(encoding="utf-8-sig")))
    assert "invalid_optional_sidecar:graph_benchmark.json" in report[0]["warnings"]
    assert report[1]["classification"] == "invalid_input"
