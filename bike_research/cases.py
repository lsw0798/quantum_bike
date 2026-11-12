"""실측 여행에서 대체 출발/반납 후보와 시간별 재고 제약을 만든다.

이것은 회고적 시나리오다. 시간별 값을 정시 스냅샷으로 해석하고,
관측되지 않은 운영자 이동은 넣지 않는다. 두 가정은 결과에 항상 기록한다.
"""
from collections import defaultdict
import math

import numpy as np
import pandas as pd
from scipy.spatial import cKDTree


def distance_m(lat1, lon1, lat2, lon2):
    """Haversine 직선거리. 도보 경로 거리로 표시하지 않는다."""
    a, b = np.radians([lat1, lat2])
    dl = math.radians(lon2-lon1)

    # 지구 반지름 6,371km를 사용한다. 부동소수 오차로 1을 넘지 않게 자른다.
    return float(2 * 6_371_000 * math.asin(min(1, math.sqrt(
        math.sin((b-a)/2)**2 + math.cos(a)*math.cos(b)*math.sin(dl/2)**2))))


def neighbours(stations, radius_m=500, alternatives=2):
    """자기 자신과 반경 이웃. alternatives=None이면 이웃 수를 제한하지 않는다."""
    ids = list(stations.index)

    # 위경도를 단위 구 위의 3차원 좌표로 바꿔 KD-트리를 만든다.
    lat = np.radians(stations.lat.to_numpy())
    lon = np.radians(stations.lon.to_numpy())
    xyz = np.column_stack((np.cos(lat)*np.cos(lon), np.cos(lat)*np.sin(lon), np.sin(lat)))
    tree = cKDTree(xyz)
    result = {}

    # 대여소별 반복
    for i, sid in enumerate(ids):
        # 반경을 단위 구의 현 길이로 바꿔 주변 대여소를 찾는다.
        nearby = tree.query_ball_point(xyz[i], 2*math.sin(radius_m/(2*6_371_000)))

        # 자기 자신을 뺀 주변 대여소를 실제 거리순으로 정렬한다.
        ranked = sorted((distance_m(stations.loc[sid, "lat"], stations.loc[sid, "lon"],
                                    stations.iloc[j].lat, stations.iloc[j].lon), ids[j])
                        for j in nearby if j != i)

        # 자기 자신(거리 0)을 맨 앞에 두고 가까운 대체 대여소를 덧붙인다.
        result[sid] = [(sid, 0.0)] + [(other, dist) for dist, other in ranked[:alternatives]]
    return result


