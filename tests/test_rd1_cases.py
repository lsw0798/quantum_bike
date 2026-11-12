"""RD-1.0 수식과 경계 조건 검사. 연구 성능 표본으로 사용하지 않는 단위 테스트."""
import pandas as pd
import pytest

from bike_research.benefit import build_window_context, candidate_deltas, evaluate_selection
from bike_research.rd1_cases import create_rd1_case


def inputs():
    anchor = pd.Timestamp("2025-06-05 09:00")
    stations = pd.DataFrame([
        ("A", 37.50, 127.00, "구"), ("B", 37.501, 127.00, "구"),
        ("C", 37.502, 127.00, "구"), ("E", 37.503, 127.00, "구"),
        ("D", 37.55, 127.05, "다른구"),
    ], columns=["station", "lat", "lon", "district"]).set_index("station")
    rows = [("A", "D", 10, 20, "original"), ("A", "D", 65, 75, "future")]
    trips = pd.DataFrame([dict(origin=o, destination=d,
                              start=anchor+pd.Timedelta(minutes=s),
                              end=anchor+pd.Timedelta(minutes=e), request_id=r,
                              valid=True, source_file="observed.csv", source_row=i,
                              bike_id=f"b{i}") for i, (o,d,s,e,r) in enumerate(rows)])
    inventory = pd.DataFrame([dict(station=s, time=anchor+pd.Timedelta(hours=h),
                                   inventory=(0 if h>0 else 1) if s=="A" else 3)
                              for s in stations.index for h in range(6)])
    od = {(s, "D"): {"seconds": 600, "n": 10, "window_days": 28}
          for s in ["B", "C", "E"]}
    return anchor, trips, inventory, stations, od


def test_reserve_is_maximum_prefix_not_net_flow_and_departures_first():
    anchor, trips, inv, stations, _ = inputs()
    # 동일 시각 대여/반납이면 순유출 0이어도 초기 자전거 1대 필요.
    trips.loc[1, "destination"] = "A"
    trips.loc[1, "end"] = trips.loc[1, "start"]
    context = build_window_context(trips, inv, stations, anchor)
    assert context["required_reserve"]["A"][0] == 1
    assert context["baseline_j"] == 1


def test_checkpoint_uses_strictly_before_and_cancels_original_trip():
    anchor, *_ = inputs()
    candidate = dict(original_origin="A", original_destination="D",
                     original_start=anchor.isoformat(),
                     original_end=(anchor+pd.Timedelta(hours=1)).isoformat(),
                     origin="B", destination="D", start=anchor.isoformat(),
                     end=(anchor+pd.Timedelta(hours=1, minutes=1)).isoformat())
    deltas = candidate_deltas(candidate, anchor)
    assert deltas[("A", 1)] == 1
    assert deltas[("B", 1)] == -1
    assert ("D", 1) not in deltas  # 정시 반납은 해당 관측 뒤에 발생.
    assert ("D", 2) not in deltas  # 두 반납이 상쇄.


def test_all_neighbours_and_positive_benefit_are_kept():
    anchor, trips, inv, stations, od = inputs()
    case = create_rd1_case(trips, inv, stations, "2025-06-05", 9, "구", od)
    assert len(case["requests"]) == 1
    assert {v["origin"] for v in case["candidates"]} == {"B", "C", "E"}
    assert all(v["individual_benefit"] == 1 for v in case["candidates"])
    chosen = [case["candidates"][0]["id"]]
    result = evaluate_selection(case, chosen)
    assert result["checkpoint_feasible"]
    assert result["benefit"] == 1
    assert result["selected_count"] == 1
    assert not result["event_baseline_feasible"]
    assert not result["strict_event_subgroup"]


def test_unknown_od_never_uses_distance_time_fallback():
    _, trips, inv, stations, _ = inputs()
    case = create_rd1_case(trips, inv, stations, "2025-06-05", 9, "구", {})
    assert not case["candidates"]
    assert case["metadata"]["candidate_exclusions"]["insufficient_travel_evidence"] == 3


def test_zero_benefit_filter_off_is_an_explicit_separate_case():
    _, trips, inv, stations, od = inputs()
    inv["inventory"] = 10
    filtered = create_rd1_case(trips, inv, stations, "2025-06-05", 9, "구", od)
    all_candidates = create_rd1_case(trips, inv, stations, "2025-06-05", 9, "구", od,
                                    {"benefit_filter": False})
    assert filtered["metadata"]["status"] == "no_beneficial_candidate"
    assert not filtered["candidates"]
    assert len(all_candidates["candidates"]) == 3
    assert all_candidates["case_id"] != filtered["case_id"]
    assert filtered["benefit_model"]["stations"] == all_candidates["benefit_model"]["stations"]


def test_missing_one_checkpoint_removes_station_before_candidates():
    _, trips, inv, stations, od = inputs()
    inv = inv.loc[~((inv.station=="B") & (inv.time.dt.hour==11))]
    case = create_rd1_case(trips, inv, stations, "2025-06-05", 9, "구", od)
    assert "B" not in case["benefit_model"]["stations"]
    assert {v["origin"] for v in case["candidates"]} == {"C", "E"}


def test_generation_limit_never_returns_partial_graph_input():
    _, trips, inv, stations, od = inputs()
    case = create_rd1_case(trips, inv, stations, "2025-06-05", 9, "구", od,
                           {"generation_seconds": 0})
    assert case["metadata"]["status"] == "generation_limit"
    assert not case["candidates"]
    assert not case["constraints"]


