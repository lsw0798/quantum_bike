"""실측 대여 가능 수량과 과거 재고로 정적 재배치 입력을 만든다.

원본을 수정하지 않으며 거치대 설치 수를 물리적 수용량으로 간주하지 않는다.
기본 목표 재고는 의사결정 시점 이전의 같은 시간대 재고 중앙값이다.
이는 수요를 추정한 최적 서비스 수준이 아닌, 재현 가능한 실험용 기준이다.
"""
from __future__ import annotations

import calendar
from collections import Counter, defaultdict
import csv
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
import hashlib
import math
from pathlib import Path
import re
from statistics import median
from typing import Any
from zoneinfo import ZoneInfo
import xml.etree.ElementTree as ET
import zipfile

SEOUL = ZoneInfo("Asia/Seoul")
XML_NS = {"m": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
INTEGER_ID = re.compile(r"\d+(?:\.0+)?\Z")
INVENTORY_FIELDS = {"일시", "대여소번호", "시간대", "거치대수량"}


def _station_id(value: Any) -> str | None:
    """00102와 102.0은 같은 번호로 묶되 다른 식별체계는 섞지 않는다."""
    text = str(value).strip()
    return str(int(Decimal(text))) if INTEGER_ID.fullmatch(text) else None


def _nonnegative_integer(value: Any) -> int | None:
    try:
        number = Decimal(str(value).strip())
        if number.is_finite() and number >= 0 and number == number.to_integral_value():
            return int(number)
    except (InvalidOperation, ValueError):
        pass
    return None


def _timestamp(value: str) -> datetime:
    try:
        moment = datetime.fromisoformat(value)
    except (TypeError, ValueError) as error:
        raise ValueError("snapshot_timestamp를 YYYY-MM-DDTHH:00:00 형식으로 지정하세요.") from error
    # 입력 시간대가 없으면 서울 현지 시각으로 해석한다.
    if moment.tzinfo is not None:
        moment = moment.astimezone(SEOUL).replace(tzinfo=None)
    if moment.minute or moment.second or moment.microsecond:
        raise ValueError("시간별 원본은 정시 스냅샷만 제공합니다. 분·초를 0으로 지정하세요.")
    return moment


def _record_source(path: Path) -> dict[str, Any]:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(8 * 1024 * 1024), b""):
            digest.update(block)
    return {"path": str(path.resolve()), "name": path.name,
            "bytes": path.stat().st_size, "sha256": digest.hexdigest()}


def _resolve(path: str, root: Path) -> Path:
    candidate = Path(path).expanduser()
    return candidate if candidate.is_absolute() else root / candidate


def _xlsx_rows(path: Path):
    """공공데이터의 첫 시트를 표준 라이브러리로 읽는다(엑셀 실행 불필요)."""
    with zipfile.ZipFile(path) as archive:
        strings: list[str] = []
        if "xl/sharedStrings.xml" in archive.namelist():
            tree = ET.fromstring(archive.read("xl/sharedStrings.xml"))
            strings = ["".join(t.text or "" for t in cell.findall(".//m:t", XML_NS))
                       for cell in tree.findall("m:si", XML_NS)]
        # 서울시 원본의 첫 시트 이름은 sheet1.xml이며 열 A~J가 고정돼 있다.
        tree = ET.fromstring(archive.read("xl/worksheets/sheet1.xml"))
        for row in tree.findall("m:sheetData/m:row", XML_NS):
            values: dict[str, str] = {}
            for cell in row.findall("m:c", XML_NS):
                value = cell.find("m:v", XML_NS)
                text = value.text if value is not None and value.text else ""
                if cell.get("t") == "s":
                    text = strings[int(text)]
                elif cell.get("t") == "inlineStr":
                    text = "".join(t.text or "" for t in cell.findall(".//m:t", XML_NS))
                column = re.sub(r"\d", "", cell.get("r", ""))
                values[column] = text
            yield values


