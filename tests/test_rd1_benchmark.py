"""RD-1.0 고전 비교의 단위와 인증 경로를 검사하는 작은 수학 테스트."""
from itertools import product

import pytest

from bike_research.benchmark import (
    benchmark_rd1_case, build_rd1_graphs, classify_hardness,
    solve_highs, solve_cpsat_graph,
)
from bike_research.encoding import decode_operational_mis, lift_operational_selection
from bike_research.graph import validate_selection


def source():
    return {"case_id": "unit-test-only", "candidates": [
        {"id": x, "request_id": x} for x in "abc"],
        "constraints": [{"id": "capacity-two", "coefficients": dict.fromkeys("abc", 1),
                         "lower": None, "upper": 2}], "metadata": {}}


def test_exact_auxiliary_path_preserves_all_assignments(tmp_path):
    case = source()
    built = build_rd1_graphs(case, tmp_path)
    assert built["status"] == "ready"
    graph = built["exact_graph"]
    assert graph["encoding_map"]["objective_offset"] == 4
    assert built["audit"]["representation"] == "exact_operational_mis"
    assert built["audit"]["candidate_only_gates_applied"] is False
    for bits in product((0, 1), repeat=3):
        chosen = [x for x, bit in zip("abc", bits) if bit]
        if validate_selection(case, chosen)["feasible"]:
            decoded = decode_operational_mis(graph, lift_operational_selection(graph, chosen))
            assert decoded["operational_feasible"]
            assert decoded["selected_candidate_ids"] == chosen


def test_auxiliary_bounds_are_converted_to_recommendations(tmp_path):
    case = source()
    built = build_rd1_graphs(case, tmp_path)
    result = benchmark_rd1_case(case, built["exact_graph"], tmp_path,
                               {"seeds": [7], "time_limit_seconds": 2}, built["audit"])
    assert result["lower_bound"] == result["upper_bound"] == 2
    assert result["optimality_proven"]
    assert result["relative_gap"] == 0
    assert {r["algorithm"] for r in result["runs"]} >= {"highs_graph", "cpsat_graph", "highs_operational"}
    for run in result["runs"]:
        if run["status"] == "optimal":
            assert run["recommendation_count"] == 2
            if run["algorithm"] != "highs_operational":
                assert run["raw_graph_objective"] == 6


def test_invalid_baseline_is_not_claimed_to_have_equivalence(tmp_path):
    case = source()
    case["constraints"][0].update(lower=1, upper=2)
    built = build_rd1_graphs(case, tmp_path)
    assert built["status"] == "source_model_invalid"
    assert built["exact_graph"] is None


def test_encoding_limit_does_not_return_partial_main_graph(tmp_path):
    built = build_rd1_graphs(source(), tmp_path, {"encoding_max_nodes": 3})
    assert built["status"] == "encoding_limit_exceeded"
    assert built["exact_graph"] is None
    assert (tmp_path / "graph.json").exists()


def test_encoding_failure_still_has_source_model_measurements(tmp_path):
    case = source()
    result = benchmark_rd1_case(case, None, tmp_path,
                               {"seeds": [8], "time_limit_seconds": 2})
    assert result["lower_bound"] == result["upper_bound"] == 2
    assert result["optimality_proven"]
    assert result["missing_exact_graph_comparison"]
    assert not result["all_required_exact_runs_normal"]
    assert [r["algorithm"] for r in result["runs"]] == ["highs_operational"]


def test_encoding_wall_clock_limit_is_explicit():
    from bike_research.encoding import EncodingLimitError, encode_operational_mis
    with pytest.raises(EncodingLimitError, match="wall-clock"):
        encode_operational_mis(source(), max_seconds=1e-12)


def test_auxiliary_residual_does_not_require_request_ids(tmp_path):
    built = build_rd1_graphs(source(), tmp_path, {"structure_scan_seconds": 0})
    assert built["audit"]["residual_size_bin"] != "missing_request_ids"


def test_solver_infeasibility_and_exact_integer_result():
    row = {"coefficients": {"x": 1}, "lower": 2, "upper": None}
    result = solve_highs(["x"], [row], 1, 3)
    assert result["status"] == "infeasible"
    assert result["objective"] is None
    graph = {"nodes": [{"id": "a"}, {"id": "b"}],
             "edges": [{"source": "a", "target": "b"}]}
    result = solve_cpsat_graph(graph, 1, 3)
    assert result["objective"] == result["upper_bound"] == 1


