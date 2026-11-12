r"""완료된 RD1 실행의 난도 원인을 사후 진단한다. 풀이기와 QPU는 실행하지 않는다.

사용 예
-------
.venv-classical\Scripts\python.exe analyze_rd1_structure.py --input outputs/rd1_main

completion.json과 모든 사례 완료 기록이 일치해야 시작한다. 후보가 있는 요청 수는
요청당 최대 한 대안을 선택하는 운영 모형의 상한이다. 검산된 해가 그 상한에
도달하면 원모형의 최적성을 바로 인증할 수 있다. 상한에 못 미친다는 사실만으로
재고가 최적값을 줄였다고 주장하지 않고, 저장된 최적성 증거도 함께 요구한다.
"""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
import csv
import gc
import hashlib
import json
import math
from pathlib import Path
import statistics
import time


def read_json(path, max_bytes=512 * 1024 * 1024):
    """큰 파일을 무제한 읽지 않으며 원본 JSON 해시를 동시에 얻는다."""
    path = Path(path)
    if path.stat().st_size > max_bytes:
        raise ValueError(f"진단 메모리 보호 한도를 넘는 파일: {path}")
    raw = path.read_bytes()
    return json.loads(raw), hashlib.sha256(raw).hexdigest()


def require_complete(campaign):
    """실행 중 자료나 일부 사례만으로 전체 비율을 계산하지 못하도록 막는다."""
    campaign = Path(campaign)
    if not (campaign / "completion.json").is_file():
        raise ValueError("completion.json이 없습니다. 주 캠페인 완료 후 실행하십시오.")
    manifest, manifest_hash = read_json(campaign / "manifest.json")
    completion, completion_hash = read_json(campaign / "completion.json")
    expected = manifest.get("expected_variant_cases")
    if not isinstance(expected, int) or expected < 1:
        raise ValueError("manifest의 예상 사례 수가 유효하지 않습니다.")
    if (completion.get("expected_cases") != expected
            or completion.get("completed_cases") != expected
            or completion.get("failed_cases") != 0):
        raise ValueError("완료 수·예상 수·실패 수가 전체 완료 조건에 맞지 않습니다.")
    summaries = sorted((campaign / "cases").glob("*/result_summary.json"))
    if len(summaries) != expected:
        raise ValueError("실제 사례 완료 JSON 수가 완료 표시와 다릅니다.")
    if list((campaign / "cases").glob("*/execution_error.json")):
        raise ValueError("실행 오류 기록이 남아 있습니다. 완전한 캠페인만 진단합니다.")
    if manifest.get("qpu_executed") is not False or completion.get("qpu_executed") is not False:
        raise ValueError("이번 진단은 QPU 미실행 캠페인만 대상으로 합니다.")
    return manifest, summaries, {"manifest_sha256": manifest_hash,
                                 "completion_sha256": completion_hash,
                                 "expected_cases": expected}


def validate_witness(case, selection):
    """저장된 선택을 요청·자원·선형 제약에서 독립 검산한다. 최적화는 하지 않는다."""
    selected = set(selection)
    candidates = {str(c["id"]): c for c in case.get("candidates", [])}
    if len(candidates) != len(case.get("candidates", [])):
        raise ValueError("중복 후보 ID가 있습니다.")
    reasons = []
    if len(selected) != len(selection) or not selected <= candidates.keys():
        reasons.append("duplicate_or_unknown_selection")
    counts = Counter(str(candidates[v]["request_id"]) for v in selected if v in candidates)
    if any(count > 1 for count in counts.values()):
        reasons.append("request_exclusivity")
    if any(str(p["source"]) in selected and str(p["target"]) in selected
           for p in case.get("resource_conflicts", [])):
        reasons.append("resource_conflict")
    tight_rows = 0
    for row in case.get("constraints", []):
        value = sum(float(a) for v, a in row["coefficients"].items() if str(v) in selected)
        lower, upper = row.get("lower"), row.get("upper")
        if not math.isfinite(value):
            raise ValueError("유한하지 않은 제약 활동량입니다.")
        if ((lower is not None and value < lower - 1e-7)
                or (upper is not None and value > upper + 1e-7)):
            reasons.append("linear_constraint")
        if ((lower is not None and abs(value - lower) <= 1e-7)
                or (upper is not None and abs(value - upper) <= 1e-7)):
            tight_rows += 1
    return {"feasible": not reasons, "violation_kinds": sorted(set(reasons)),
            "tight_constraint_rows": tight_rows}


def _normal_run(run):
    return (run.get("status") in ("optimal", "limit_reached")
            and not run.get("budget_overrun", False)
            and run.get("fixed_budget_eligible", True))


