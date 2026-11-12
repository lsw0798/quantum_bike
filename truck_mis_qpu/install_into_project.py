#!/usr/bin/env python3
"""완성된 코드만 사용자가 지정한 프로젝트의 새 폴더에 설치한다. QPU를 실행하지 않는다."""
from pathlib import Path
import shutil
import tempfile


def main() -> None:
    source = Path(__file__).resolve().parent
    project = Path("/Users/leeseungwoo/Desktop/backup/lsw2026/quantum_bike")
    target = project / "truck_mis_qpu"
    if source == target:
        print(f"이미 지정 위치에 설치되어 있습니다: {target}")
        return
    if target.exists():
        raise FileExistsError(f"기존 폴더를 덮어쓰지 않습니다: {target}")
    if not (project / "origin_data").is_dir():
        raise FileNotFoundError(f"원본 데이터 폴더를 찾을 수 없습니다: {project / 'origin_data'}")
    staging = Path(tempfile.mkdtemp(prefix=".truck-mis-install-", dir=project))
    try:
        for path in source.iterdir():
            if path.name in {".venv", "outputs", "__pycache__", ".pytest_cache", ".env"}:
                continue
            if path.is_dir():
                shutil.copytree(path, staging / path.name, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
            elif path.is_file():
                shutil.copy2(path, staging / path.name)
        staging.rename(target)
        print(f"설치 완료: {target}")
        print(f"실행: {target / 'run_qpu.command'}")
    finally:
        if staging.exists():
            shutil.rmtree(staging)


if __name__ == "__main__":
    main()
