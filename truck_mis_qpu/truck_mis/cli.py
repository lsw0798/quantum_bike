"""데이터 준비, QPU 실행, 기존 작업 조회를 연결하는 명령행 인터페이스."""
from __future__ import annotations

import argparse
import csv
from pathlib import Path
import os
import sys

from .data import load_stations
from .domain import build_case, evaluate_counts
from .settings import load_settings, public_settings
from .storage import digest, read_json, write_json


def _connection(settings: dict):
    """노트북과 동일한 환경변수 이름 사용. 인증값을 파일·로그에 복제하지 않는다."""
    from dotenv import dotenv_values
    from .backend import create_connection

    values = {**dotenv_values(settings["resolved_paths"]["env_file"]), **os.environ}
    keys = ("PASQAL_USERNAME", "PASQAL_PASSWORD", "PASQAL_PROJECT_ID")
    missing = [key for key in keys if not values.get(key)]
    if missing:
        raise ValueError("상위 프로젝트 .env 또는 환경변수에 필요한 항목: " + ", ".join(missing))
    try:
        return create_connection(username=values[keys[0]], password=values[keys[1]],
                                 project_id=values[keys[2]], region=settings["qpu"]["region"])
    except Exception as error:
        # 서버 오류 원문은 인증정보를 포함할 수 있어 콘솔에 표시하지 않는다.
        raise RuntimeError(f"Pasqal 연결 실패({type(error).__name__}). 계정·프로젝트·네트워크를 확인하세요.") from None


def _check_manifest(directory: Path) -> tuple[dict, dict]:
    manifest = read_json(directory / "manifest.json")
    case = read_json(directory / "case.json")
    expected = digest({"case": case, "qpu": manifest["settings"]["qpu"], "pipeline_version": "1.0.0"})
    if (digest(case) != manifest["case_hash"] or expected != manifest["manifest_hash"]
            or manifest["node_order"] != [t["task_id"] for t in case["tasks"]]):
        raise ValueError("사례·설정·노드 순서가 저장된 제출 입력 해시와 일치하지 않습니다.")
    return manifest, case


def prepare(settings: dict, *, resume_existing: bool = False):
    from .backend import prepare_program

    print("[1/4] 원본 재고·좌표 읽기 및 과거 목표 재고 계산", flush=True)
    stations, metadata = load_stations(settings["data"], settings["resolved_paths"]["data_dir"])
    case = build_case(stations, settings["model"], metadata)
    # 같은 입력·장치·측정 설정이면 같은 디렉터리를 사용해 재실행 중복 제출을 막는다.
    manifest_hash = digest({"case": case, "qpu": settings["qpu"], "pipeline_version": "1.0.0"})
    directory = settings["resolved_paths"]["output_dir"] / manifest_hash[:20]
    directory.mkdir(parents=True, exist_ok=True)
    if (directory / "manifest.json").exists():
        previous, _ = _check_manifest(directory)
        if previous["manifest_hash"] != manifest_hash:
            raise ValueError("실행 디렉터리의 입력 해시가 일치하지 않습니다.")
    else:
        write_json(directory / "case.json", case)
        write_json(directory / "manifest.json", {"manifest_hash": manifest_hash,
                   "case_hash": digest(case), "settings": public_settings(settings),
                   "node_order": [t["task_id"] for t in case["tasks"]]})
    print(f"후보 {case['candidate_audit']['enumerated_tasks']}개 → 실험 입력 {len(case['tasks'])}개, "
          f"충돌 {len(case['edges'])}개", flush=True)
    if resume_existing and any((directory / name).exists()
                               for name in ("qpu_submission.json", "emulator_submission.json")):
        print("기존 제출 기록 발견: 배치·펄스 재생성 없이 같은 원격 작업을 조회합니다.", flush=True)
        return case, None, directory, manifest_hash
    print("[2/4] 논리 그래프를 원자 좌표에 배치하고 MIS 에너지 조건 검증", flush=True)
    try:
        prepared = prepare_program(
            [t["task_id"] for t in case["tasks"]],
            [(e["source"], e["target"]) for e in case["edges"]], settings["qpu"])
    except Exception as error:
        write_json(directory / "preparation_failure.json", {
            "stage": "embedding", "error_type": type(error).__name__,
            "message": str(error), "qpu_submitted": False,
            "graph_was_not_changed": True,
        })
        raise
    # 구체적인 배치와 검증 기록은 backend가 제출 영수증에도 보관한다.
    write_json(directory / "preparation.json", prepared.embedding_report)
    write_json(directory / "coordinates.json", {
        "coordinates": {node: [float(x) for x in xy]
                        for node, xy in zip(prepared.node_ids, prepared.coordinates)}})
    print(f"입력 및 검증 결과: {directory}", flush=True)
    return case, prepared, directory, manifest_hash