def create_case(trips, inventory, stations, day, hour, district, request_limit,
                window_minutes=30, radius_m=500, alternatives=2):
    """지정한 날짜·시간·구의 실측 이용에서 대체 후보와 재고 제약을 담은 사례를 만든다."""
    # 분석 구간의 시작과 끝을 정한다.
    anchor = pd.Timestamp(day) + pd.Timedelta(hours=int(hour))
    stop = anchor + pd.Timedelta(minutes=window_minutes)

    # 구간 안에 해당 구에서 출발한 유효 이용만 고른다.
    valid = trips.loc[trips.valid].copy()
    area = set(stations.index[stations.district.eq(district)])
    selected = valid.loc[(valid.start >= anchor) & (valid.start < stop)
                         & valid.origin.isin(area)
                         & valid.origin.isin(stations.index)
                         & valid.destination.isin(stations.index)]

    # 결정론적으로 중첩되는 표본을 만든다. 제외되는 건수도 보존한다.
    selected = selected.sort_values(["start", "request_id"])
    eligible_count = len(selected)
    selected = selected.head(request_limit)

    # 시작 시각의 재고 스냅샷이 있는 이용만 남긴다.
    snapshots = inventory.loc[inventory.time.eq(anchor)].set_index("station").inventory.to_dict()
    selected = selected.loc[selected.origin.isin(snapshots)
                            & selected.destination.isin(snapshots)]

    # 대체 대여소 목록을 준비한다.
    nearby = neighbours(stations, radius_m, alternatives)
    candidates, requests = [], []
    excluded = defaultdict(int)

    # 요청(실측 이용)별 반복
    for row in selected.itertuples():
        # 요청 정보를 기록한다.
        request = {
            "request_id": row.request_id,
            "origin": row.origin,
            "destination": row.destination,
            "start": row.start.isoformat(),
            "end": row.end.isoformat(),
            "source_file": row.source_file,
            "source_row": int(row.source_row),
            "bike_id_observed": row.bike_id,
        }
        requests.append(request)

        # 원래 경로의 직선거리를 계산한다.
        original_distance = distance_m(
            stations.loc[row.origin, "lat"], stations.loc[row.origin, "lon"],
            stations.loc[row.destination, "lat"], stations.loc[row.destination, "lon"])

        # 출발·반납 대여소의 대체 조합별 반복
        for origin, walk_start in nearby[row.origin]:
            for destination, walk_end in nearby[row.destination]:
                # 원래 경로와 같은 조합은 후보가 아니다.
                if (origin, destination) == (row.origin, row.destination):
                    continue

                # 재고 스냅샷이 없는 대여소는 제외하고 사유를 센다.
                if origin not in snapshots or destination not in snapshots:
                    excluded["missing_snapshot"] += 1
                    continue

                # 재현 가능한 시나리오 파라미터이며, 실측한 개인 선호가 아니다.
                new_distance = distance_m(
                    stations.loc[origin, "lat"], stations.loc[origin, "lon"],
                    stations.loc[destination, "lat"], stations.loc[destination, "lon"])

                # 보행 1.2m/s, 자전거 4.0m/s로 후보의 출발·도착 시각을 계산한다.
                walking_seconds = math.ceil(walk_start / 1.2)
                cycling_delta = math.ceil((new_distance-original_distance)/4.0)
                duration = max(60, int((row.end-row.start).total_seconds())+cycling_delta)
                start = row.start + pd.Timedelta(seconds=walking_seconds)
                end = start + pd.Timedelta(seconds=duration)

                # 원래보다 15분 넘게 늦어지는 후보는 제외하고 사유를 센다.
                if (end + pd.Timedelta(seconds=walk_end/1.2) - row.end).total_seconds() > 900:
                    excluded["extra_time_over_15min"] += 1
                    continue

                # 후보를 등록한다.
                candidates.append({
                    "id": f"v{len(candidates):06d}",
                    "request_id": row.request_id,
                    "origin": origin,
                    "destination": destination,
                    "start": start.isoformat(),
                    "end": end.isoformat(),
                    "original_origin": row.origin,
                    "original_destination": row.destination,
                    "original_start": row.start.isoformat(),
                    "original_end": row.end.isoformat(),
                    "extra_walk_straight_m": round(walk_start+walk_end, 3),
                    "time_model": "observed_duration_plus_straight_distance_delta",
                    "source_file": row.source_file,
                    "source_row": int(row.source_row),
                })

    # 사례 ID와 사례 본문을 만들고, 조건과 가정을 메타데이터에 남긴다.
    case_id = f"{day}_{hour:02d}_{district}_{window_minutes}m_r{request_limit}"
    case = {
        "case_id": case_id,
        "requests": requests,
        "candidates": candidates,
        "metadata": {
            "date": day,
            "hour": int(hour),
            "district": district,
            "window_minutes": window_minutes,
            "request_limit": request_limit,
            "eligible_requests": eligible_count,
            "snapshot_missing_requests": int(min(eligible_count, request_limit)-len(selected)),
            "radius_m": radius_m,
            "neighbour_alternatives": alternatives,
            "walking_m_s": 1.2,
            "cycling_m_s": 4.0,
            "max_extra_seconds": 900,
            "selection": "peak_date_hour_district; earliest requests; nested scale",
            "candidate_exclusions": dict(excluded),
            "assumptions": [
                "retrospective_completed_trip_requests_future_destination_known",
                "hour_label_treated_as_instant_snapshot_UNCONFIRMED",
                "unobserved_operator_movements_not_reconstructed",
                "metadata_snapshot_is_retrospective_proxy",
                "no_measured_return_capacity_upper_bound",
                "no_observed_acceptance_or_lost_demand",
                "same_timestamp_departures_before_returns",
            ],
        },
    }

    # 중복 관측을 뺀 전체 이용을 배경 이벤트로 삼아 재고 제약을 붙인다.
    background = trips.loc[~trips.get("duplicate_observation", pd.Series(False, index=trips.index))]
    case["metadata"]["incomplete_request_rows_in_loaded_data"] = int((~background.valid).sum())
    case["metadata"]["background_policy"] = (
        "all individually known endpoint events; unknown endpoints remain missing")
    attach_inventory_constraints(case, background, inventory, snapshots, anchor)
    return case


