"""RD-1.0의 공통 후처리와 날짜 단위 비교 통계.

난수 기준선은 실제 사례의 동일 그래프에서 비트만 뽑는다. 합성 요청·재고·
충돌을 만들지 않는다. 모든 함수는 로컬 계산이며 양자 계정에 접근하지 않는다.
"""
from __future__ import annotations

from collections import defaultdict
import time

import numpy as np
import pandas as pd

from .encoding import decode_operational_mis
from .graph import validate_selection
from .benefit import candidate_events


def _adjacency(graph):
    nodes = [str(node["id"]) for node in graph["nodes"]]
    if len(nodes) != len(set(nodes)):
        raise ValueError("중복 그래프 정점 ID")
    adjacency = {node: set() for node in nodes}
    for edge in graph["edges"]:
        a, b = str(edge["source"]), str(edge["target"])
        if a not in adjacency or b not in adjacency or a == b:
            raise ValueError("잘못된 그래프 간선")
        adjacency[a].add(b)
        adjacency[b].add(a)
    return adjacency


def _repair(adjacency, selected):
    chosen = set(map(str, selected))
    if not chosen <= adjacency.keys():
        raise ValueError("그래프에 없는 선택 정점")
    # 현재 선택 집합 안의 충돌 차수가 가장 큰 정점을 제거한다.
    # 같은 차수이면 ID가 가장 작은 정점을 제거하는 규칙으로 고정한다.
    while True:
        degrees = {node: len(adjacency[node] & chosen) for node in chosen}
        if not degrees or max(degrees.values()) == 0:
            break
        remove = min(chosen, key=lambda node: (-degrees[node], node))
        chosen.remove(remove)
    # ID 순으로 충돌하지 않는 정점을 추가한다. 최적화 풀이로 완성하지 않는다.
    for node in sorted(adjacency):
        if not adjacency[node] & chosen:
            chosen.add(node)
    return sorted(chosen)


def common_graph_repair(graph, selected_ids):
    """모든 알고리즘 표본에 동일하게 적용하는 결정론적 보조 후처리."""
    return _repair(_adjacency(graph), selected_ids)


def _decode(graph, case, adjacency, selected):
    chosen = set(selected)
    independent = chosen <= adjacency.keys() and not any(adjacency[n] & chosen for n in chosen)
    if graph.get("encoding_map", {}).get("kind") == "bounded_cnf_independent_twins":
        decoded = decode_operational_mis(graph, sorted(chosen))
        candidates = decoded["selected_candidate_ids"]
        valid = independent and decoded["operational_feasible"]
        clause_complete = decoded["clause_complete"]
    else:
        candidates = sorted(chosen)
        valid = independent
        clause_complete = None
    checked = validate_selection(case, candidates)
    valid = valid and checked["feasible"]
    return {"valid": bool(valid), "selected_candidate_ids": candidates,
            "selected_candidate_count": len(candidates), "clause_complete": clause_complete}