def _metadata_snapshot(path: Path) -> datetime | None:
    match = re.search(r"\((\d{2})\.(\d{1,2})", path.name)
    if not match:
        return None
    year, month = 2000 + int(match[1]), int(match[2])
    return datetime(year, month, calendar.monthrange(year, month)[1])


def _read_metadata(config: dict, root: Path, snapshot: datetime):
    override = config.get("station_metadata_file")
    if override:
        path = _resolve(str(override), root)
        dated = _metadata_snapshot(path)
    else:
        candidates = [(date, path) for path in root.rglob("*.xlsx")
                      if (date := _metadata_snapshot(path)) is not None]
        eligible = [(date, path) for date, path in candidates if date <= snapshot]
        if not eligible:
            raise ValueError("스냅샷 이전의 대여소 XLSX가 없습니다. 과거 메타데이터를 제공하거나 "
                             "2025-07 이후 스냅샷을 사용하세요.")
        dated, path = max(eligible, key=lambda item: (item[0], str(item[1])))
    if dated is not None and dated > snapshot:
        raise ValueError("대여소 메타데이터 시점이 스냅샷보다 미래입니다.")
    stations: dict[str, dict] = {}
    audit: Counter = Counter()
    for row in _xlsx_rows(path):
        audit["metadata_rows_read"] += 1
        station = _station_id(row.get("A", ""))
        try:
            latitude, longitude = float(row.get("E", "")), float(row.get("F", ""))
        except ValueError:
            latitude, longitude = math.nan, math.nan
        if station is None or not (-90 <= latitude <= 90 and -180 <= longitude <= 180):
            audit["metadata_header_or_invalid_rows"] += 1
            continue
        if station in stations:
            raise ValueError(f"대여소 메타데이터 번호 중복: {station}")
        stations[station] = {
            "station_id": station, "name": row.get("B", "").strip(),
            "latitude": latitude, "longitude": longitude,
            "installed_lcd_racks": _nonnegative_integer(row.get("H", "")),
            "installed_qr_racks": _nonnegative_integer(row.get("I", "")),
        }
    source = _record_source(path)
    source["metadata_snapshot"] = dated.date().isoformat() if dated else "user_supplied_unknown"
    return stations, audit, source


def _inventory_paths(root: Path, start: datetime, end: datetime) -> list[Path]:
    # 월 경계가 걸린 과거 관측도 읽되 관련 없는 월은 읽지 않는다.
    months = set()
    cursor = start.replace(day=1, hour=0)
    while cursor <= end:
        months.add(cursor.strftime("%y%m"))
        cursor = (cursor.replace(day=28) + timedelta(days=4)).replace(day=1)
    paths = []
    for month in sorted(months):
        matching = sorted(root.rglob(f"data_{month}.csv"))
        if len(matching) != 1:
            raise ValueError(f"월 {month}의 시간별 재고 CSV가 정확히 1개 필요합니다: {len(matching)}개")
        paths.extend(matching)
    return paths