def attach_inventory_constraints(case, trips, inventory, snapshots, anchor):
    """취소된 원래 사건과 새 사건의 차이를 누적한다. 모든 배경 이용 포함.

    사전·중간·종료 재고에서 I0 + sum(delta*x) >= 0.
    음수 기준재고를 잘라내거나 공급을 조작하지 않는다.
    """
    # 후보가 없으면 제약도 없다.
    if not case["candidates"]:
        case.update(constraints=[], inventory_audit={"status": "no_candidates"})
        return

    # 대여소 -> (시각, 단계) -> 후보 -> 재고 변화량 구조로 누적한다.
    changes = defaultdict(lambda: defaultdict(lambda: defaultdict(int)))
    involved = set()
    horizon = anchor

    # 후보별로 네 가지 재고 변화를 기록한다.
    for c in case["candidates"]:
        # 원래 출발 취소(+1), 원래 반납 취소(-1), 새 출발(-1), 새 반납(+1).
        # 단계 0은 출발, 1은 반납이며 같은 시각이면 출발을 먼저 처리한다.
        effects = [(c["original_origin"], c["original_start"], 0, +1),
                   (c["original_destination"], c["original_end"], 1, -1),
                   (c["origin"], c["start"], 0, -1),
                   (c["destination"], c["end"], 1, +1)]
        for sid, when, phase, effect in effects:
            time = pd.Timestamp(when)
            changes[sid][(time, phase)][c["id"]] += effect
            involved.add(sid)
            horizon = max(horizon, time)

    # 관련 대여소의 배경 출발(-1)과 반납(+1)을 누적한다.
    baseline = defaultdict(lambda: defaultdict(int))
    for column, station_col, phase, sign in [
            ("start", "origin", 0, -1), ("end", "destination", 1, 1)]:
        rows = trips.loc[(trips[column] >= anchor) & (trips[column] <= horizon)
                         & trips[station_col].isin(involved)]
        for sid, time in zip(rows[station_col], rows[column]):
            baseline[sid][(time, phase)] += sign

    rows, audit, seen = [], [], set()

    # 대여소별로 시간 순서대로 재고를 따라가며 제약을 만든다.
    for sid in sorted(involved):
        stock = float(snapshots[sid])
        coefficients = {}
        minimum = stock
        times = sorted(set(changes[sid]) | set(baseline[sid]))

        # 시작 시점의 재고 하한 제약
        rows.append({
            "id": f"initial:{sid}",
            "kind": "inventory_lower",
            "station": sid,
            "time": anchor.isoformat(),
            "coefficients": {},
            "lower": -stock,
            "upper": None,
        })

        # 이벤트 시점별 반복
        for time_phase in times:
            # 배경 이벤트를 재고에 반영한다.
            stock += baseline[sid].get(time_phase, 0)
            minimum = min(minimum, stock)

            # 후보별 계수를 누적한다. 상쇄되어 0이 된 계수는 지운다.
            for cid, effect in changes[sid].get(time_phase, {}).items():
                coefficients[cid] = coefficients.get(cid, 0) + effect
                if coefficients[cid] == 0:
                    del coefficients[cid]

            # 계수와 하한이 같은 제약은 한 번만 추가한다.
            signature = (tuple(sorted(coefficients.items())), -stock)
            if signature not in seen:
                rows.append({
                    "id": f"inventory:{sid}:{len(rows)}",
                    "kind": "inventory_lower",
                    "station": sid,
                    "time": time_phase[0].isoformat(),
                    "phase": time_phase[1],
                    "coefficients": dict(coefficients),
                    "lower": -stock,
                    "upper": None,
                })
                seen.add(signature)

        # 구간 안의 실제 관측 스냅샷을 가져온다.
        checkpoints = inventory.loc[(inventory.station == sid) & (inventory.time > anchor)
                                    & (inventory.time <= horizon)].sort_values("time")
        residuals = []

        # 관측값과 사건 재구성 예측값의 차이를 감사용으로 기록한다.
        for snap in checkpoints.itertuples():
            predicted = float(snapshots[sid]) + sum(
                value for (time, phase), value in baseline[sid].items() if time < snap.time)
            residuals.append({
                "time": snap.time.isoformat(),
                "observed": float(snap.inventory),
                "predicted_before_events": predicted,
                "residual": float(snap.inventory)-predicted,
            })

        # 대여소별 감사 기록을 남긴다.
        audit.append({
            "station": sid,
            "initial": float(snapshots[sid]),
            "baseline_min": minimum,
            "baseline_final": stock,
            "checkpoints": residuals,
        })

    # 제약과 감사 기록을 사례에 저장한다.
    case["constraints"] = rows
    case["inventory_audit"] = {
        "status": "conditional_reconstruction",
        "horizon": horizon.isoformat(),
        "stations": audit,
        "baseline_feasible": all(x["baseline_min"] >= 0 for x in audit),
        "negative_baseline_stations": sum(x["baseline_min"] < 0 for x in audit),
    }

    # 실측 반납 용량 상한은 없으므로 0건으로 기록한다.
    case["metadata"]["known_capacity_constraints"] = 0