def _export(case: dict, counts: dict, directory: Path) -> None:
    # 형식 검증 전에 정수 변환하면 손상된 실수/불리언 빈도를 숨길 수 있다.
    evaluation = evaluate_counts(case, counts)
    write_json(directory / "raw_counts.json", counts)
    if (directory / "manifest.json").exists():
        execution = read_json(directory / "manifest.json")["settings"]["qpu"]
        expected_shots = execution["shots"]
        evaluation["shots_requested"] = expected_shots
        evaluation["shot_count_matches_request"] = evaluation["shots_received"] == expected_shots
        evaluation["device_name"] = execution["device_name"]
        evaluation["backend"] = "EMULATOR" if execution["device_name"] == "EMU_FRESNEL" else "QPU"
    write_json(directory / "evaluation.json", evaluation)
    best = evaluation["best_observed_feasible"]
    if best is not None:
        write_json(directory / "best_plan.json", best["operational"])
        fields = ["task_id", "pickup_id", "delivery_id", "quantity", "start_min", "end_min", "distance_km"]
        with (directory / "best_plan.csv").open("w", encoding="utf-8-sig", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=fields)
            writer.writeheader()
            writer.writerows(best["operational"]["schedule"])
    else:
        for name in ("best_plan.json", "best_plan.csv"):
            (directory / name).unlink(missing_ok=True)
    print(f"원시 실행 가능률: {evaluation['raw_feasible_rate']:.1%} "
          f"({evaluation['feasible_shots']}/{evaluation['shots_received']})")
    if best:
        print(f"관측된 최선의 실행 가능 계획: {best['operational']['selected_task_count']}개 작업, "
              f"{best['operational']['bikes_delivered']}대 운반 (최적성 증명 없음)")
    else:
        print("실행 가능한 표본이 없습니다. 실패 표본을 그대로 저장했습니다.")


def _collect(job, case: dict, directory: Path, timeout: float) -> None:
    from .backend import fetch_results
    try:
        counts = fetch_results(job, timeout=timeout)
    except Exception as error:
        print(f"결과가 아직 준비되지 않았거나 조회 실패({type(error).__name__}). 새 작업은 제출하지 않습니다.")
        print(f"나중에 실행: python run.py fetch --run-dir '{directory}'")
        return
    _export(case, counts, directory)


def run(settings: dict) -> None:
    from .backend import submit_or_resume, recover_job
    case, prepared, directory, manifest_hash = prepare(settings, resume_existing=True)
    connection = _connection(settings)
    print(f"[3/4] 장치 사양 검증 및 {settings['qpu']['device_name']} 제출/기존 작업 재조회", flush=True)
    try:
        if prepared is None:
            job = recover_job(connection, directory, manifest_hash)
        else:
            job = submit_or_resume(prepared, connection, settings["qpu"]["device_name"],
                                   settings["qpu"]["shots"], directory, manifest_hash)
    except Exception as error:
        # 자세한 비밀정보 없는 단계·상태는 backend의 영수증에 기록된다.
        raise RuntimeError(f"원격 제출/복구 중단({type(error).__name__}). "
                           f"{directory}의 제출 기록을 확인하세요.") from None
    print("[4/4] 원시 측정 결과 조회 및 운영 계획 복원", flush=True)
    _collect(job, case, directory, settings["qpu"]["poll_timeout_seconds"])


def fetch(settings: dict, directory: Path) -> None:
    from .backend import recover_job
    directory = directory.resolve()
    manifest, case = _check_manifest(directory)
    # 조회 대상 장치와 결과 해석은 제출 당시 입력을 기준으로 고정한다.
    connection = _connection(settings)
    try:
        job = recover_job(connection, directory, manifest["manifest_hash"])
    except Exception as error:
        raise RuntimeError(f"기존 원격 작업 조회 실패({type(error).__name__}). 제출 영수증을 확인하세요.") from None
    _collect(job, case, directory, settings["qpu"]["poll_timeout_seconds"])


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="따릉이 트럭 작업 선택 — Pasqal QPU/에뮬레이터 비가중 MIS")
    parser.add_argument("command", choices=["prepare", "run", "fetch"], help="준비만 / 설정한 원격 장치 실행 / 기존 결과 조회")
    parser.add_argument("--config", type=Path, default=Path(__file__).resolve().parents[1] / "config.toml")
    parser.add_argument("--project-root", help="원본 데이터와 .env가 있는 프로젝트 경로")
    parser.add_argument("--run-dir", type=Path, help="fetch로 조회할 기존 실행 결과 폴더")
    args = parser.parse_args(argv)
    if args.run_dir is not None and args.command != "fetch":
        parser.error("--run-dir는 fetch 명령에만 사용할 수 있습니다.")
    try:
        settings = load_settings(args.config, args.project_root)
        if args.command == "prepare":
            prepare(settings)
            print("로컬 준비 완료. 원격 장치 접속·제출은 수행하지 않았습니다.")
        elif args.command == "run":
            run(settings)
        else:
            if args.run_dir is None:
                parser.error("fetch에는 --run-dir가 필요합니다.")
            fetch(settings, args.run_dir)
    except (ValueError, FileNotFoundError, RuntimeError) as error:
        print(f"중단: {error}", file=sys.stderr)
        return 1
    return 0
