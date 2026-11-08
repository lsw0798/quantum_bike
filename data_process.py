"""로컬 자료 처리 전용 진입점. QPU 모듈을 import하거나 호출하지 않는다.

실행: python data_process.py
확대: python data_process.py --months 202509,202512 --days 3 --request-limits 2,40,200,600
출력: outputs/summary.csv, outputs/cases/<case_id>/
"""
import argparse
import hashlib
import json
from pathlib import Path
import platform
import time

import pandas as pd

from bike_research.data import (
    monthly_ranking, daily_ranking, load_stations,
    load_trips, load_inventory, dump_json,
)
from bike_research.cases import create_case, checkpoint_variant
from bike_research.graph import (
    build_graph, benchmark_graph, solve_operational,
    save_graph, validate_selection,
)
from bike_research.encoding import (
    encode_operational_mis, decode_operational_mis,
    lift_operational_selection,
)


def process_case(case, folder, time_limit, exact_candidate_limit):
    """사례 하나를 처리해 그래프 생성, 벤치마크, 정확 인코딩을 수행하고 요약 행을 반환한다."""
    folder.mkdir(parents=True, exist_ok=True)

    # 이 파이프라인이 만든 파생 산출물만 지운다. 원본 자료는 절대 지우지 않는다.
    for name in ("graph.json", "nodes.csv", "edges.csv", "graph_benchmark.json",
                 "operational_benchmark.json", "exact_graph.json",
                 "exact_graph_benchmark.json", "encoding_status.json"):
        (folder / name).unlink(missing_ok=True)

    # 사례를 저장하고 입력 해시를 계산한다.
    dump_json(folder / "case.json", case)
    case_hash = hashlib.sha256((folder / "case.json").read_bytes()).hexdigest()

    # 그래프를 만들고 사례 해시를 기록한 뒤 저장한다.
    graph = build_graph(case, inference_seconds=5)
    graph["metadata"]["case_sha256"] = case_hash
    save_graph(graph, folder)

    # 그래프 벤치마크를 돌리고 해당 해가 운영상 가능한지 검증한다.
    graph_result = benchmark_graph(graph, time_limit=time_limit, greedy_repeats=8)
    graph_result["operational_validation"] = validate_selection(case, graph_result["selected_ids"])

    # 원래의 운영 모델을 직접 푼다.
    operating_result = solve_operational(case, time_limit=time_limit)
    dump_json(folder / "graph_benchmark.json", graph_result)
    dump_json(folder / "operational_benchmark.json", operating_result)

    # 정확 인코딩 상태의 기본값은 '시도하지 않음'이다.
    exact = {"status": "not_attempted", "reason": "candidate_count_over_configured_limit"}

    # 후보 수가 한도 이하이고 비어 있지 않을 때만 정확 인코딩을 시도한다.
    if len(case["candidates"]) <= exact_candidate_limit and case["candidates"]:
        try:
            # 운영 모델을 보조 그래프(MIS)로 인코딩하고 저장한다.
            encoded = encode_operational_mis(
                case, max_support=16, max_nodes=12000, max_edges=500000)
            encoded["metadata"]["case_sha256"] = case_hash
            dump_json(folder / "exact_graph.json", encoded)
            exact = {"status": "encoded",
                     "nodes": len(encoded["nodes"]),
                     "edges": len(encoded["edges"])}

            # 운영 해가 있으면 보조 그래프로 옮긴(lift) 뒤 다시 복원(decode)해 확인한다.
            if operating_result["objective"] is not None:
                lifted = lift_operational_selection(encoded, operating_result["selected_ids"])
                decoded = decode_operational_mis(encoded, lifted)
                exact["lifted_operational_solution"] = {
                    "graph_size": len(lifted),
                    "decoded": decoded,
                    "optimality_certified_by_original_model": operating_result["optimal"],
                }

            # 정확 그래프 벤치마크도 로컬 고전 계산이다. 노드가 5000개 이하일 때만 수행한다.
            if len(encoded["nodes"]) <= 5000:
                result = benchmark_graph(encoded, time_limit=time_limit, greedy_repeats=2)
                result["decoded"] = decode_operational_mis(encoded, result["selected_ids"])
                result["interpretation"] = (
                    "Auxiliary-graph solve time is not intrinsic operational hardness. "
                    "Compare lifted original-model solution.")
                dump_json(folder / "exact_graph_benchmark.json", result)
                exact.update(classical_status=result["status"], classical_gap=result["gap"])
        except (ValueError, RuntimeError) as error:
            # 인코딩이 실패하면 사유를 기록한다.
            exact = {"status": "not_encoded", "reason": str(error)}

    # 인코딩 상태에 해시를 붙여 저장한다.
    exact["case_sha256"] = case_hash
    dump_json(folder / "encoding_status.json", exact)

    # summary.csv에 들어갈 한 줄 요약을 만들어 반환한다.
    metadata = case["metadata"]
    return {
        "case_id": case["case_id"],
        "date": metadata["date"],
        "hour": metadata["hour"],
        "district": metadata["district"],
        "request_limit": metadata["request_limit"],
        "inventory_model": metadata.get("inventory_model", "event_reconstruction"),
        "requests": len(case["requests"]),
        "candidates": len(case["candidates"]),
        "nodes": len(graph["nodes"]),
        "edges": len(graph["edges"]),
        "constraints": len(case["constraints"]),
        "representation": graph["metadata"]["representation"],
        "baseline_feasible": case["inventory_audit"].get("baseline_feasible", False),
        "graph_status": graph_result["status"],
        "graph_objective": graph_result["objective"],
        "graph_upper_bound": graph_result["upper_bound"],
        "graph_gap": graph_result["gap"],
        "graph_seconds": graph_result["solver_seconds"],
        "graph_hardness": graph_result.get("hardness_status", ""),
        "graph_solution_operational_feasible": graph_result["operational_validation"]["feasible"],
        "operational_status": operating_result["status"],
        "operational_objective": operating_result["objective"],
        "operational_upper_bound": operating_result["upper_bound"],
        "operational_gap": operating_result["gap"],
        "operational_seconds": operating_result["solver_seconds"],
        "encoding_status": exact["status"],
        "exact_nodes": exact.get("nodes"),
        "exact_edges": exact.get("edges"),
        "input_sha256": case_hash,
    }