def test_both_walking_legs_share_one_total_budget():
    _, trips, inv, stations, od = inputs()
    stations.loc["E", ["lat", "lon"]] = [37.5536, 127.05]
    stations.loc["B", "lat"] = 37.5036
    od[("B", "E")] = {"seconds": 600, "n": 10, "window_days": 28}
    case = create_rd1_case(trips, inv, stations, "2025-06-05", 9, "구", od)
    assert not any(v["origin"]=="B" and v["destination"]=="E" for v in case["candidates"])
    assert case["metadata"]["candidate_exclusions"]["total_walking_distance"] > 0


def test_conflicting_or_invalid_duplicate_stock_is_not_silently_selected():
    anchor, trips, inv, stations, _ = inputs()
    duplicate = inv.loc[(inv.station=="B") & (inv.time==anchor)].copy()
    duplicate["inventory"] = -1
    context = build_window_context(trips, pd.concat([inv, duplicate]), stations, anchor)
    assert "B" not in context["stations"]


def test_rounding_applies_to_timestamp_after_walking():
    from bike_research.cases import distance_m
    _, trips, inv, stations, od = inputs()
    trips.loc[0, "start"] += pd.Timedelta(milliseconds=200)
    case = create_rd1_case(trips, inv, stations, "2025-06-05", 9, "구", od)
    candidate = next(v for v in case["candidates"] if v["origin"]=="B")
    walk = distance_m(37.50, 127.00, 37.501, 127.00)
    expected = (trips.loc[0, "start"]+pd.Timedelta(seconds=walk/1.2)).ceil("s")
    assert pd.Timestamp(candidate["start"]) == expected


def test_shared_context_cost_is_charged_to_the_same_generation_budget():
    anchor, trips, inv, stations, od = inputs()
    context = build_window_context(trips, inv, stations, anchor)
    case = create_rd1_case(trips, inv, stations, "2025-06-05", 9, "구", od,
        {"_window_context": context, "_context_seconds": 2.0, "generation_seconds": 1.0})
    assert case["metadata"]["status"] == "generation_limit"
    assert case["metadata"]["shared_context_seconds"] == 2.0
    assert case["metadata"]["generation_seconds"] >= 2.0


def test_sparse_event_storage_matches_direct_path_for_every_selection():
    """작은 입력의 모든 선택을 별도 직접 사건 재생과 대조한다."""
    from itertools import combinations
    from collections import defaultdict
    from bike_research.benefit import candidate_events
    from bike_research.graph import validate_selection
    anchor, trips, inv, stations, od = inputs()
    context = build_window_context(trips, inv, stations, anchor)
    case = create_rd1_case(trips, inv, stations, "2025-06-05", 9, "구", od,
                           {"_window_context": context})
    assert case["event_validation"]["representation"] == "sparse_event_replay"
    assert case["event_constraints"] == []
    assert case["metadata"]["event_constraints_materialized"] is False
    candidates = case["candidates"]
    for n in range(len(candidates)+1):
        for subset in combinations(candidates, n):
            ids = [v["id"] for v in subset]
            expected = validate_selection({**case, "constraints": []}, ids)["feasible"]
            for station in context["stations"]:
                events = defaultdict(int, dict(context["background_events"][station]))
                for candidate in subset:
                    for sid, when, phase, value in candidate_events(candidate):
                        if sid == station:
                            events[(when, phase)] += value
                stock = context["observed"][station][0]
                expected &= stock >= 0
                for (when, _), value in sorted(events.items()):
                    if when <= anchor+pd.Timedelta(hours=4):
                        stock += value
                        expected &= stock >= 0
            assert evaluate_selection(case, ids)["event_feasible"] == expected


def test_sparse_event_storage_does_not_expand_cumulative_candidate_columns():
    import copy
    import json
    from bike_research.benefit import attach_rd1_constraints
    anchor, trips, inv, stations, od = inputs()
    context = build_window_context(trips, inv, stations, anchor)
    case = create_rd1_case(trips, inv, stations, "2025-06-05", 9, "구", od,
                           {"_window_context": context})
    expanded = copy.deepcopy(case)
    for copy_id in range(20):
        for candidate in case["candidates"]:
            expanded["candidates"].append({**candidate, "id": f"copy{copy_id}_{candidate['id']}"})
    attach_rd1_constraints(expanded, context)
    assert json.dumps(expanded["event_validation"], sort_keys=True) == json.dumps(case["event_validation"], sort_keys=True)
    assert expanded["event_constraints"] == []


def test_sparse_event_check_preserves_untouched_station_failure_at_horizon():
    anchor, trips, inv, stations, od = inputs()
    stations.loc["Z"] = [37.7, 127.2, "다른구"]
    inv = pd.concat([inv, pd.DataFrame([dict(station="Z", time=anchor+pd.Timedelta(hours=h), inventory=0)
                                       for h in range(6)])], ignore_index=True)
    extra = dict(origin="Z", destination="D", start=anchor+pd.Timedelta(hours=4),
                 end=anchor+pd.Timedelta(hours=4, minutes=10), request_id="outside",
                 valid=True, source_file="observed.csv", source_row=99, bike_id="b99")
    trips = pd.concat([trips, pd.DataFrame([extra])], ignore_index=True)
    case = create_rd1_case(trips, inv, stations, "2025-06-05", 9, "구", od)
    assert "Z" not in case["event_validation"]["initial_inventory"]
    assert case["event_validation"]["unchanged_stations_baseline_min"] == -1
    result = evaluate_selection(case, [case["candidates"][0]["id"]])
    assert not result["event_feasible"]
    assert any(v.get("station")=="unchanged_stations" for v in result["event_violations"])


def test_empty_materialized_rows_are_never_treated_as_event_certification():
    _, trips, inv, stations, od = inputs()
    case = create_rd1_case(trips, inv, stations, "2025-06-05", 9, "구", od)
    del case["event_validation"]
    with pytest.raises(ValueError, match="사건 검사 표현"):
        evaluate_selection(case, [])