def _solver_diagnostics(runs, algorithm):
    chosen = [r for r in runs if r.get("algorithm") == algorithm and _normal_run(r)]
    proof = [r.get("proof_seconds") for r in chosen
             if r.get("optimal") and r.get("proof_seconds") is not None]
    preprocess = [r.get("preprocessing", {}) for r in chosen if r.get("preprocessing")]
    sizes = {(p["before_nodes"], p["before_edges"], p["after_nodes"], p["after_edges"])
             for p in preprocess}
    if len(sizes) > 1:
        raise ValueError("동일 사례 반복 간 축약 전후 크기가 달라졌습니다.")
    return {"normal_runs": len(chosen),
            "median_solver_seconds": statistics.median(r["solver_seconds"] for r in chosen) if chosen else None,
            "median_proof_seconds": statistics.median(proof) if len(proof) == len(chosen) and chosen else None,
            "before_after_size": next(iter(sizes)) if sizes else None,
            "median_preprocessing_seconds": statistics.median(p["seconds"] for p in preprocess) if preprocess else None}


def diagnose_case(case, summary, benchmark):
    """상한 도달·재고 제한·미확정과 인코딩 오버헤드를 구분하는 순수 계산."""
    candidates = case.get("candidates", [])
    request_bound = len({str(c["request_id"]) for c in candidates})
    chosen = [str(v) for v in benchmark.get("selected_candidate_ids", [])]
    witness = validate_witness(case, chosen)
    has_result = bool(benchmark)
    q = len(chosen) if has_result and witness["feasible"] else None
    upper = benchmark.get("upper_bound")
    source_proven = bool(has_result and witness["feasible"]
                         and benchmark.get("optimality_proven")
                         and benchmark.get("lower_bound") == q == upper)
    bound_attained = q == request_bound if q is not None else False
    optimum_proven = source_proven or bound_attained
    kinds = {str(row.get("kind", "unspecified")) for row in case.get("constraints", [])}
    inventory_only = bool(kinds) and all("inventory" in kind for kind in kinds)
    resources = len(case.get("resource_conflicts", []))
    if source_proven and q < request_bound and not kinds and resources == 0:
        raise ValueError("요청 외 제약이 없는데 저장된 최적값이 요청 상한보다 작습니다.")
    generation_unavailable = summary.get("generation_status") in ("generation_limit", "data_ineligible")
    if generation_unavailable:
        category = "generation_unavailable"
    elif request_bound == 0:
        category = "zero_candidate_requests"
    elif not has_result or not witness["feasible"]:
        category = "no_validated_solution"
    elif bound_attained:
        category = "request_bound_certificate"
    elif optimum_proven and inventory_only and resources == 0:
        category = "inventory_limits_optimum"
    elif optimum_proven:
        category = "other_or_combined_constraints_limit_optimum"
    else:
        category = "unproven_below_request_bound"
    diagnostics = {name: _solver_diagnostics(benchmark.get("runs", []), name)
                   for name in ("highs_operational", "highs_graph", "cpsat_graph")}
    twin = diagnostics["highs_graph"]["before_after_size"] or diagnostics["cpsat_graph"]["before_after_size"]
    row = {key: summary.get(key) for key in ("case_id", "date", "hour", "district", "variant", "split",
           "generation_status", "encoding_status", "exact_nodes", "exact_edges", "candidate_nodes",
           "candidate_edges", "full_exact_runs", "residual_lcc", "benefit")}
    row.update(candidate_count=len(candidates), candidate_request_count=request_bound,
               request_bound_available=not generation_unavailable,
               validated_recommendations=q, optimum_proven=optimum_proven,
               request_bound_attained=bound_attained, structure_category=category,
               shortfall_from_request_bound=request_bound-q if q is not None else None,
               request_bound_fraction=q/request_bound if q is not None and request_bound else None,
               inventory_constraints_only=inventory_only, resource_conflict_count=resources,
               constraint_count=len(case.get("constraints", [])),
               witness_feasible=witness["feasible"] if has_result else None,
               tight_constraint_rows=witness["tight_constraint_rows"] if has_result else None,
               source_optimal_but_exact_graph_unavailable=optimum_proven and summary.get("encoding_status") != "exact",
               auxiliary_expansion_ratio=summary.get("exact_nodes")/len(candidates)
               if candidates and summary.get("exact_nodes") is not None else None,
               twin_before_nodes=twin[0] if twin else None, twin_before_edges=twin[1] if twin else None,
               twin_after_nodes=twin[2] if twin else None, twin_after_edges=twin[3] if twin else None,
               false_twin_node_reduction_fraction=1-twin[2]/twin[0] if twin and twin[0] else None,
               budget_overrun_runs=sum(bool(r.get("budget_overrun")) for r in benchmark.get("runs", [])))
    for name, values in diagnostics.items():
        row[f"{name}_normal_runs"] = values["normal_runs"]
        row[f"{name}_median_solver_seconds"] = values["median_solver_seconds"]
        row[f"{name}_median_proof_seconds"] = values["median_proof_seconds"]
        row[f"{name}_median_preprocessing_seconds"] = values["median_preprocessing_seconds"]
    return row


