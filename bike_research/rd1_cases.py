"""최종 연구설계 RD-1.0에 따른 실제 여행의 대체 후보 생성.

기존 탐색용 create_case는 보존한다. 본 모듈은 요청 수나 이웃 수를 자르지
않으며, 과거 동일 OD의 관측시간 근거가 없는 대체 여행을 생성하지 않는다.
"""
from __future__ import annotations

from collections import Counter
import hashlib
import json
import math
import time

import pandas as pd

from .benefit import (GenerationBudget, GenerationLimit, attach_rd1_constraints,
                      build_window_context, candidate_deltas, individual_benefit)
from .cases import distance_m, neighbours


DEFAULT_CONFIG = {
    "radius_m": 500.0, "walking_m_s": 1.2, "max_extra_seconds": 900,
    "minimum_od_observations": 10, "benefit_filter": True,
    "time_model": "past_observed_od_median", "generation_seconds": 600.0,
    "generation_memory_bytes": 4 * 1024**3,
}


def _request_record(row):
    bike_id = getattr(row, "bike_id", None)
    record = {
        "request_id": str(row.request_id), "origin": str(row.origin),
        "destination": str(row.destination), "start": row.start.isoformat(),
        "end": row.end.isoformat(), "source_file": str(row.source_file),
        "source_row": int(row.source_row),
        "bike_id_observed": None if pd.isna(bike_id) else str(bike_id),
        "source_lineage_count": int(getattr(row, "source_lineage_count", 1)),
    }
    lineage = getattr(row, "source_lineage", None)
    if lineage is not None:
        record["source_lineage"] = lineage
    return record


def _duration_seconds(row, origin, destination, od_estimates, config, stations):
    if config["time_model"] == "past_observed_od_median":
        evidence = od_estimates.get((origin, destination))
        if not evidence or int(evidence["n"]) < config["minimum_od_observations"]:
            return None, None
        seconds = float(evidence["seconds"])
        if not math.isfinite(seconds) or not 0 < seconds <= 7200:
            return None, None
        return math.ceil(seconds), evidence
    if config["time_model"] == "observed_duration_plus_straight_distance_delta":
        old = distance_m(*stations.loc[row.origin, ["lat", "lon"]],
                         *stations.loc[row.destination, ["lat", "lon"]])
        new = distance_m(*stations.loc[origin, ["lat", "lon"]],
                         *stations.loc[destination, ["lat", "lon"]])
        seconds = max(60, math.ceil((row.end-row.start).total_seconds()+(new-old)/4.0))
        return seconds, {"sensitivity_only": True}
    raise ValueError(f"Unknown time_model: {config['time_model']}")


