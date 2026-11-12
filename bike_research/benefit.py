"""RD-1.0의 고정 관측 기준 재고 부족 지표와 사건 경로 검사.

J는 실제 실패 요청 수가 아니다. 동일 관측점의 실제 재고 B에 후보의
사건 차이만 더하고, 고정된 원래 이동을 지원할 초기 보유량 L과 비교한다.
이 모듈은 QPU 및 외부 서비스에 접근하지 않는다.
"""
from __future__ import annotations

from collections import defaultdict
import time

import numpy as np
import pandas as pd


class GenerationLimit(RuntimeError):
    """완료되지 않은 사례는 최적화 입력으로 사용하지 않는다."""


class GenerationBudget:
    """공유 문맥 시간을 포함하는 경과시간과 현재 프로세스 RSS 예산."""
    def __init__(self, seconds=600.0, memory_bytes=4*1024**3, charged_seconds=0.0):
        self.local_started = time.perf_counter()
        self.charged_seconds = max(0.0, float(charged_seconds))
        self.started = self.local_started-self.charged_seconds
        self.seconds = float(seconds)
        self.bytes = int(memory_bytes)
        self.peak_bytes = 0
        try:
            import psutil
            self.process = psutil.Process()
        except ImportError:
            self.process = None

    def check(self):
        if time.perf_counter()-self.started >= self.seconds:
            raise GenerationLimit("candidate_generation_wall_time_limit")
        if self.process is None:
            raise GenerationLimit("candidate_generation_memory_guard_unavailable")
        self.peak_bytes = max(self.peak_bytes, self.process.memory_info().rss)
        if self.peak_bytes > self.bytes:
            raise GenerationLimit("candidate_generation_process_memory_limit")


def candidate_events(candidate):
    """원래 사건 취소와 변경 사건 등록: 출발 단계 0, 반납 단계 1."""
    for station, key, phase, change in [
        (candidate["original_origin"], "original_start", 0, 1),
        (candidate["original_destination"], "original_end", 1, -1),
        (candidate["origin"], "start", 0, -1),
        (candidate["destination"], "end", 1, 1),
    ]:
        yield str(station), pd.Timestamp(candidate[key]), phase, change


def candidate_deltas(candidate, anchor):
    """다섯 관측 직전의 정수 재고 변화 {(대여소, 시간 인덱스): 변화}."""
    anchor = pd.Timestamp(anchor)
    delta = defaultdict(int)
    for station, when, _, change in candidate_events(candidate):
        for hour in range(5):
            if when < anchor + pd.Timedelta(hours=hour):
                delta[(station, hour)] += change
    return {key: value for key, value in delta.items() if value}


def _clean_background(trips):
    """유효 요청이 아닌 행도 알려진 한쪽 사건을 배경에 남긴다."""
    if "duplicate_observation" in trips:
        return trips.loc[~trips.duplicate_observation.fillna(False)]
    return trips