def _read_inventory(paths: list[Path], start: datetime, snapshot: datetime, same_hour: bool):
    observations: dict[tuple[str, datetime], int] = {}
    invalid_keys: set[tuple[str, datetime]] = set()
    audit: Counter = Counter()
    for path in paths:
        with path.open(encoding="cp949", newline="") as stream:
            reader = csv.DictReader(stream)
            reader.fieldnames = [name.strip() for name in reader.fieldnames or []]
            if not INVENTORY_FIELDS.issubset(reader.fieldnames):
                raise ValueError(f"재고 CSV 필수 열 누락: {path.name}")
            for row in reader:
                audit["inventory_rows_scanned"] += 1
                day = row["일시"].strip()[:10]
                if day < start.date().isoformat() or day > snapshot.date().isoformat():
                    continue
                hour = _nonnegative_integer(row["시간대"])
                if hour is None or hour >= 24:
                    audit["invalid_time_rows"] += 1
                    continue
                if same_hour and hour != snapshot.hour:
                    continue
                try:
                    moment = datetime.fromisoformat(day) + timedelta(hours=hour)
                except ValueError:
                    audit["invalid_time_rows"] += 1
                    continue
                if not start <= moment <= snapshot:
                    continue
                audit["inventory_rows_in_window"] += 1
                station = _station_id(row["대여소번호"])
                if station is None:
                    audit["invalid_station_rows"] += 1
                    continue
                key = station, moment
                inventory = _nonnegative_integer(row["거치대수량"])
                if inventory is None:
                    audit["invalid_inventory_rows"] += 1
                    invalid_keys.add(key)
                elif key in observations:
                    if observations[key] == inventory:
                        audit["identical_duplicate_rows"] += 1
                    else:
                        invalid_keys.add(key)
                        audit["conflicting_duplicate_rows"] += 1
                else:
                    observations[key] = inventory
    # 충돌 관측을 평균 내거나 마지막 값으로 덮지 않는다.
    for key in invalid_keys:
        observations.pop(key, None)
    audit["excluded_invalid_or_conflicting_keys"] = len(invalid_keys)
    return observations, audit


def _read_manual_csv(path: Path, snapshot: datetime):
    """사용자가 검증한 관측·목표를 직접 제공하는 CSV 입력 경로."""
    required = {"station_id", "name", "latitude", "longitude", "bikes", "target"}
    stations = []
    seen = set()
    with path.open(encoding="utf-8-sig", newline="") as stream:
        reader = csv.DictReader(stream)
        if not required.issubset(reader.fieldnames or []):
            raise ValueError(f"직접 입력 CSV에 필요한 열: {', '.join(sorted(required))}")
        for index, row in enumerate(reader, start=2):
            station = _station_id(row["station_id"])
            bikes, target = _nonnegative_integer(row["bikes"]), _nonnegative_integer(row["target"])
            try:
                latitude, longitude = float(row["latitude"]), float(row["longitude"])
            except ValueError as error:
                raise ValueError(f"직접 입력 CSV {index}행의 좌표 오류") from error
            if (station is None or bikes is None or target is None or station in seen
                    or not (-90 <= latitude <= 90 and -180 <= longitude <= 180)):
                raise ValueError(f"직접 입력 CSV {index}행의 값 또는 중복 번호 오류")
            if row.get("timestamp") and _timestamp(row["timestamp"]) != snapshot:
                raise ValueError(f"직접 입력 CSV {index}행의 timestamp가 설정과 다릅니다.")
            seen.add(station)
            stations.append({"station_id": station, "name": row["name"].strip(),
                             "latitude": latitude, "longitude": longitude,
                             "bikes": bikes, "target": target})
    if not stations:
        raise ValueError("직접 입력 CSV가 비어 있습니다.")
    return sorted(stations, key=lambda item: int(item["station_id"])), {
        "input_mode": "manual_csv", "snapshot_timestamp": snapshot.isoformat(),
        "timezone": "Asia/Seoul", "sources": [_record_source(path)],
        "target_definition": "사용자가 제공한 target. 자동 수요 추정 없음.",
        "audit": {"usable_stations": len(stations)},
    }