def checkpoint_variant(case, inventory):
    """실제 관측 시점에만 재고 하한을 적용하는 별도의 조건부 모형.

    관측 사이 실행 가능성은 보장하지 않는다. 원래 사건 제약은 감사용으로
    그대로 보존하며, 이 모형을 사건 모형의 정확한 대체라고 표시하지 않는다.
    """
    import copy

    # 원본 사례를 복사하고, 사건 제약은 감사용 필드로 옮긴다.
    result = copy.deepcopy(case)
    result["case_id"] += "_checkpoints"
    result["event_constraints"] = result.pop("constraints")
    result["constraints"] = []
    result["metadata"]["inventory_model"] = "observed_checkpoints_only"
    result["metadata"]["assumptions"].append("inter_checkpoint_feasibility_NOT_guaranteed")

    # 후보가 없으면 추가할 제약도 없다.
    if not result["candidates"]:
        return result

    # 사례의 시작 시각과, 정시로 올린 종료 시각을 구한다.
    anchor = (pd.Timestamp(result["metadata"]["date"])
              + pd.Timedelta(hours=result["metadata"]["hour"]))
    horizon = pd.Timestamp(result["inventory_audit"]["horizon"]).ceil("h")

    # 대여소별로 후보가 일으키는 재고 변화를 모은다.
    changes = defaultdict(list)
    for c in result["candidates"]:
        for sid, ts, sign in [(c["original_origin"], c["original_start"], 1),
                              (c["original_destination"], c["original_end"], -1),
                              (c["origin"], c["start"], -1),
                              (c["destination"], c["end"], 1)]:
            changes[sid].append((pd.Timestamp(ts), c["id"], sign))

    # 관측 스냅샷마다 재고 하한 제약을 하나씩 만든다.
    for snap in inventory.loc[inventory.time.between(anchor, horizon)
                              & inventory.station.isin(changes)].itertuples():
        # 스냅샷 시각 이전에 일어난 변화만 계수에 누적한다.
        coefficients = defaultdict(int)
        for ts, cid, sign in changes[snap.station]:
            if ts < snap.time:
                coefficients[cid] += sign

        result["constraints"].append({
            "id": f"checkpoint:{snap.station}:{snap.time.isoformat()}",
            "kind": "observed_inventory_lower",
            "station": snap.station,
            "time": snap.time.isoformat(),
            "coefficients": {cid: v for cid, v in coefficients.items() if v},
            "lower": -float(snap.inventory),
            "upper": None,
        })

    # 사건 모형의 판정은 따로 보존한다. 관측 재고는 음수가 아니므로
    # 아무것도 고르지 않은 기준 상태는 이 모형의 제약을 항상 만족한다.
    result["inventory_audit"]["event_reconstruction_baseline_feasible"] = (
        result["inventory_audit"].get("baseline_feasible"))
    result["inventory_audit"]["baseline_feasible"] = True
    return result