def build_window_context(trips, inventory, stations, anchor, check_budget=None):
    """같은 정시의 모든 자치구가 재사용하는 S, B, L, 배경 사건.

    inventory는 정시 직전의 순간 수량이라는 미확인 가정으로 해석한다.
    요청/편익 필터 전 S를 고정하며 다섯 관측 중 하나라도 없거나 충돌하면
    해당 대여소를 S에서 뺀다. 입력 날짜의 완전성은 수집 단계가 기록한다.
    """
    construction_started = time.perf_counter()
    anchor = pd.Timestamp(anchor)
    # 직접 재사용 문맥을 만드는 호출도 예산 밖으로 빠져나가지 않는다.
    own_budget = GenerationBudget() if check_budget is None else None
    check_budget = own_budget.check if own_budget else check_budget
    check_budget()
    checkpoints = [anchor + pd.Timedelta(hours=i) for i in range(5)]
    station_ids = set(map(str, stations.index))
    stock = inventory.loc[inventory.time.isin(checkpoints)
                          & inventory.station.isin(station_ids)].copy()
    valid = (stock.inventory.notna() & np.isfinite(stock.inventory)
             & stock.inventory.ge(0) & stock.inventory.eq(np.floor(stock.inventory)))
    # 충돌 중복은 한 행을 골라 살리지 않는다. 상위 정제 단계와 같은 정책.
    invalid_keys = set(zip(stock.loc[~valid, "station"], stock.loc[~valid, "time"]))
    stock = stock.loc[valid]
    counts = stock.groupby(["station", "time"]).inventory.nunique()
    conflicts = set(counts.index[counts.gt(1)]) | invalid_keys
    stock = stock.drop_duplicates(["station", "time", "inventory"])
    if conflicts:
        stock = stock.loc[[key not in conflicts for key in zip(stock.station, stock.time)]]
    table = stock.pivot(index="station", columns="time", values="inventory")
    table = table.reindex(columns=checkpoints).dropna()
    common = sorted(map(str, table.index))
    observed = {s: [int(v) for v in table.loc[s].tolist()] for s in common}
    common_set = set(common)
    background = _clean_background(trips)

    # 동일 시각은 모든 출발 후 모든 반납. 단계별 집계로 최솟값을 보존한다.
    events = defaultdict(lambda: defaultdict(int))
    context_end = anchor + pd.Timedelta(hours=5)
    for time_col, station_col, phase, sign in [
        ("start", "origin", 0, -1), ("end", "destination", 1, 1)
    ]:
        check_budget()
        known = background.loc[background[station_col].isin(common_set)
                               & background[time_col].ge(anchor)
                               & background[time_col].lt(context_end)]
        grouped = known.groupby([station_col, time_col], sort=False).size()
        for (station, when), count in grouped.items():
            events[str(station)][(pd.Timestamp(when), phase)] += sign * int(count)
    ordered = {s: sorted(events[s].items()) for s in common}

    required, audits = {}, {}
    horizon = anchor + pd.Timedelta(hours=4)
    for station in common:
        check_budget()
        sequence = ordered[station]
        reserves = []
        for hour in range(1, 5):
            left = anchor + pd.Timedelta(hours=hour)
            right = left + pd.Timedelta(hours=1)
            balance = minimum = 0
            for (when, _), change in sequence:
                if left <= when < right:
                    balance += change
                    minimum = min(minimum, balance)
            reserves.append(-minimum)
        required[station] = reserves

        balance = observed[station][0]
        minimum = balance
        for (when, _), change in sequence:
            if when <= horizon:
                balance += change
                minimum = min(minimum, balance)
        residuals = []
        for hour in range(1, 5):
            predicted = observed[station][0] + sum(
                change for (when, _), change in sequence if when < checkpoints[hour])
            residuals.append(observed[station][hour] - predicted)
        audits[station] = {"baseline_min": minimum, "residuals": residuals}

    baseline_j = sum(max(0, required[s][h-1] - observed[s][h])
                     for s in common for h in range(1, 5))
    missing_start = int((background.origin.isna() | background.start.isna()).sum())
    missing_end = int((background.destination.isna() | background.end.isna()).sum())
    check_budget()
    return {
        "anchor": anchor, "stations": common, "observed": observed,
        "required_reserve": required, "baseline_j": int(baseline_j),
        "background_events": ordered, "station_audits": audits,
        "missing_start_events_in_loaded_data": missing_start,
        "missing_return_events_in_loaded_data": missing_end,
        "snapshot_semantics_assumed": True,
        "construction_seconds": time.perf_counter()-construction_started,
        "peak_process_memory_bytes": own_budget.peak_bytes if own_budget else None,
    }


def individual_benefit(context, deltas):
    """전체 J를 매번 순회하지 않고 변화가 있는 관측점만 차감한다."""
    benefit = 0
    for (station, hour), delta in deltas.items():
        if hour == 0:
            continue
        shortfall = context["required_reserve"][station][hour-1] - context["observed"][station][hour]
        benefit += max(0, shortfall) - max(0, shortfall-delta)
    return int(benefit)