def run(args):
    """월·일·시간·구·요청 수 조합마다 사례를 만들어 처리하고 요약과 manifest를 저장한다."""
    started = time.perf_counter()

    # 입력/출력 경로를 정하고 출력 폴더를 만든다.
    root = args.workspace / "origin_data"
    output = args.output_dir or args.workspace / "outputs"
    output.mkdir(parents=True, exist_ok=True)

    # 월별 이용건수 순위를 저장하고, 대상 월을 정한다(미지정 시 이용건수 최대 월).
    ranking, sources = monthly_ranking(root)
    ranking.to_csv(output / "monthly_ranking.csv", index=False, encoding="utf-8-sig")
    months = args.months.split(",") if args.months else ranking.month.head(1).tolist()

    # 실행 조건과 환경을 기록할 manifest를 초기화한다.
    manifest = {
        "parameters": {k: str(v) if isinstance(v, Path) else v for k, v in vars(args).items()},
        "selected_months": months,
        "monthly_sources": sources,
        "environment": {"python": platform.python_version(), "platform": platform.platform()},
        "sampling_scope": "peak-month and peak-day stress sample; not representative annual sample",
        "qpu_executed": False,
        "sources": [],
        "failures": [],
    }
    rows = []

    # 월별 반복
    for month in months:
        # 해당 월의 일별 순위를 저장하고, 이용건수 상위 날짜를 고른다.
        daily = daily_ranking(root, month)
        daily.to_csv(output / f"daily_ranking_{month}.csv", index=False, encoding="utf-8-sig")
        dates = daily.date.head(args.days).tolist()
        print(f"[load] {month} selected dates={dates}", flush=True)

        # 대여소, 이용 기록, 재고 자료를 불러온다.
        stations, station_source = load_stations(root, month)
        cache_dir = args.workspace / "outputs" / "cache"
        trips, trip_sources = load_trips(root, month, dates, cache_dir)
        inventory, inventory_source = load_inventory(root, month, dates, cache_dir)
        manifest["sources"].extend([station_source, inventory_source, *trip_sources])

        # 자료 품질 지표(무효·중복 이용 기록 수 등)를 기록한다.
        manifest.setdefault("quality", []).append({
            "month": month,
            "loaded_trips": len(trips),
            "invalid_trip_rows": int((~trips.valid).sum()),
            "duplicate_observed_trips": int(
                trips.duplicated(["bike_id", "start", "end", "origin", "destination"]).sum()),
            "station_metadata_date_proxy": station_source["path"],
        })

        # 날짜별 반복
        for day in dates:
            # 그날의 유효한 이용 기록에 시간대와 출발 대여소의 구를 붙인다.
            day_trips = trips.loc[trips.valid & trips.start.dt.strftime("%Y-%m-%d").eq(day)].copy()
            day_trips["hour"] = day_trips.start.dt.hour
            day_trips["district"] = day_trips.origin.map(stations.district)

            # 이용이 많은 상위 시간대를 고른다.
            peak_hours = day_trips.groupby("hour").size().nlargest(args.hours).index

            # 시간대별 반복
            for hour in peak_hours:
                # 그 시간대에 이용이 많은 상위 구를 고른다.
                districts = (day_trips.loc[day_trips.hour.eq(hour)]
                             .groupby("district").size().nlargest(args.districts).index)

                # 구별 반복
                for district in districts:
                    # 요청 수 한도별 반복
                    for limit in [int(x) for x in args.request_limits.split(",")]:
                        # 사례를 생성한다.
                        case = create_case(
                            trips, inventory, stations, day, int(hour), district, limit,
                            args.window_minutes, args.radius_m, args.alternatives)

                        # 선택한 재고 모델에 따라 사례 변형을 만든다.
                        variants = []
                        if "events" in args.inventory_models.split(","):
                            variants.append(case)
                        if "checkpoints" in args.inventory_models.split(","):
                            variants.append(checkpoint_variant(case, inventory))

                        # 변형별로 처리한다.
                        for variant in variants:
                            folder = output / "cases" / variant["case_id"]
                            print(f"[case] {variant['case_id']} "
                                  f"candidates={len(variant['candidates'])}",
                                  flush=True)
                            try:
                                result = process_case(
                                    variant, folder, args.time_limit, args.exact_candidate_limit)
                                rows.append(result)
                                print(f"  graph={result['graph_status']} "
                                      f"operating={result['operational_status']}"
                                      f" gap={result['operational_gap']}", flush=True)
                            except Exception as error:
                                # 한 사례가 실패해도 전체는 계속 진행하고 실패만 기록한다.
                                manifest["failures"].append(
                                    {"case_id": variant["case_id"], "error": repr(error)})
                                print(f"  FAILED {type(error).__name__}: {error}", flush=True)

                            # 사례마다 중간 결과를 저장해 도중에 멈춰도 보존되게 한다.
                            pd.DataFrame(rows).to_csv(
                                output / "summary.csv", index=False, encoding="utf-8-sig")
                            dump_json(output / "manifest.json", manifest)

    # 전체 실행 통계를 manifest에 기록한다.
    manifest["wall_seconds"] = time.perf_counter() - started
    manifest["completed_cases"] = len(rows)
    manifest["graph_timeouts"] = sum(r["graph_status"] == "limit_reached" for r in rows)
    manifest["operational_timeouts"] = sum(r["operational_status"] == "limit_reached" for r in rows)

    # 제한시간 내 못 푼 사례가 있는지에 따라 난이도 결론 문구를 정한다.
    manifest["hardness_conclusion"] = (
        "Budget-relative unresolved cases found; "
        "graph and operational labels must be distinguished."
        if manifest["graph_timeouts"] or manifest["operational_timeouts"] else
        "No budget-relative hard cases found. Do not claim classical intractability.")
    dump_json(output / "manifest.json", manifest)

    # 핵심 통계를 한 줄 JSON으로 출력한다.
    print(json.dumps({k: manifest[k] for k in ("completed_cases", "graph_timeouts",
                                               "operational_timeouts", "wall_seconds")}),
          flush=True)

    # 완료된 사례가 하나도 없으면 오류로 종료한다.
    if not rows:
        raise RuntimeError("완성된 사례 없음. manifest failures를 확인하세요.")


