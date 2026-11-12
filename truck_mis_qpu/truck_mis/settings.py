"""TOML 설정과 경로를 한 곳에서 해석하고 잘못된 값을 조기에 차단한다."""
from __future__ import annotations

from copy import deepcopy
from hashlib import sha256
from pathlib import Path
import math
import tomllib


def positive(value: object, name: str, *, integer: bool = False, zero: bool = False) -> None:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError(f"{name}: 유한한 숫자를 입력하세요.")
    if value < 0 or (not zero and value == 0) or (integer and not isinstance(value, int)):
        raise ValueError(f"{name}: 허용 범위와 정수 여부를 확인하세요.")


def load_settings(path: Path, project_override: str | None = None) -> dict:
    path = path.resolve()
    with path.open("rb") as f:
        result = tomllib.load(f)
    def reject_secrets(value: dict) -> None:
        for key, item in value.items():
            if any(word in key.lower() for word in ("password", "secret", "token", "username", "credential")):
                raise ValueError("계정정보는 TOML이 아닌 상위 .env 또는 환경변수에 보관하세요.")
            if isinstance(item, dict):
                reject_secrets(item)
    reject_secrets(result)
    for section in ("paths", "data", "model", "qpu"):
        if section not in result:
            raise ValueError(f"설정 섹션 [{section}]이 없습니다.")
    paths = result["paths"]
    project = Path(project_override or paths["project_root"]).expanduser()
    if not project.is_absolute():
        project = path.parent / project
    result["resolved_paths"] = {
        "project_root": project.resolve(),
        "data_dir": (project / paths["data_dir"]).resolve(),
        "env_file": (project / paths["env_file"]).resolve(),
        "output_dir": (path.parent / paths["output_dir"]).resolve(),
    }
    m, q = result["model"], result["qpu"]
    for key in ("radius_km", "horizon_min", "start_step_min", "truck_speed_kmh"):
        positive(m[key], f"model.{key}")
    for key in ("truck_capacity", "bikes_per_task", "max_tasks"):
        positive(m[key], f"model.{key}", integer=True)
    for key in ("handling_min_per_bike", "stop_overhead_min"):
        positive(m[key], f"model.{key}", zero=True)
    for key in ("history_days", "minimum_history_days"):
        positive(result["data"][key], f"data.{key}", integer=True)
    if m["bikes_per_task"] > m["truck_capacity"]:
        raise ValueError("bikes_per_task가 truck_capacity보다 큽니다.")
    if m["max_tasks"] > 20:
        raise ValueError("현재 직접 배치 파일럿 구현은 max_tasks ≤ 20으로 제한합니다.")
    positive(q["shots"], "qpu.shots", integer=True)
    positive(q["duration"], "qpu.duration")
    positive(q.get("amplitude_factor", 1.0), "qpu.amplitude_factor")
    positive(q["maxiter"], "qpu.maxiter", integer=True)
    positive(q["poll_timeout_seconds"], "qpu.poll_timeout_seconds")
    positive(q["device_max_duration_ratio"], "qpu.device_max_duration_ratio")
    if not 0 < q["device_max_duration_ratio"] <= 1:
        raise ValueError("device_max_duration_ratio는 0 초과, 1 이하입니다.")
    for section, name in ((m, "candidate_seed"), (q, "seed")):
        positive(section[name], name, integer=True, zero=True)
    if q.get("coordinates_path"):
        coordinate_file = Path(q["coordinates_path"]).expanduser()
        q["coordinates_path"] = str((path.parent / coordinate_file).resolve())
        q["coordinates_sha256"] = sha256(Path(q["coordinates_path"]).read_bytes()).hexdigest()
    result["config_path"] = path
    return result


def public_settings(settings: dict) -> dict:
    """인증값은 설정에 넣지 않는다. 경로만 문자열로 바꾸어 보관한다."""
    result = deepcopy(settings)
    result["resolved_paths"] = {k: str(v) for k, v in result["resolved_paths"].items()}
    result["config_path"] = str(result["config_path"])
    return result