def aggregate(rows, variant="all", split="all"):
    selected = [r for r in rows if (variant == "all" or r["variant"] == variant)
                and (split == "all" or r["split"] == split)]
    nonempty = [r for r in selected if r["request_bound_available"] and r["candidate_request_count"] > 0]
    proven = [r for r in nonempty if r["optimum_proven"]]
    hits = [r for r in proven if r["request_bound_attained"]]
    return {"variant": variant, "split": split, "completed_cases": len(selected),
            "nonempty_cases": len(nonempty), "proven_optimum_nonempty_cases": len(proven),
            "request_bound_certificate_cases": len(hits),
            "request_bound_rate_among_proven_nonempty": len(hits)/len(proven) if proven else None,
            "request_bound_rate_among_all_nonempty": len(hits)/len(nonempty) if nonempty else None,
            "inventory_limited_cases": sum(r["structure_category"] == "inventory_limits_optimum" for r in selected),
            "source_proven_exact_graph_unavailable": sum(r["source_optimal_but_exact_graph_unavailable"] for r in selected),
            "category_counts": dict(Counter(r["structure_category"] for r in selected)),
            "max_candidates": max((r["candidate_count"] for r in selected), default=0),
            "max_exact_nodes": max((r["exact_nodes"] for r in selected if r["exact_nodes"] is not None), default=None),
            "max_twin_reduction_fraction": max((r["false_twin_node_reduction_fraction"] for r in selected
                                                if r["false_twin_node_reduction_fraction"] is not None), default=None)}


def choose_representatives(rows):
    """사후 설명용 대표 사례다. 실험 사례 선정이나 성능 평가 규칙을 변경하지 않는다."""
    choices = []
    selectors = [
        ("요청 상한에 도달한 큰 정확 그래프", lambda r: r["structure_category"] == "request_bound_certificate",
         lambda r: (r["exact_nodes"] or 0, r["candidate_count"])),
        ("재고 제약이 최적 추천 수를 제한한 사례", lambda r: r["structure_category"] == "inventory_limits_optimum",
         lambda r: (r["shortfall_from_request_bound"] or 0, r["candidate_count"])),
        ("원모형은 증명됐지만 정확 인코딩을 만들지 못한 사례", lambda r: r["source_optimal_but_exact_graph_unavailable"],
         lambda r: (r["candidate_count"], r["candidate_request_count"]))]
    used = set()
    for label, predicate, key in selectors:
        available = [r for r in rows if r["case_id"] not in used and predicate(r)]
        if available:
            chosen = sorted(available, key=lambda r: (key(r), r["case_id"]), reverse=True)[0]
            choices.append({"reason": label, **chosen})
            used.add(chosen["case_id"])
    return choices