def attach_rd1_constraints(case, context, check_budget=None):
    """관측점 제약과 사건 부검증의 희소 사건 목록을 각각 저장한다.

    S의 변하지 않는 재고는 검증된 비음수 상수이므로 주 모형에 반복 저장하지
    않는다. 사건 부검증은 누적 후보 계수 행렬을 전개하지 않고 배경 사건과
    선택 후보의 네 사건을 순서대로 재생한다. 후보가 닿지 않는 대여소의 음수도
    그대로 보존한다. 저장량은 배경 사건 수와 후보 수에 선형으로 증가한다.
    """
    check_budget = check_budget or (lambda: None)
    anchor = context["anchor"]
    candidates = case["candidates"]
    checkpoint_coefficients = defaultdict(dict)
    involved = set()
    effects = {}
    for candidate in candidates:
        check_budget()
        identifier = candidate["id"]
        deltas = candidate_deltas(candidate, anchor)
        effects[identifier] = [[s, h, value] for (s, h), value in sorted(deltas.items())]
        for key, value in deltas.items():
            checkpoint_coefficients[key][identifier] = value
        for station, when, phase, value in candidate_events(candidate):
            involved.add(station)

    constraints = []
    for station in sorted(involved):
        check_budget()
        for hour in range(5):
            constraints.append({
                "id": f"checkpoint:{station}:{hour}", "kind": "observed_inventory_lower",
                "station": station, "time": (anchor+pd.Timedelta(hours=hour)).isoformat(),
                "coefficients": checkpoint_coefficients.get((station, hour), {}),
                "lower": -context["observed"][station][hour], "upper": None,
            })
    horizon = anchor + pd.Timedelta(hours=4)
    sparse_events = {}
    for station in sorted(involved):
        check_budget()
        sparse_events[station] = [
            [when.isoformat(), phase, value]
            for (when, phase), value in context["background_events"][station]
            if anchor <= when <= horizon]

    outside_min = min((audit["baseline_min"] for station, audit in context["station_audits"].items()
                       if station not in involved), default=None)
    audits = context["station_audits"]
    residuals = [v for audit in audits.values() for v in audit["residuals"]]
    case["constraints"] = constraints
    # 빈 event_constraints는 사건 검사를 생략했다는 뜻이 아니다.
    # evaluate_selection은 아래 표현을 반드시 확인하고 희소 재생을 수행한다.
    case["event_constraints"] = []
    case.setdefault("metadata", {})["event_constraints_materialized"] = False
    case["event_validation"] = {
        "representation": "sparse_event_replay", "anchor": anchor.isoformat(),
        "horizon": horizon.isoformat(), "same_timestamp_order": "departures_before_returns",
        "initial_inventory": {s: context["observed"][s][0] for s in sorted(involved)},
        "background_events": sparse_events,
        "unchanged_stations_baseline_min": outside_min,
        "fixed_station_count": len(context["stations"]),
        "note": "Replay background and selected four-event deltas; no cumulative coefficient expansion.",
    }
    case["benefit_model"] = {
        "definition": "fixed_observed_prefix_reserve_RD1", "stations": context["stations"],
        "observed": context["observed"], "required_reserve": context["required_reserve"],
        "baseline_j": context["baseline_j"], "candidate_effects": effects,
        "unit": "sum_of_checkpoint_bicycle_shortfalls_NOT_lost_trips",
    }
    case["inventory_audit"] = {
        "status": "conditional_observed_checkpoints", "baseline_feasible": True,
        "event_reconstruction_baseline_feasible": all(a["baseline_min"] >= 0 for a in audits.values()),
        "negative_baseline_stations": sum(a["baseline_min"] < 0 for a in audits.values()),
        "nonzero_residual_points": sum(v != 0 for v in residuals),
        "max_absolute_residual": max(map(abs, residuals), default=0),
        "all_residuals_zero": all(v == 0 for v in residuals),
        "station_audits": audits, "horizon": horizon.isoformat(),
        "snapshot_semantics_assumed": True,
    }


