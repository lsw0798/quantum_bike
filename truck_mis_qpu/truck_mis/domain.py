"""대여소·작업 후보와 운영 제약. 양자 SDK 및 인증과 독립적인 순수 함수 모음."""
from __future__ import annotations

from dataclasses import asdict, dataclass
from itertools import combinations
import math
import random


@dataclass(frozen=True)
class Station:
    station_id: str
    name: str
    latitude: float
    longitude: float
    bikes: int
    target: int

    @classmethod
    def from_dict(cls, row: dict) -> "Station":
        return cls(**{key: row[key] for key in cls.__dataclass_fields__})


@dataclass(frozen=True)
class Task:
    task_id: str
    pickup_id: str
    delivery_id: str
    quantity: int
    start_min: float
    end_min: float
    distance_km: float


def distance_km(a: Station, b: Station) -> float:
    """위경도의 Haversine 거리. 도로 거리가 아닌 직선거리 근사이다."""
    lat1, lat2 = math.radians(a.latitude), math.radians(b.latitude)
    dlat = lat2 - lat1
    dlon = math.radians(b.longitude - a.longitude)
    h = math.sin(dlat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2) ** 2
    return 6371.0088 * 2 * math.asin(min(1.0, math.sqrt(h)))


def conflict_reasons(a: Task, b: Task) -> list[str]:
    """[시작, 종료) 구간을 사용하여 바로 이어지는 작업은 허용한다."""
    reasons = []
    if max(a.start_min, b.start_min) < min(a.end_min, b.end_min):
        reasons.append("time_overlap")
    if {a.pickup_id, a.delivery_id} & {b.pickup_id, b.delivery_id}:
        reasons.append("shared_station")
    return reasons


def build_case(station_rows: list[dict], config: dict, metadata: dict) -> dict:
    """고정 수량의 왕복 작업을 열거하고 사전 설정된 난수 표본으로 규모를 제한한다.

    모든 작업은 빈 트럭으로 s→수거소→배치소→s를 수행한다. 대여소는 계획기간
    중 최대 한 작업에서 사용한다. 이 조건 아래에서 충돌그래프와 운영 제약이 대응한다.
    후보 표본 추출은 QPU 용량을 위한 명시적 입력 축소이며, MIS 해를 계산하지 않는다.
    """
    stations = {row["station_id"]: Station.from_dict(row) for row in station_rows}
    origin_id = str(config["origin_station_id"])
    if origin_id not in stations:
        raise ValueError(f"출발 대여소 {origin_id}의 좌표·재고·목표 재고가 없습니다.")
    origin = stations[origin_id]
    nearby = sorted(
        (s for s in stations.values() if s.station_id != origin_id
         and distance_km(origin, s) <= config["radius_km"]),
        key=lambda s: (distance_km(origin, s), s.station_id),
    )
    q = config["bikes_per_task"]
    pickups = [s for s in nearby if s.bikes - s.target >= q]
    deliveries = [s for s in nearby if s.target - s.bikes >= q]
    if not pickups or not deliveries:
        raise ValueError("반경 내 고정 수량을 수거·배치할 대여소가 부족합니다. 날짜·반경·수량을 확인하세요.")

    tasks = []
    for pickup in pickups:
        for delivery in deliveries:
            km = (distance_km(origin, pickup) + distance_km(pickup, delivery)
                  + distance_km(delivery, origin))
            duration = (60 * km / config["truck_speed_kmh"]
                        + 2 * q * config["handling_min_per_bike"]
                        + 2 * config["stop_overhead_min"])
            n_slots = math.floor(config["horizon_min"] / config["start_step_min"]) + 1
            for slot in range(n_slots):
                start = slot * config["start_step_min"]
                if start + duration <= config["horizon_min"]:
                    tasks.append(Task("", pickup.station_id, delivery.station_id, q,
                                      start, start + duration, km))
    if not tasks:
        raise ValueError("계획기간 안에 완료되는 왕복 작업이 없습니다.")
    total_candidates = len(tasks)
    if len(tasks) > config["max_tasks"]:
        # 그래프나 해의 품질을 확인하기 전에 한 번만 표본을 고정한다.
        indices = sorted(random.Random(config["candidate_seed"]).sample(range(len(tasks)), config["max_tasks"]))
        tasks = [tasks[i] for i in indices]
    tasks = [Task(f"T{i:03d}", t.pickup_id, t.delivery_id, t.quantity,
                  t.start_min, t.end_min, t.distance_km) for i, t in enumerate(tasks)]
    edges = []
    for i, j in combinations(range(len(tasks)), 2):
        reasons = conflict_reasons(tasks[i], tasks[j])
        if reasons:
            edges.append({"source": i, "target": j, "reasons": reasons})
    return {
        "schema_version": 1,
        "objective": "maximum_number_of_fixed_quantity_tasks",
        "representation": "exact_under_declared_round_trip_and_station_exclusivity",
        "assumptions": {
            "one_truck": True, "empty_start_and_finish_per_task": True,
            "return_to_origin_per_task": True, "station_use_at_most_once": True,
            "static_inventory": True, "fixed_quantity": q,
            "distance_model": "haversine_straight_line",
            "rack_count_is_not_a_capacity_constraint": True,
        },
        "config": config,
        "data": metadata,
        "origin": asdict(origin),
        "stations": [asdict(s) for s in nearby],
        "candidate_audit": {
            "enumerated_tasks": total_candidates, "retained_tasks": len(tasks),
            "retained_fraction": len(tasks) / total_candidates,
            "selection": "seeded_uniform_sample_before_embedding",
            "nearby_stations": len(nearby), "pickup_stations": len(pickups),
            "delivery_stations": len(deliveries),
        },
        "tasks": [asdict(t) for t in tasks], "edges": edges,
    }


