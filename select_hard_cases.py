"""명시한 summary.csv의 실제 저장 사례만 구조 감사한다. 풀이기·QPU 실행 없음.

예: python select_hard_cases.py --summaries outputs/summary.csv outputs/stress/summary.csv
"""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
import csv
from datetime import datetime, timezone
import json
import math
from pathlib import Path
import platform

from bike_research.hardness import audit_case, read_json, sha256_bytes


def _write_json(path, document):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(document, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")


def _size_bin(nodes):
    if nodes <= 0:
        return "0"
    lower = 1 << (nodes.bit_length() - 1)
    return f"{lower}-{2 * lower - 1}"


def run_selection(summaries, output_dir, policy_path):
    """원래 결과는 보존하고 별도 폴더에 정책·입력 해시와 모든 판정을 남긴다."""
    summaries = [Path(path).resolve() for path in summaries]
    output_dir, policy_path = Path(output_dir).resolve(), Path(policy_path).resolve()
    policy_raw = policy_path.read_bytes()
    policy = read_json(policy_raw)
    if not isinstance(policy, dict) or not isinstance(policy.get("structural_screen", {}), dict):
        raise ValueError("Policy and structural_screen must be objects")
    seconds = policy.get("structure_scan_seconds", 5)
    count = policy.get("shortlist_per_size_bin", 3)
    if not isinstance(seconds, (int, float)) or isinstance(seconds, bool) or not math.isfinite(seconds) or seconds < 0:
        raise ValueError("structure_scan_seconds must be finite and nonnegative")
    if not isinstance(count, int) or isinstance(count, bool) or count < 1:
        raise ValueError("shortlist_per_size_bin must be a positive integer")
    if policy.get("size_bin", "powers_of_two") != "powers_of_two":
        raise ValueError("Only powers_of_two size bins are supported")
    if len(set(summaries)) != len(summaries):
        raise ValueError("Duplicate summary paths")
    if any(output_dir == summary.parent or output_dir == summary.parent / "cases" for summary in summaries):
        raise ValueError("Use a separate output directory")
    output_dir.mkdir(parents=True, exist_ok=True)
    policy_hash = sha256_bytes(policy_raw)
    audits, inputs = [], []
    for summary in summaries:
        try:
            raw = summary.read_bytes()
            inputs.append({"path": str(summary), "sha256": sha256_bytes(raw)})
            rows = list(csv.DictReader(raw.decode("utf-8-sig").splitlines()))
            if not rows:
                raise ValueError("Summary contains no cases")
        except (OSError, ValueError) as error:
            audits.append({"case_id": None, "classification": "invalid_input",
                           "summary_path": str(summary), "errors": [str(error)],
                           "warnings": [], "structural_candidate_eligible": False,
                           "full_operational_model_eligible": False})
            continue
        seen = set()
        for row_number, row in enumerate(rows, start=2):
            case_id = row.get("case_id") or ""
            folder = summary.parent / "cases" / case_id
            if not case_id or Path(case_id).name != case_id or case_id in {".", ".."} or case_id in seen:
                audit = {"case_id": case_id, "classification": "invalid_input",
                         "errors": ["Invalid or duplicate case_id in summary"], "warnings": [],
                         "structural_candidate_eligible": False, "full_operational_model_eligible": False}
            else:
                audit = audit_case(folder, row, policy)
            seen.add(case_id)
            audit.update(summary_path=str(summary), summary_row=row_number,
                         run_group=summary.parent.name, policy_sha256=policy_hash)
            audits.append(audit)
    # 잔여 성분과 요청 간 결합은 선별 순서일 뿐, 고난도 판정 점수가 아니다.
    bins = defaultdict(list)
    for audit in audits:
        if audit["structural_candidate_eligible"]:
            audit["size_bin"] = _size_bin(audit["reduction"]["largest_residual_component"])
            bins[audit["size_bin"]].append(audit)
    shortlisted = []
    for bin_name in sorted(bins, key=lambda value: int(value.split("-")[0])):
        ranked = sorted(bins[bin_name], key=lambda item: (
            -item["reduction"]["largest_residual_component"],
            -(item["metrics"]["cross_request_edges"] or 0), item["case_id"], item["summary_path"]))
        shortlisted.extend(ranked[:count])
    selected_keys = {(a["summary_path"], a["summary_row"]) for a in shortlisted}
    report = []
    for index, audit in enumerate(audits):
        identifier = f"{index:04d}_{sha256_bytes((audit['summary_path'] + '|' + str(audit['case_id'])).encode())[:12]}"
        audit["shortlisted"] = (audit["summary_path"], audit.get("summary_row")) in selected_keys
        audit["audit_file"] = f"cases/{identifier}.json"
        _write_json(output_dir / audit["audit_file"], audit)
        metrics, reduction = audit.get("metrics", {}), audit.get("reduction", {})
        historical, operating = audit.get("historical_graph", {}), audit.get("operational", {})
        report.append({"case_id": audit["case_id"], "run_group": audit.get("run_group"),
            "classification": audit["classification"], "shortlisted": audit["shortlisted"],
            "representation": audit.get("representation"),
            "full_operational_model_eligible": audit["full_operational_model_eligible"],
            "baseline_feasible": audit.get("baseline_feasible"),
            "baseline_selection_validated": audit.get("baseline_selection_validated"),
            "structural_evidence_complete": audit.get("structural_evidence_complete"),
            "certificate_type": (audit.get("certificate") or {}).get("type"),
            "certificate_verified": audit.get("certificate_verified", False),
            "certificate_objective": (audit.get("certificate") or {}).get("objective"),
            **metrics, **reduction,
            "historical_graph_status": historical.get("status"),
            "historical_graph_operational_feasible": historical.get("operational_feasible"),
            "historical_operational_status": operating.get("status"),
            "historical_operational_incumbent_validated": operating.get("incumbent_validated"),
            "case_sha256": audit.get("case_sha256"), "graph_sha256": audit.get("graph_sha256"),
            "policy_sha256": policy_hash, "summary_path": audit["summary_path"],
            "audit_file": audit["audit_file"], "errors": " | ".join(audit["errors"]),
            "warnings": " | ".join(audit["warnings"])})
    fields = list(dict.fromkeys(key for row in report for key in row))
    with (output_dir / "report.csv").open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader()
        writer.writerows(report)
    _write_json(output_dir / "selection.json", {
        "policy_sha256": policy_hash, "structural_candidates": [
            {"case_id": a["case_id"], "summary_path": a["summary_path"],
             "audit_file": a["audit_file"], "size_bin": a["size_bin"],
             "full_operational_model_eligible": a["full_operational_model_eligible"]}
            for a in shortlisted],
        "interpretation": "Structural candidates require new repeated classical confirmation; none is labeled hard.",
        "selection_rules": {"size_bin": "powers_of_two", "shortlist_per_size_bin": count,
                            "size_metric": "largest_residual_component",
                            "order": ["residual_largest_component_desc", "cross_request_edges_desc", "case_id_then_source_asc"]}})
    manifest = {
        "schema_version": 1, "created_at_utc": datetime.now(timezone.utc).isoformat(),
        "policy": policy, "policy_path": str(policy_path), "policy_sha256": policy_hash,
        "summary_inputs": inputs, "case_count": len(audits),
        "classification_counts": dict(Counter(a["classification"] for a in audits)),
        "certificate_counts": dict(Counter(a["certificate"]["type"] for a in audits if a.get("certificate"))),
        "structural_candidate_count": len(shortlisted), "qpu_executed": False,
        "classical_solvers_executed": False,
        "environment": {"python": platform.python_version(), "platform": platform.platform()},
        "interpretation": "Input-bound mathematical certificates plus historical sidecar audit; no new runtime comparison.",
    }
    _write_json(output_dir / "manifest.json", manifest)
    return manifest


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--summaries", type=Path, nargs="+", required=True)
    parser.add_argument("--policy", type=Path, default=Path(__file__).resolve().parent / "configs/hardness_policy_v1.json")
    parser.add_argument("--output-dir", type=Path, default=Path(__file__).resolve().parent / "outputs/hardness_v1")
    args = parser.parse_args()
    manifest = run_selection(args.summaries, args.output_dir, args.policy)
    print(json.dumps({key: manifest[key] for key in
                      ("case_count", "classification_counts", "certificate_counts", "structural_candidate_count")},
                     ensure_ascii=False))


if __name__ == "__main__":
    main()