def create_rd1_case(trips, inventory, stations, day, hour, district, od_estimates, config=None):
    """실제 입력에서 완전한 1시간 요청 사례를 만든다.

    od_estimates[(출발, 반납)] = {seconds, n, window_days}. 수집 단계가 해당
    날짜 이전 28일/90일만 사용했음을 보장해야 한다. 입력 trips는 선정 요청뿐
    아니라 t0+5시간까지 알려진 모든 배경 끝점 사건을 포함해야 한다.

    반복 호출 시 config['_window_context']와 config['_neighbours']를 재사용할
    수 있다. 이 캐시는 모형 매개변수 해시에 포함하지 않는다. 제한에 걸리면
    metadata.status='generation_limit'이고 candidates/constraints는 비운다.
    """
    supplied = config or {}
    parameters = {**DEFAULT_CONFIG, **{k: v for k, v in supplied.items() if not k.startswith("_")}}
    if parameters["radius_m"] <= 0 or parameters["walking_m_s"] <= 0:
        raise ValueError("거리와 보행 속도는 양수여야 합니다.")
    signature = hashlib.sha256(json.dumps(parameters, sort_keys=True,
                                        ensure_ascii=False).encode()).hexdigest()[:12]
    anchor = pd.Timestamp(day).normalize() + pd.Timedelta(hours=int(hour))
    case = {
        "case_id": f"RD1_{anchor:%Y-%m-%d}_{hour:02d}_{district}_{signature}",
        "requests": [], "candidates": [], "constraints": [], "event_constraints": [],
        "metadata": {
            "design_version": "RD-1.0", "date": anchor.strftime("%Y-%m-%d"),
            "hour": int(hour), "district": district, "window_minutes": 60,
            "horizon_hours": 4, "context_hours": 5, "parameters": parameters,
            "parameter_hash": signature, "inventory_model": "observed_checkpoints_only",
            "known_capacity_constraints": 0, "snapshot_semantics_assumed": True,
            "assumptions": ["retrospective_completed_trip_requests_future_destination_known",
                            "hour_label_treated_as_instant_snapshot_UNCONFIRMED",
                            "unobserved_operator_response_held_fixed",
                            "conditional_100_percent_execution_NOT_upper_bound",
                            "no_measured_return_capacity_upper_bound",
                            "same_timestamp_departures_before_returns",
                            "inter_checkpoint_feasibility_NOT_guaranteed"],
        },
    }
    excluded = Counter()
    shared_seconds = float(supplied.get("_context_seconds", supplied.get(
        "_window_context", {}).get("construction_seconds", 0.0)))
    budget = GenerationBudget(parameters["generation_seconds"],
                              parameters["generation_memory_bytes"], shared_seconds)
    context = None
    checked_combinations = 0
    try:
        budget.check()
        context = supplied.get("_window_context")
        if context is None:
            context = build_window_context(trips, inventory, stations, anchor, budget.check)
        if context["anchor"] != anchor:
            raise ValueError("시간이 다른 window_context를 재사용할 수 없습니다.")
        common = set(context["stations"])
        area = set(stations.index[stations.district.eq(district)])
        durations = (trips.end-trips.start).dt.total_seconds()
        mask = (trips.start.ge(anchor) & trips.start.lt(anchor+pd.Timedelta(hours=1))
                & trips.origin.isin(area) & trips.valid.fillna(False)
                & durations.gt(0) & durations.le(7200))
        if "duplicate_observation" in trips:
            mask &= ~trips.duplicate_observation.fillna(False)
        selected = trips.loc[mask].sort_values(["start", "request_id"])
        case["metadata"]["eligible_requests_before_complete_stock"] = len(selected)
        has_stock = selected.origin.isin(common) & selected.destination.isin(common)
        case["metadata"]["missing_complete_stock_requests"] = int((~has_stock).sum())
        selected = selected.loc[has_stock]
        case["metadata"]["eligible_requests"] = len(selected)
        case["metadata"]["fixed_station_count"] = len(common)
        nearby = supplied.get("_neighbours")
        if nearby is None:
            nearby = neighbours(stations, radius_m=parameters["radius_m"], alternatives=None)
        for row in selected.itertuples():
            budget.check()
            case["requests"].append(_request_record(row))
            for origin, walk_start in nearby[row.origin]:
                for destination, walk_end in nearby[row.destination]:
                    checked_combinations += 1
                    if checked_combinations % 256 == 0:
                        budget.check()
                    if (origin, destination) == (row.origin, row.destination):
                        continue
                    if walk_start+walk_end > parameters["radius_m"]+1e-7:
                        excluded["total_walking_distance"] += 1
                        continue
                    if origin not in common or destination not in common:
                        excluded["missing_complete_stock"] += 1
                        continue
                    duration, evidence = _duration_seconds(
                        row, origin, destination, od_estimates, parameters, stations)
                    if duration is None:
                        excluded["insufficient_travel_evidence"] += 1
                        continue
                    start = (row.start + pd.Timedelta(
                        seconds=walk_start/parameters["walking_m_s"])).ceil("s")
                    end = start + pd.Timedelta(seconds=duration)
                    arrival = end + pd.Timedelta(seconds=math.ceil(walk_end/parameters["walking_m_s"]))
                    extra_seconds = (arrival-row.end).total_seconds()
                    if extra_seconds > parameters["max_extra_seconds"]+1e-7:
                        excluded["extra_time_limit"] += 1
                        continue
                    if end > anchor+pd.Timedelta(hours=4):
                        excluded["four_hour_end_limit"] += 1
                        continue
                    candidate = {
                        "id": "", "request_id": str(row.request_id), "origin": origin,
                        "destination": destination, "start": start.isoformat(), "end": end.isoformat(),
                        "original_origin": str(row.origin), "original_destination": str(row.destination),
                        "original_start": row.start.isoformat(), "original_end": row.end.isoformat(),
                        "extra_walk_straight_m": round(walk_start+walk_end, 6),
                        "extra_arrival_seconds": extra_seconds, "time_model": parameters["time_model"],
                        "travel_time_evidence": evidence, "source_file": str(row.source_file),
                        "source_row": int(row.source_row),
                    }
                    benefit = individual_benefit(context, candidate_deltas(candidate, anchor))
                    case["metadata"]["travel_eligible_candidates"] = case["metadata"].get("travel_eligible_candidates", 0)+1
                    if parameters["benefit_filter"] and benefit <= 0:
                        excluded["nonpositive_individual_benefit"] += 1
                        continue
                    candidate["individual_benefit"] = benefit
                    candidate["id"] = f"v{len(case['candidates']):08d}"
                    case["candidates"].append(candidate)
        budget.check()
        attach_rd1_constraints(case, context, budget.check)
        budget.check()
        if not common:
            status = "data_ineligible"
        elif case["candidates"]:
            status = "complete"
        elif parameters["benefit_filter"]:
            status = "no_beneficial_candidate"
        else:
            status = "no_travel_eligible_candidate"
        case["metadata"]["status"] = status
    except GenerationLimit as error:
        case["metadata"]["status"] = "generation_limit"
        case["metadata"]["generation_limit_reason"] = str(error)
        case["metadata"]["discarded_partial_candidate_count"] = len(case["candidates"])
        case["candidates"] = []
        case["constraints"] = []
        case["event_constraints"] = []
        case.pop("benefit_model", None)
        case.pop("inventory_audit", None)
        case.pop("event_validation", None)
    case["metadata"].update({
        "candidate_exclusions": dict(excluded), "checked_combinations": checked_combinations,
        "candidate_count": len(case["candidates"]), "generation_seconds": time.perf_counter()-budget.started,
        "shared_context_seconds": shared_seconds,
        "local_generation_seconds": time.perf_counter()-budget.local_started,
        "peak_process_memory_bytes": budget.peak_bytes,
        "memory_guard_available": budget.process is not None,
    })
    return case