def validate_selection(case: dict, selected: list[int]) -> dict:
    """그래프와 독립적으로 시간·트럭·재고 조건을 다시 계산한다.

    그래프 엣지를 재사용하지 않으므로 후보 생성/충돌 정의의 오류도 검증할 수 있다.
    """
    tasks = [Task(**row) for row in case["tasks"]]
    if len(selected) != len(set(selected)) or any(i < 0 or i >= len(tasks) for i in selected):
        return {"feasible": False, "violations": ["invalid_selection"]}
    chosen = sorted((tasks[i] for i in selected), key=lambda t: (t.start_min, t.task_id))
    station_map = {s["station_id"]: s for s in case["stations"]}
    inventories = {sid: s["bikes"] for sid, s in station_map.items()}
    used, violations, previous_end = set(), [], 0.0
    for t in chosen:
        if t.pickup_id not in station_map or t.delivery_id not in station_map or t.pickup_id == t.delivery_id:
            violations.append("invalid_station")
            continue
        if not all(math.isfinite(x) for x in (t.start_min, t.end_min, t.distance_km)):
            violations.append("nonfinite_task")
            continue
        origin = Station.from_dict(case["origin"])
        pickup, delivery = Station.from_dict(station_map[t.pickup_id]), Station.from_dict(station_map[t.delivery_id])
        expected_km = distance_km(origin, pickup) + distance_km(pickup, delivery) + distance_km(delivery, origin)
        required_min = (60 * expected_km / case["config"]["truck_speed_kmh"]
                        + 2 * t.quantity * case["config"]["handling_min_per_bike"]
                        + 2 * case["config"]["stop_overhead_min"])
        if (t.end_min <= t.start_min or t.start_min < 0 or t.distance_km < 0
                or not math.isclose(t.distance_km, expected_km, abs_tol=1e-7)
                or t.end_min - t.start_min < required_min - 1e-7):
            violations.append("invalid_route_duration")
        if t.start_min < previous_end or t.end_min > case["config"]["horizon_min"]:
            violations.append("truck_time")
        if t.quantity > case["config"]["truck_capacity"] or t.quantity <= 0:
            violations.append("truck_load")
        if t.quantity != case["assumptions"]["fixed_quantity"]:
            violations.append("quantity")
        if t.pickup_id in used or t.delivery_id in used:
            violations.append("station_reuse")
        inventories[t.pickup_id] -= t.quantity
        inventories[t.delivery_id] += t.quantity
        if inventories[t.pickup_id] < station_map[t.pickup_id]["target"]:
            violations.append("pickup_below_target")
        if inventories[t.delivery_id] > station_map[t.delivery_id]["target"]:
            violations.append("delivery_above_target")
        used.update([t.pickup_id, t.delivery_id])
        previous_end = t.end_min
    moved = sum(t.quantity for t in chosen)
    return {
        "feasible": not violations, "violations": sorted(set(violations)),
        "selected_task_count": len(chosen), "bikes_delivered": moved,
        "shortage_reduction": moved, "surplus_reduction": moved,
        "total_distance_km": sum(t.distance_km for t in chosen),
        "total_active_min": sum(t.end_min - t.start_min for t in chosen),
        "finish_min": previous_end,
        "schedule": [asdict(t) for t in chosen],
        "final_inventory": {sid: inventories[sid] for sid in sorted(used)},
    }


def evaluate_counts(case: dict, counts: dict[str, int]) -> dict:
    """원시 표본을 수정하지 않고 그래프·운영 제약을 평가한다. 최적해를 보장하지 않는다."""
    n = len(case["tasks"])
    samples, total, valid = [], 0, 0
    for bits, count in counts.items():
        if not isinstance(bits, str) or len(bits) != n or set(bits) - {"0", "1"}:
            raise ValueError("측정 비트열의 길이·문자가 제출 당시 노드 순서와 일치하지 않습니다.")
        if isinstance(count, bool) or not isinstance(count, int) or count < 0:
            raise ValueError("표본 빈도는 0 이상의 정수여야 합니다.")
        selected = [i for i, bit in enumerate(bits) if bit == "1"]
        independent = not any(bits[e["source"]] == bits[e["target"]] == "1" for e in case["edges"])
        operational = validate_selection(case, selected)
        feasible = independent and operational["feasible"]
        total += count
        valid += count if feasible else 0
        samples.append({"bitstring": bits, "count": count, "independent": independent,
                        "operational": operational, "feasible": feasible})
    if total <= 0:
        raise ValueError("QPU 결과에 유효한 측정 횟수가 없습니다.")
    # 동점은 비트열 순으로 고정한다. 이동거리로 새로운 최적화 목표를 추가하지 않는다.
    valid_rows = [r for r in samples if r["feasible"] and r["count"] > 0]
    best = min(valid_rows, key=lambda r: (-r["operational"]["selected_task_count"], r["bitstring"])) if valid_rows else None
    return {"shots_received": total, "feasible_shots": valid,
            "raw_feasible_rate": valid / total, "samples": samples,
            "best_observed_feasible": best, "optimum_proven": False,
            "postprocessing": "decode_validate_and_rank_only_no_repair"}