def parse_args():
    """명령줄 인자를 정의하고 값의 범위를 검증한다."""
    parser = argparse.ArgumentParser(description=__doc__)

    # 경로 관련 인자
    parser.add_argument("--workspace", type=Path, default=Path(__file__).resolve().parent)
    parser.add_argument("--months", default="", help="빈 값은 이용건수 최대 월. 예: 202509,202512")
    parser.add_argument("--output-dir", type=Path, default=None)

    # 사례 선택 범위 인자
    parser.add_argument("--days", type=int, default=2)
    parser.add_argument("--hours", type=int, default=2)
    parser.add_argument("--districts", type=int, default=1)
    parser.add_argument("--request-limits", default="2,40,200,600")

    # 사례 생성 조건 인자
    parser.add_argument("--window-minutes", type=int, default=60)
    parser.add_argument("--radius-m", type=float, default=500)
    parser.add_argument("--alternatives", type=int, default=2)

    # 풀이 조건 인자
    parser.add_argument("--time-limit", type=float, default=5.0)
    parser.add_argument("--exact-candidate-limit", type=int, default=16)
    parser.add_argument("--inventory-models", default="events,checkpoints")
    args = parser.parse_args()

    # 재고 모델 값이 허용된 것인지 검증한다.
    if set(args.inventory_models.split(",")) - {"events", "checkpoints"}:
        parser.error("inventory-models: events,checkpoints 중 선택하세요.")

    # 횟수·시간·후보 수가 양수인지 검증한다.
    if min(args.days, args.hours, args.districts, args.window_minutes, args.alternatives) < 1:
        parser.error("횟수·시간·후보 수는 양수여야 합니다.")

    # 반경과 제한시간이 양수인지 검증한다.
    if args.radius_m <= 0 or args.time_limit <= 0:
        parser.error("반경·제한시간은 양수여야 합니다.")

    # 요청 수 한도가 모두 양수인지 검증한다.
    if any(int(x) < 1 for x in args.request_limits.split(",")):
        parser.error("요청 수는 양수여야 합니다.")
    return args


# 스크립트로 직접 실행될 때만 진입한다.
if __name__ == "__main__":
    run(parse_args())