def analyze(campaign, output=None):
    """완료 조건을 먼저 검사하고, 원자료·주 결과를 변경하지 않는 별도 보고서를 만든다."""
    campaign = Path(campaign).resolve()
    manifest, summaries, provenance = require_complete(campaign)
    output = Path(output).resolve() if output else campaign / "postanalysis_structure"
    if output.exists():
        raise ValueError("출력 경로가 이미 있습니다. 새로운 --output 경로를 사용하십시오.")
    started = time.perf_counter()
    rows, inputs, seen, windows = [], [], set(), defaultdict(set)
    variants = set(manifest["config"]["variants"])
    for path in summaries:
        summary, summary_hash = read_json(path)
        case, case_hash = read_json(path.parent / "case.json")
        if summary["case_id"] in seen or case["case_id"] != summary["case_id"]:
            raise ValueError(f"사례 ID 중복 또는 불일치: {path}")
        seen.add(summary["case_id"])
        if case["metadata"].get("campaign_config_hash") != manifest["config_hash"]:
            raise ValueError(f"캠페인 설정 해시가 다른 사례: {path}")
        for key in ("date", "hour", "district", "variant", "split"):
            if summary[key] != case["metadata"][key]:
                raise ValueError(f"사례와 요약의 {key} 불일치: {path}")
        if summary["candidates"] != len(case["candidates"]):
            raise ValueError(f"후보 수 불일치: {path}")
        windows[(summary["date"], summary["hour"], summary["district"])].add(summary["variant"])
        built = path.parent / "build_summary.json"
        if built.exists():
            build, _ = read_json(built)
            if build["case_sha256"] != case_hash:
                raise ValueError(f"원래 사례 파일의 해시가 변경됐습니다: {path}")
        benchmark_path = path.parent / "classical_benchmark.json"
        benchmark, benchmark_hash = read_json(benchmark_path) if benchmark_path.exists() else ({}, None)
        row = diagnose_case(case, summary, benchmark)
        row["case_folder"] = str(path.parent)
        rows.append(row)
        inputs.append({"folder": path.parent.name, "summary_sha256": summary_hash,
                       "case_sha256": case_hash, "benchmark_sha256": benchmark_hash})
        del case, benchmark
        gc.collect()
    if any(found != variants for found in windows.values()):
        raise ValueError("같은 실제 창의 primary/off 변형이 완성되지 않았습니다.")
    if len(windows) * len(variants) != len(rows):
        raise ValueError("동일한 실제 창·변형에 중복 사례가 있습니다.")
    groups = [aggregate(rows)]
    for variant in sorted(variants):
        groups.append(aggregate(rows, variant))
        for split in sorted({row["split"] for row in rows}):
            groups.append(aggregate(rows, variant, split))
    report = {"kind": "posthoc_explanation_not_case_selection", "qpu_executed": False,
              "solvers_executed": False, "campaign": str(campaign),
              "provenance": provenance, "input_files": inputs,
              "groups": groups, "representatives": choose_representatives(rows),
              "elapsed_seconds": time.perf_counter()-started,
              "interpretation": [
                  "요청 상한 도달은 검산된 운영 해와 요청당 최대1개 규칙으로 얻는 인증이다.",
                  "R=0 사례는 상한 도달 비율의 분모에서 제외한다.",
                  "생성 제한·자료 부적격 사례는 후보가 없는 정상 사례와 구분하고 도달률 분모에서 제외한다.",
                  "재고 제한 판정은 최적성 증명·q<R·재고 외 추가자원 제약 없음의 세 조건을 요구한다.",
                  "활성 제약 수 자체는 최적값을 줄였다는 증거가 아니다.",
                  "큰 보조 그래프·인코딩 실패는 원래 운영 문제의 고전 난도를 증명하지 않는다.",
                  "대표 사례는 사후 설명용이며 실험군이나 고난도 규칙을 변경하지 않는다."]}
    output.mkdir(parents=True)
    (output / "structure_diagnostics.json").write_text(json.dumps(report, ensure_ascii=False, indent=2,
                                                                 allow_nan=False), encoding="utf-8")
    with (output / "structure_cases.csv").open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
    lines = ["# RD1 완료 사례의 구조 사후진단", "",
             "이 진단은 저장된 결과와 선택의 타당성만 다시 검사했다. 풀이기와 QPU는 실행하지 않았다.", "",
             "| 변형 | 날짜 분할 | 완료 | 후보 요청 존재 | 최적 증명 | 요청 상한 도달 | 증명 사례 중 도달률 | 재고 제한 |",
             "|---|---|---:|---:|---:|---:|---:|---:|"]
    for group in groups:
        rate = group["request_bound_rate_among_proven_nonempty"]
        lines.append(f"| {group['variant']} | {group['split']} | {group['completed_cases']} | "
                     f"{group['nonempty_cases']} | {group['proven_optimum_nonempty_cases']} | "
                     f"{group['request_bound_certificate_cases']} | {rate:.1%} | {group['inventory_limited_cases']} |"
                     if rate is not None else
                     f"| {group['variant']} | {group['split']} | {group['completed_cases']} | {group['nonempty_cases']} | 0 | 0 | NA | {group['inventory_limited_cases']} |")
    lines.extend(["", "R은 후보가 하나 이상 있는 요청 수다. q=R인 실행 가능 해는 요청 배타성만으로 최적임을 인증한다. "
                  "q<R인 경우에는 최적성 증명과 추가 제약 종류를 함께 확인해야 재고 제한으로 해석할 수 있다.",
                  "", "## 대표 실제 사례", ""])
    for row in report["representatives"]:
        lines.extend([f"- **{row['reason']}**: {row['date']} {row['hour']:02d}시 {row['district']}, "
                      f"{row['variant']} / {row['split']}. 후보 {row['candidate_count']:,}개, "
                      f"후보 요청 R={row['candidate_request_count']:,}, 검산된 추천 q={row['validated_recommendations']}. "
                      f"정확 그래프 {row['exact_nodes']}노드, 쌍둥이 축약 후 {row['twin_after_nodes']}대표.", ""])
    lines.extend(["## 해석 범위", "", *[f"- {item}" for item in report["interpretation"]], ""])
    (output / "구조진단.md").write_text("\n".join(lines), encoding="utf-8")
    return {"output": str(output), "cases": len(rows), "groups": groups}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, default=Path(__file__).resolve().parent / "outputs/rd1_main")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    print(json.dumps(analyze(args.input, args.output), ensure_ascii=False, indent=2))