def random_graph_baseline(graph, case, shots=2500, seed=20261109):
    """QPU 사전 규모군과 같은 1–80 정점 실제 정확 그래프의 난수 기준선.

    원시 비트는 독립 Bernoulli(0.5), 수선은 별도로 보고한다. 무효 표본도
    전체 shots 분모에 남긴다. 원시 노드 수에서 offset을 빼지 않고 복호화한
    유효 추천 수를 사용한다. 이 함수 호출은 QPU 실행을 뜻하지 않는다.
    """
    encoded = (graph.get("encoding_map", {}).get("kind") == "bounded_cnf_independent_twins"
               and graph.get("metadata", {}).get("representation") == "exact_operational_mis")
    if not (encoded or graph.get("metadata", {}).get("is_exact_for_declared_model", False)):
        raise ValueError("난수 비교에는 선언 모형의 정확 그래프가 필요합니다.")
    adjacency = _adjacency(graph)
    if not 1 <= len(adjacency) <= 80:
        raise ValueError("난수 기준선 사전 규모는 1–80 정점입니다.")
    if int(shots) != shots or shots <= 0:
        raise ValueError("shots는 양의 정수여야 합니다.")
    nodes = sorted(adjacency)
    rng = np.random.default_rng(seed)
    summaries = {name: {"valid_samples": 0, "invalid_samples": 0,
                        "best_candidate_count": None, "best_candidate_ids": [],
                        "valid_candidate_count_sum": 0, "clause_incomplete_samples": 0}
                 for name in ("raw", "repaired")}
    timings = {"bit_generation_seconds": 0., "repair_seconds": 0., "decode_seconds": 0.}
    started = time.perf_counter()
    for _ in range(int(shots)):
        before = time.perf_counter()
        raw = [node for node, bit in zip(nodes, rng.integers(0, 2, size=len(nodes))) if bit]
        timings["bit_generation_seconds"] += time.perf_counter()-before
        before = time.perf_counter()
        repaired = _repair(adjacency, raw)
        timings["repair_seconds"] += time.perf_counter()-before
        for name, selected in (("raw", raw), ("repaired", repaired)):
            before = time.perf_counter()
            decoded = _decode(graph, case, adjacency, selected)
            timings["decode_seconds"] += time.perf_counter()-before
            summary = summaries[name]
            if decoded["clause_complete"] is False:
                summary["clause_incomplete_samples"] += 1
            if not decoded["valid"]:
                summary["invalid_samples"] += 1
                continue
            summary["valid_samples"] += 1
            count, identifiers = decoded["selected_candidate_count"], decoded["selected_candidate_ids"]
            summary["valid_candidate_count_sum"] += count
            best = summary["best_candidate_count"]
            if best is None or count > best or (count == best and identifiers < summary["best_candidate_ids"]):
                summary["best_candidate_count"] = count
                summary["best_candidate_ids"] = identifiers
    for summary in summaries.values():
        summary["valid_fraction"] = summary["valid_samples"]/shots
        summary["mean_valid_candidate_count"] = (summary.pop("valid_candidate_count_sum") / summary["valid_samples"]
                                                  if summary["valid_samples"] else None)
    return {"shots": int(shots), "seed": int(seed), "random_bit_probability": .5,
            "logical_nodes": len(nodes), "raw": summaries["raw"], "repaired": summaries["repaired"],
            "timings": {**timings, "total_seconds": time.perf_counter()-started},
            "qpu_executed": False}


def paired_date_bootstrap(frame, left, right, *, date_column="date", repeats=2000, seed=20261109):
    """동일 행의 right-left를 비교하고 날짜 전체를 복원추출한다.

    행은 호출자가 같은 사례·변형끼리 연결해야 한다. 서로 다른 후보 모형의
    목적값을 같은 최적화 문제의 성능 차이로 해석하지 않는다. 유효 날짜가
    10개 미만이면 사전 규약에 따라 신뢰구간 없이 기술통계만 제공한다.
    """
    a, b = pd.to_numeric(frame[left], errors="coerce"), pd.to_numeric(frame[right], errors="coerce")
    good = np.isfinite(a) & np.isfinite(b) & frame[date_column].notna()
    paired = pd.DataFrame({"date": frame.loc[good, date_column].astype(str),
                           "difference": (b-a).loc[good]})
    groups = [part.difference.to_numpy(dtype=float) for _, part in paired.groupby("date", sort=True)]
    differences = paired.difference.to_numpy(dtype=float)
    result = {"direction": f"{right} - {left}", "paired_rows": len(paired),
              "excluded_unpaired_or_nonfinite_rows": int((~good).sum()), "dates": len(groups),
              "median_paired_difference": float(np.median(differences)) if len(paired) else None,
              "iqr": list(map(float, np.quantile(differences, [.25, .75]))) if len(paired) else None,
              "ci95": None, "bootstrap_repeats": 0, "seed": int(seed)}
    if len(groups) < 10:
        return {**result, "status": "descriptive_only_fewer_than_10_dates"}
    if int(repeats) != repeats or repeats <= 0:
        raise ValueError("bootstrap repeats는 양의 정수여야 합니다.")
    rng = np.random.default_rng(seed)
    statistics = []
    for _ in range(int(repeats)):
        draw = rng.integers(0, len(groups), size=len(groups))
        statistics.append(float(np.median(np.concatenate([groups[index] for index in draw]))))
    return {**result, "status": "paired_date_block_bootstrap",
            "bootstrap_repeats": int(repeats), "ci95": list(map(float, np.quantile(statistics, [.025, .975])))}