def load_stations(config: dict, data_dir: Path) -> tuple[list[dict], dict]:
    """좌표·관측 재고·목표 재고를 합치고 제외 현황과 입력 출처를 반환한다.

    설정: snapshot_timestamp, history_days=14, same_hour=True,
    minimum_history_days=7, mode='historical_median' 또는 'fixed', fixed_target=10.
    stations_csv를 지정하면 표준 6개 열의 사용자 입력을 그대로 검증해 사용한다.
    """
    root = Path(data_dir).expanduser()
    snapshot = _timestamp(config.get("snapshot_timestamp", ""))
    if config.get("stations_csv"):
        return _read_manual_csv(_resolve(str(config["stations_csv"]), root), snapshot)
    mode = config.get("mode", "historical_median")
    if mode not in {"historical_median", "fixed"}:
        raise ValueError("목표 재고 mode는 historical_median 또는 fixed여야 합니다.")
    history_days = config.get("history_days", 14)
    minimum_days = config.get("minimum_history_days", 7)
    if (not isinstance(history_days, int) or isinstance(history_days, bool) or history_days < 1
            or not isinstance(minimum_days, int) or isinstance(minimum_days, bool)
            or not 1 <= minimum_days <= history_days):
        raise ValueError("history_days와 minimum_history_days는 1 이상의 정수이고 최소 일수는 관측기간 이하여야 합니다.")
    same_hour = config.get("same_hour", True)
    if not isinstance(same_hour, bool):
        raise ValueError("same_hour에는 true 또는 false를 지정하세요.")
    fixed = _nonnegative_integer(config.get("fixed_target", 10))
    if mode == "fixed" and fixed is None:
        raise ValueError("fixed_target은 0 이상의 정수여야 합니다.")
    start = snapshot - timedelta(days=history_days) if mode == "historical_median" else snapshot
    metadata, audit, metadata_source = _read_metadata(config, root, snapshot)
    paths = _inventory_paths(root, start, snapshot)
    observations, inventory_audit = _read_inventory(paths, start, snapshot, same_hour)
    audit.update(inventory_audit)
    current = {station: value for (station, moment), value in observations.items() if moment == snapshot}
    if not current:
        raise ValueError(f"정확한 스냅샷 {snapshot.isoformat()}의 유효 재고가 없습니다. 다른 시각으로 대체하지 않습니다.")
    history: dict[str, list[tuple[datetime, int]]] = defaultdict(list)
    for (station, moment), value in observations.items():
        if moment < snapshot:
            history[station].append((moment, value))
    stations = []
    for station_id, item in sorted(metadata.items(), key=lambda pair: int(pair[0])):
        if station_id not in current:
            audit["excluded_missing_snapshot"] += 1
            continue
        historic = history[station_id]
        observed_days = len({moment.date() for moment, _ in historic})
        if mode == "historical_median" and observed_days < minimum_days:
            audit["excluded_insufficient_history"] += 1
            continue
        # 홀수·짝수 관측 모두 동일 규칙: 중앙값의 소수 부분을 버린다.
        target = math.floor(median(value for _, value in historic)) if mode == "historical_median" else fixed
        stations.append({**item, "bikes": current[station_id], "target": target,
                         "history_observation_count": len(historic), "history_day_count": observed_days})
    if not stations:
        raise ValueError("메타데이터·정확한 스냅샷·과거 관측을 모두 충족한 대여소가 없습니다.")
    audit["snapshot_stations_without_metadata"] = len(set(current) - set(metadata))
    audit["snapshot_stations"] = len(current)
    audit["usable_stations"] = len(stations)
    return stations, {
        "input_mode": "historical_csv", "snapshot_timestamp": snapshot.isoformat(),
        "timezone": "Asia/Seoul", "history_start_inclusive": start.isoformat(),
        "history_end_exclusive": snapshot.isoformat(), "same_hour": same_hour,
        "mode": mode, "history_days": history_days, "minimum_history_days": minimum_days,
        "target_rounding": "floor", "fixed_target": fixed if mode == "fixed" else None,
        "target_definition": "과거 재고 중앙값 내림(서비스 수준·잠재 수요 추정 아님)" if mode == "historical_median" else "사용자 지정 고정 실험 목표",
        "inventory_definition": "시간별 원본 거치대수량 열의 대여 가능 자전거 수. 분 단위 재고를 복원하지 않음.",
        "rack_policy": "설치 거치대 수는 참고 메타데이터이며 물리적 수용량이나 재고 상한으로 사용하지 않음.",
        "sources": [metadata_source, *[_record_source(path) for path in paths]],
        "audit": dict(audit),
    }