def replay_selected_events(case, selected_ids):
    """선택 후보만 추가해 모든 사건 시점의 재고 하한을 정확히 검사한다.

    시각 문자열은 생성 시 모두 같은 ISO 형식으로 저장된다. 재생에서는
    Timestamp로 정규화하여 입력 문자열 표기 차이가 순서를 바꾸지 않게 한다.
    주 관측점 제약은 여기서 재검사하지 않고 별도 결과로 유지한다.
    """
    from .graph import validate_selection

    model = case.get("event_validation", {})
    if model.get("representation") != "sparse_event_replay":
        raise ValueError("사건 검사 표현이 없거나 지원되지 않습니다. 빈 제약을 유효로 간주하지 않습니다.")
    chosen = list(map(str, selected_ids))
    chosen_set = set(chosen)
    structural = validate_selection({**case, "constraints": []}, chosen)
    violations = list(structural["violations"])
    anchor, horizon = pd.Timestamp(model["anchor"]), pd.Timestamp(model["horizon"])
    changes = defaultdict(lambda: defaultdict(int))
    for candidate in case["candidates"]:
        if candidate["id"] in chosen_set:
            for station, when, phase, value in candidate_events(candidate):
                changes[station][(when, phase)] += value
    outside_min = model["unchanged_stations_baseline_min"]
    overall_min = outside_min
    if outside_min is not None and outside_min < 0:
        violations.append({"kind": "event_inventory_lower", "station": "unchanged_stations",
                           "inventory": outside_min})
    for station, initial in model["initial_inventory"].items():
        events = defaultdict(int)
        for when, phase, value in model["background_events"][station]:
            events[(pd.Timestamp(when), int(phase))] += int(value)
        for key, value in changes[station].items():
            events[key] += value
        stock = minimum = initial
        minimum_key = (anchor, -1)
        for key in sorted(events):
            when, _ = key
            if anchor <= when <= horizon:
                stock += events[key]
                if stock < minimum:
                    minimum, minimum_key = stock, key
        overall_min = minimum if overall_min is None else min(overall_min, minimum)
        if minimum < 0:
            violations.append({"kind": "event_inventory_lower", "station": station,
                               "time": minimum_key[0].isoformat(), "phase": minimum_key[1],
                               "inventory": minimum})
    return {"feasible": not violations, "violations": violations,
            "minimum_inventory": overall_min, "representation": "sparse_event_replay"}


def evaluate_selection(case, selected_ids):
    """동일 선택의 요청·관측점·사건 유효성과 집합 편익을 다시 계산한다."""
    from .graph import validate_selection

    chosen = list(map(str, selected_ids))
    checkpoint = validate_selection(case, chosen)
    event = replay_selected_events(case, chosen)
    model = case["benefit_model"]
    changes = defaultdict(int)
    chosen_set = set(chosen)
    for identifier in chosen_set:
        for station, hour, delta in model["candidate_effects"].get(identifier, []):
            changes[(station, hour)] += delta
    benefit = individual_benefit(model, changes)
    audit = case["inventory_audit"]
    baseline_feasible = audit["event_reconstruction_baseline_feasible"]
    selected_candidates = [c for c in case["candidates"] if c["id"] in chosen_set]
    return {
        "selected_count": len(set(chosen)), "checkpoint_feasible": checkpoint["feasible"],
        "checkpoint_violations": checkpoint["violations"], "event_feasible": event["feasible"],
        "event_violations": event["violations"], "event_baseline_feasible": baseline_feasible,
        "event_validation_representation": event["representation"],
        "strict_event_subgroup": baseline_feasible and event["feasible"] and audit["all_residuals_zero"],
        "baseline_j": model["baseline_j"], "selected_j": model["baseline_j"]-benefit,
        "benefit": benefit, "benefit_worsened": benefit < 0,
        "total_extra_walk_straight_m": sum(c["extra_walk_straight_m"] for c in selected_candidates),
        "total_extra_arrival_seconds": sum(c["extra_arrival_seconds"] for c in selected_candidates),
        "max_extra_arrival_seconds": max((c["extra_arrival_seconds"] for c in selected_candidates), default=0),
        "return_capacity_effect": None, "snapshot_semantics_assumed": True,
    }