def test_hardness_requires_complete_runs_and_same_unit_gap():
    rows = [{"case_id": str(i), "residual_size_bin": "32-63",
             "optimality_proven": True, "fastest_exact_median_seconds": i / 10,
             "all_required_exact_runs_normal": True,
             "all_required_exact_runs_unproven": False, "relative_gap": 0}
            for i in range(20)]
    rows.append({"case_id": "failed", "residual_size_bin": "64-127",
                 "optimality_proven": False, "relative_gap": 0.2,
                 "all_required_exact_runs_normal": False,
                 "all_required_exact_runs_unproven": False})
    labels = classify_hardness(rows)
    assert sum(row["hardness_class"] == "relative_hard" for row in labels) == 2
    assert labels[-1]["hardness_class"] == "incomplete_benchmark"


def test_structural_certificate_is_retained_as_exact_bound(tmp_path, monkeypatch):
    import bike_research.benchmark as module
    case = {"case_id": "unit-test-only", "candidates": [
        {"id": "a", "request_id": "same"}, {"id": "b", "request_id": "same"}],
        "constraints": []}
    built = build_rd1_graphs(case, tmp_path)
    assert built["audit"]["certificate_verified"]
    def no_incumbent(*args, **kwargs):
        return {"status": "limit_reached", "optimal": False, "objective": None,
                "selected_ids": [], "upper_bound": 2, "solver_seconds": 1,
                "model_seconds": 0, "seed": 7, "time_limit_seconds": 1}
    monkeypatch.setattr(module, "solve_highs", no_incumbent)
    monkeypatch.setattr(module, "solve_cpsat_graph", no_incumbent)
    result = benchmark_rd1_case(case, built["exact_graph"], tmp_path,
                               {"seeds": [7], "time_limit_seconds": 1}, built["audit"])
    assert result["lower_bound"] == result["upper_bound"] == 1
    assert result["optimality_proven"]


def test_case_binding_rejects_changed_input(tmp_path):
    case = source()
    built = build_rd1_graphs(case, tmp_path)
    case["constraints"][0]["upper"] = 3
    with pytest.raises(ValueError, match="bound"):
        benchmark_rd1_case(case, built["exact_graph"], tmp_path)


def test_redumis_adapter_validates_returned_labels(tmp_path, monkeypatch):
    import bike_research.benchmark as module
    from pathlib import Path
    from types import SimpleNamespace
    graph = {"nodes": [{"id": "a"}, {"id": "b"}],
             "edges": [{"source": "a", "target": "b"}]}
    def invalid_labels(command, **kwargs):
        path = next(arg.split("=", 1)[1] for arg in command if arg.startswith("--output="))
        Path(path).write_text("1\n1\n", encoding="ascii")
        assert kwargs["env"]["OMP_NUM_THREADS"] == "1"
        return SimpleNamespace(returncode=0, stdout="", stderr="")
    monkeypatch.setattr(module.subprocess, "run", invalid_labels)
    with pytest.raises(ValueError, match="non-independent"):
        module.solve_redumis_graph(graph, "mock-executable", tmp_path)


def test_sparse_greedy_matches_full_validation_oracle():
    """혼합 부호·요청 배타·자원 충돌에서도 기존 전체 검사와 같은 선택을 한다."""
    import random
    from bike_research.benchmark import greedy_operational
    rng = random.Random(20261109)
    for _ in range(100):
        candidates = [{"id": f"v{i}", "request_id": f"r{i // 3}",
                       "individual_benefit": rng.randrange(4)} for i in range(15)]
        rows = [{"id": f"row{j}",
                 "coefficients": {c["id"]: rng.randrange(-2, 3) for c in candidates
                                  if rng.random() < 0.3},
                 "lower": -rng.randrange(4), "upper": rng.randrange(4)}
                for j in range(12)]
        case = {"candidates": candidates, "constraints": rows,
                "resource_conflicts": [{"source": "v1", "target": "v8"}]}
        for by_benefit in (False, True):
            order = sorted(candidates, key=lambda c: (-c["individual_benefit"], c["id"])) if by_benefit else sorted(candidates, key=lambda c: c["id"])
            expected = []
            for c in order:
                if validate_selection(case, expected + [c["id"]])["feasible"]:
                    expected.append(c["id"])
            assert greedy_operational(case, by_benefit) == sorted(expected)


