"""공통 수선·동일 그래프 난수 기준선·날짜 블록 통계의 작은 수학 검증."""
import pandas as pd
import pytest

from bike_research.evaluation import (common_graph_repair, random_graph_baseline,
                                      paired_date_bootstrap, event_path_sensitivity)


def graph_and_case():
    candidates = [{"id": "a", "request_id": "r1"}, {"id": "b", "request_id": "r1"},
                  {"id": "c", "request_id": "r2"}]
    graph = {"nodes": candidates, "edges": [{"source": "a", "target": "b"},
                                              {"source": "b", "target": "c"}],
             "metadata": {"is_exact_for_declared_model": True}}
    case = {"candidates": candidates, "constraints": [], "resource_conflicts": [{"source": "b", "target": "c"}]}
    return graph, case


def test_repair_removes_highest_conflict_degree_then_adds_by_id():
    graph, _ = graph_and_case()
    assert common_graph_repair(graph, ["a", "b", "c"]) == ["a", "c"]
    assert common_graph_repair(graph, []) == ["a", "c"]
    with pytest.raises(ValueError):
        common_graph_repair(graph, ["absent"])


def test_random_baseline_preserves_all_samples_and_is_deterministic():
    graph, case = graph_and_case()
    first = random_graph_baseline(graph, case, shots=128, seed=20261109)
    second = random_graph_baseline(graph, case, shots=128, seed=20261109)
    assert first["shots"] == 128
    assert first["raw"]["valid_samples"] < 128
    assert first["repaired"]["valid_samples"] == 128
    assert first["raw"]["valid_samples"] == second["raw"]["valid_samples"]
    assert first["repaired"]["best_candidate_count"] == 2
    assert first["repaired"]["best_candidate_ids"] == ["a", "c"]
    assert first["random_bit_probability"] == .5


def test_random_baseline_does_not_claim_relaxation_is_exact():
    graph, case = graph_and_case()
    graph["metadata"]["is_exact_for_declared_model"] = False
    with pytest.raises(ValueError):
        random_graph_baseline(graph, case)


def test_auxiliary_graph_uses_decoded_recommendations_not_node_offset():
    from bike_research.encoding import encode_operational_mis
    _, case = graph_and_case()
    graph = encode_operational_mis(case)
    result = random_graph_baseline(graph, case, shots=64)
    for variant in ("raw", "repaired"):
        count = result[variant]["best_candidate_count"]
        assert count is None or 0 <= count <= 2


def test_paired_dates_resample_blocks_and_short_series_is_descriptive():
    frame = pd.DataFrame({"date": [f"2025-01-{d:02}" for d in range(1, 11)],
                          "left": range(10), "right": range(2, 12)})
    result = paired_date_bootstrap(frame, "left", "right")
    assert result["median_paired_difference"] == 2
    assert result["ci95"] == [2., 2.]
    assert result["bootstrap_repeats"] == 2000
    short = paired_date_bootstrap(frame.head(9), "left", "right")
    assert short["ci95"] is None
    assert short["status"] == "descriptive_only_fewer_than_10_dates"


def test_return_order_and_processing_delay_are_explicit_event_sensitivities():
    from bike_research.benefit import build_window_context
    anchor = pd.Timestamp("2025-06-05 09:00")
    stations = pd.DataFrame([{"station": "A", "lat": 37.5, "lon": 127.0, "district": "구"}]).set_index("station")
    inventory = pd.DataFrame([{"station": "A", "time": anchor+pd.Timedelta(hours=h), "inventory": 0}
                              for h in range(5)])
    trips = pd.DataFrame([{"origin": "A", "destination": "A",
                           "start": anchor+pd.Timedelta(minutes=1),
                           "end": anchor+pd.Timedelta(minutes=1)}])
    context = build_window_context(trips, inventory, stations, anchor)
    case = {"candidates": [], "constraints": []}
    primary = event_path_sensitivity(context, case, [])
    reordered = event_path_sensitivity(context, case, [], returns_first=True)
    assert not primary["event_feasible"]
    assert reordered["event_feasible"]
    assert reordered["strict_event_subgroup"]
    delayed = event_path_sensitivity(context, case, [], returns_first=True, return_delay_seconds=60)
    assert not delayed["event_feasible"]
    assert delayed["return_delay_seconds"] == 60