def event_path_sensitivity(context, case, selected_ids, *, returns_first=False, return_delay_seconds=0):
    """선택된 추천을 고정하고 사건 순서·반납 처리 지연만 재검사한다.

    원래/변경 반납을 모두 같은 지연만큼 이동한다. 관측 시작 재고 B는 고정하고
    그 이후 사건만 재생한다. 후보·J·주 최적화 모형을 다시 생성한 실험이라고
    표시하지 않는다. 이 계산은 실제 처리 지연을 관측했다는 주장도 아니다.
    """
    if return_delay_seconds < 0 or not np.isfinite(return_delay_seconds):
        raise ValueError("반납 처리 지연은 유한한 비음수여야 합니다.")
    chosen = set(map(str, selected_ids))
    known = {str(c["id"]) for c in case["candidates"]}
    if not chosen <= known or len(chosen) != len(selected_ids):
        raise ValueError("없거나 중복된 선택 후보")
    anchor, horizon = context["anchor"], context["anchor"]+pd.Timedelta(hours=4)
    delay = pd.Timedelta(seconds=float(return_delay_seconds))

    def event_key(when, phase):
        available = when+delay if phase == 1 else when
        order = 1-phase if returns_first else phase
        return available, order

    changes = defaultdict(lambda: defaultdict(int))
    for candidate in case["candidates"]:
        if candidate["id"] in chosen:
            for station, when, phase, change in candidate_events(candidate):
                changes[station][event_key(when, phase)] += change
    baseline_negative = selected_negative = nonzero_residuals = 0
    min_baseline = min_selected = None
    for station in context["stations"]:
        events = defaultdict(int)
        for (when, phase), value in context["background_events"][station]:
            events[event_key(when, phase)] += value
        balance = selected_balance = context["observed"][station][0]
        station_minimum = selected_minimum = balance
        for key in sorted(set(events) | set(changes[station])):
            when, _ = key
            if not anchor <= when <= horizon:
                continue
            balance += events.get(key, 0)
            selected_balance += events.get(key, 0)+changes[station].get(key, 0)
            station_minimum = min(station_minimum, balance)
            selected_minimum = min(selected_minimum, selected_balance)
        for hour in range(1, 5):
            checkpoint = anchor+pd.Timedelta(hours=hour)
            predicted = context["observed"][station][0]+sum(
                value for (when, _), value in events.items() if anchor <= when < checkpoint)
            nonzero_residuals += predicted != context["observed"][station][hour]
        baseline_negative += station_minimum < 0
        selected_negative += selected_minimum < 0
        min_baseline = station_minimum if min_baseline is None else min(min_baseline, station_minimum)
        min_selected = selected_minimum if min_selected is None else min(min_selected, selected_minimum)
    checkpoint = validate_selection(case, sorted(chosen))
    # 시간 경로의 재고 검사와 요청/자원 배타성을 함께 확인한다.
    non_inventory = {**case, "constraints": []}
    request_and_resource = validate_selection(non_inventory, sorted(chosen))["feasible"]
    feasible = selected_negative == 0 and request_and_resource
    return {"scope": "fixed_recommendations_event_path_sensitivity",
            "returns_first": bool(returns_first), "return_delay_seconds": float(return_delay_seconds),
            "event_baseline_feasible": baseline_negative == 0, "event_feasible": feasible,
            "checkpoint_feasible_under_primary_model": checkpoint["feasible"],
            "negative_baseline_stations": baseline_negative, "negative_selected_stations": selected_negative,
            "baseline_minimum_inventory": min_baseline, "selected_minimum_inventory": min_selected,
            "nonzero_baseline_residual_points": nonzero_residuals,
            "strict_event_subgroup": baseline_negative == 0 and feasible and nonzero_residuals == 0,
            "snapshot_semantics_assumed": True}