def test_sparse_greedy_does_not_rescan_full_model_per_candidate(monkeypatch):
    import bike_research.benchmark as module
    case = {"candidates": [{"id": str(i), "request_id": str(i)} for i in range(100)],
            "constraints": [{"id": str(i), "coefficients": {str(i): -1},
                             "lower": -1, "upper": None} for i in range(100)]}
    calls = 0
    original = module.validate_selection
    def count(*args, **kwargs):
        nonlocal calls
        calls += 1
        return original(*args, **kwargs)
    monkeypatch.setattr(module, "validate_selection", count)
    assert len(module.greedy_operational(case)) == 100
    assert calls <= 1


def test_sparse_greedy_rejects_infeasible_zero_baseline():
    from bike_research.benchmark import greedy_operational
    case = {"candidates": [{"id": "supply", "request_id": "r"}],
            "constraints": [{"id": "invalid-zero", "coefficients": {"supply": 1},
                             "lower": 1, "upper": None}]}
    assert greedy_operational(case) == []


def test_request_clique_budget_rejects_before_pair_enumeration(monkeypatch):
    import bike_research.graph as module
    case = {"candidates": [{"id": f"v{i}", "request_id": "one-request"}
                           for i in range(1001)], "constraints": []}
    def must_not_enumerate(*args, **kwargs):
        raise AssertionError("An over-budget clique must be rejected before enumeration")
    monkeypatch.setattr(module, "combinations", must_not_enumerate)
    with pytest.raises(ValueError, match="mandatory request cliques"):
        module.build_graph(case, max_edges=500_000)


def test_false_twin_reduction_preserves_all_small_graph_optima():
    """5정점 이하 모든 단순 그래프에서 원래 MIS와 축약 MWIS 값을 전수 대조한다."""
    from itertools import combinations
    from bike_research.benchmark import collapse_false_twins
    for n in range(6):
        ids = [str(i) for i in range(n)]
        pairs = list(combinations(ids, 2))
        for edge_bits in product((0, 1), repeat=len(pairs)):
            edges = [pair for pair, bit in zip(pairs, edge_bits) if bit]
            graph = {"nodes": [{"id": v} for v in ids],
                     "edges": [{"source": a, "target": b} for a, b in edges]}
            reduced = collapse_false_twins(graph)
            best_original = 0
            for bits in product((0, 1), repeat=n):
                chosen = {v for v, bit in zip(ids, bits) if bit}
                if not any(a in chosen and b in chosen for a, b in edges):
                    best_original = max(best_original, len(chosen))
            nodes = reduced["graph"]["nodes"]
            best_reduced = 0
            for bits in product((0, 1), repeat=len(nodes)):
                chosen = {v["id"] for v, bit in zip(nodes, bits) if bit}
                if not any(e["source"] in chosen and e["target"] in chosen
                           for e in reduced["graph"]["edges"]):
                    best_reduced = max(best_reduced, sum(reduced["weights"][v] for v in chosen))
            assert best_original == best_reduced


def test_twin_graph_solvers_expand_original_ids_and_weighted_bounds():
    from bike_research.benchmark import solve_highs_graph, solve_cpsat_graph
    # a,b,c는 같은 열린 이웃집합 {d}를 갖는다. 최적은 세 정점 전부다.
    graph = {"nodes": [{"id": v} for v in "abcd"],
             "edges": [{"source": v, "target": "d"} for v in "abc"]}
    for solve in (solve_highs_graph, solve_cpsat_graph):
        result = solve(graph, 2, 7)
        assert result["objective"] == result["upper_bound"] == 3
        assert result["selected_ids"] == ["a", "b", "c"]
        assert result["preprocessing"]["before_nodes"] == 4
        assert result["preprocessing"]["after_nodes"] == 2
        assert result["unweighted_original"] is True
        assert not result["budget_overrun"]


def test_budget_overrun_solution_is_not_used_for_fixed_budget(tmp_path, monkeypatch):
    import bike_research.benchmark as module
    case = source()
    def overrun(*args, **kwargs):
        return {"status": "optimal", "optimal": True, "objective": 2,
                "selected_ids": ["a", "b"], "upper_bound": 2,
                "solver_seconds": 131, "model_seconds": 0, "seed": 7,
                "budget_overrun": True, "proof_seconds": 131}
    monkeypatch.setattr(module, "solve_highs", overrun)
    result = benchmark_rd1_case(case, None, tmp_path,
                               {"seeds": [7], "time_limit_seconds": 60})
    assert result["lower_bound"] == 0
    assert result["upper_bound"] == 3
    assert not result["optimality_proven"]
    assert not result["all_required_exact_runs_normal"]
    assert result["runs"][0]["recommendation_count"] == 2
