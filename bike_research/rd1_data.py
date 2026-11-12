"""RD-1.0 실측 자료 캐시와 날짜 분할.

원본 CSV는 수정하지 않는다. 한 번의 순차 읽기로 일별 parquet를 만들고,
전체 행의 출처는 source_file/source_row, 중복 행의 연결은 lineage에 남긴다.
여행의 양 끝 사건은 별도로 보관하므로 월 경계나 120분 초과 여행의
알려진 사건을 배경에서 잃지 않는다. 계정이나 QPU 모듈은 사용하지 않는다.
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
import hashlib
import json
import math
import re

import duckdb
import numpy as np
import pandas as pd

from .data import dump_json, source_record, station_id

CACHE_VERSION = "rd1-data-1.0"
CHUNK_ROWS = 200_000
PHYSICAL_KEY = ["bike_id", "start", "end", "origin", "destination"]
TRIP_NAMES = {"대여일시": "start", "반납일시": "end", "대여 대여소번호": "origin",
              "반납대여소번호": "destination", "자전거번호": "bike_id"}


def _sql(text):
    """SQL 문자열 값 인용. 경로도 식별자가 아닌 문자열로만 넣는다."""
    return "'" + str(text).replace("'", "''") + "'"


def _connect(cache_dir=None):
    con = duckdb.connect()
    con.execute("SET threads=2")
    con.execute("SET memory_limit='2GB'")
    con.execute("SET preserve_insertion_order=false")
    if cache_dir is not None:
        temporary = Path(cache_dir) / "duckdb_temp"
        temporary.mkdir(parents=True, exist_ok=True)
        con.execute(f"SET temp_directory={_sql(temporary.as_posix())}")
    return con


def _progress(cache, stage, **details):
    record = {"stage": stage, "utc": datetime.now(timezone.utc).isoformat(), **details}
    dump_json(Path(cache) / "progress.json", record)
    print(json.dumps(record, ensure_ascii=False), flush=True)


def clean_inventory(frame):
    """동일 값 중복은 합치고 충돌·잘못된 수량을 가진 관측 키는 결측 처리."""
    frame = frame.copy()
    finite = np.isfinite(pd.to_numeric(frame.inventory, errors="coerce"))
    good = finite & frame.inventory.ge(0) & frame.inventory.mod(1).eq(0)
    good &= frame.station.notna() & frame.time.notna()
    frame["good"] = good
    groups = frame.dropna(subset=["station", "time"]).groupby(["station", "time"])
    stats = groups.agg(inventory=("inventory", "first"), n=("inventory", "size"),
                       different=("inventory", "nunique"), all_good=("good", "all"))
    conflict = stats.different.gt(1)
    keep = stats.all_good & ~conflict
    result = stats.loc[keep, ["inventory"]].reset_index()
    result["inventory"] = result.inventory.astype("int64")
    audit = {"input_rows": len(frame), "valid_keys": len(result),
             "invalid_rows": int((~good).sum()), "conflicting_keys": int(conflict.sum()),
             "identical_duplicate_rows": int((stats.loc[keep, "n"]-1).sum())}
    return result, audit


def deduplicate_trips(frame):
    """완전한 물리 키만 중복 제거. 식별 불충분 행은 임의로 합치지 않는다."""
    raw = frame.sort_values(["source_file", "source_row"]).copy()
    complete = raw[PHYSICAL_KEY].notna().all(axis=1)
    raw["source_lineage_count"] = 1
    count = raw.loc[complete].groupby(PHYSICAL_KEY, dropna=False).request_id.transform("size")
    raw.loc[complete, "source_lineage_count"] = count
    duplicate = pd.Series(False, index=raw.index)
    duplicate.loc[complete] = raw.loc[complete].duplicated(PHYSICAL_KEY, keep="first")
    lineage = raw.loc[raw.source_lineage_count.gt(1)].copy()
    if not lineage.empty:
        lineage["canonical_request_id"] = lineage.groupby(PHYSICAL_KEY).request_id.transform("first")
    clean = raw.loc[~duplicate].copy()
    clean["duplicate_observation"] = False
    seconds = (clean.end-clean.start).dt.total_seconds()
    clean["valid"] = (clean[PHYSICAL_KEY[1:]].notna().all(axis=1)
                      & seconds.gt(0) & seconds.le(7200))
    clean["duration_seconds"] = seconds
    return clean, lineage


def choose_snapshot(day, available, max_days=183):
    """가장 가까운 스냅샷. 같은 거리면 과거 것을 선택한다."""
    day = pd.Timestamp(day).normalize()
    possibilities = [(abs((pd.Timestamp(s)-day).days), pd.Timestamp(s)>day, str(s))
                     for s in available]
    if not possibilities:
        return None
    distance, _, selected = min(possibilities)
    return selected if distance <= max_days else None


def frozen_date_split(dates, forced_development=("2025-06-05", "2025-06-10")):
    """날짜 전체를 70/30로 분할하고 기존 확인 날짜와 5시간 경계를 보존."""
    dates = sorted(set(str(d)[:10] for d in dates))
    other = [d for d in dates if d not in forced_development]
    n = math.floor(0.7*len(other))
    tentative = other[n-1] if n else None
    previous = max((d for d in forced_development if d in dates), default=None)
    cutoff = max([d for d in (tentative, previous) if d is not None], default=None)
    development = [d for d in dates if cutoff is not None and d <= cutoff]
    evaluation = [d for d in dates if cutoff is None or d > cutoff]
    return {"cutoff_date": cutoff, "development_dates": development,
            "evaluation_dates": evaluation, "first_evaluation_date": evaluation[0] if evaluation else None,
            "embargo_first_hour": 19, "embargo_rule": "t0+5h >= first evaluation midnight",
            "forced_development_dates": [d for d in forced_development if d in dates]}


def estimate_historical_od(trips, day, min_count=10):
    """과거 28일 중앙값, 부족하면 과거 90일 중앙값. 당일은 항상 제외."""
    anchor = pd.Timestamp(day).normalize()
    duration = (trips.end-trips.start).dt.total_seconds()
    # 당일 자정까지 완료되지 않은 여행의 소요시간은 당시 알 수 없으므로 제외한다.
    keep = trips.start.lt(anchor) & trips.end.lt(anchor) & trips.start.ge(anchor-pd.Timedelta(days=90))
    keep &= duration.gt(0) & duration.le(7200) & trips.origin.notna() & trips.destination.notna()
    if "valid" in trips:
        keep &= trips.valid
    frame = trips.loc[keep, ["origin", "destination", "start"]].copy()
    frame["seconds"] = duration.loc[keep]
    result = {}
    for window in (28, 90):
        selected = frame.loc[frame.start.ge(anchor-pd.Timedelta(days=window))]
        for (origin, destination), group in selected.groupby(["origin", "destination"]):
            key = (str(origin), str(destination))
            if key not in result and len(group) >= min_count:
                result[key] = {"seconds": float(group.seconds.median()),
                               "n": len(group), "window_days": window}
    return result


def _hash_file(path):
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(8*1024*1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _write_date_fragments(frame, date_column, directory, prefix):
    """청크의 실제 날짜별 fragment. 나중에 하루씩 중복을 정리한다."""
    date = frame[date_column].dt.strftime("%Y-%m-%d").fillna("unknown")
    for day, indexes in date.groupby(date).groups.items():
        folder = directory / str(day)
        folder.mkdir(parents=True, exist_ok=True)
        frame.loc[indexes].to_parquet(folder / f"{prefix}.parquet", index=False)


def _ingest_trips(paths, cache):
    raw_dir = cache / "raw_trips"
    for file_index, path in enumerate(paths):
        marker = cache / "source_complete" / f"trips_{file_index:02}.json"
        if marker.exists():
            continue
        offset = 0
        reader = pd.read_csv(path, encoding="cp949", dtype=str, chunksize=CHUNK_ROWS,
                             usecols=lambda c: c.strip() in TRIP_NAMES)
        for chunk_index, frame in enumerate(reader):
            frame.columns = frame.columns.str.strip()
            frame = frame.rename(columns=TRIP_NAMES)
            frame["source_row"] = np.arange(offset+1, offset+len(frame)+1)
            offset += len(frame)
            frame["source_file"] = str(path.resolve())
            frame["request_id"] = f"T{file_index:02}-" + frame.source_row.astype(str)
            for column in ("origin", "destination"):
                frame[column] = frame[column].map(station_id)
            for column in ("start", "end"):
                frame[column] = pd.to_datetime(frame[column], errors="coerce")
            frame["partition_time"] = frame.start.fillna(frame.end)
            _write_date_fragments(frame, "partition_time", raw_dir,
                                  f"f{file_index:02}_p{chunk_index:04}")
        dump_json(marker, {"rows": offset, "path": str(path)})
        _progress(cache, "trip_sources", completed=file_index+1, total=len(paths), rows=offset)


def _finalize_trips(cache):
    rows, duplicates, invalid, eligible = 0, 0, 0, 0
    for index, folder in enumerate(sorted((cache / "raw_trips").iterdir())):
        day = folder.name
        destination = cache / "trips" / f"{day}.parquet"
        audit_path = cache / "trip_audit" / f"{day}.json"
        if audit_path.exists():
            stats = json.loads(audit_path.read_text(encoding="utf-8"))
        else:
            raw = pd.concat([pd.read_parquet(p) for p in sorted(folder.glob("*.parquet"))], ignore_index=True)
            clean, lineage = deduplicate_trips(raw.drop(columns=["partition_time"]))
            destination.parent.mkdir(parents=True, exist_ok=True)
            clean.to_parquet(destination, index=False)
            if not lineage.empty:
                (cache / "lineage").mkdir(exist_ok=True)
                lineage.to_parquet(cache / "lineage" / f"{day}.parquet", index=False)
            # 다른 날 시작한 여행이 오늘 끝나는 경우도 문맥에 넣을 수 있게 색인한다.
            cross = clean.loc[clean.end.notna() & clean.end.dt.normalize().ne(clean.start.dt.normalize())]
            if not cross.empty:
                _write_date_fragments(cross, "end", cache / "cross_day_returns", f"start_{day}")
            events = []
            for station_col, time_col, phase, amount in (("origin", "start", 0, -1),
                                                       ("destination", "end", 1, 1)):
                event = clean.loc[clean[station_col].notna() & clean[time_col].notna(),
                                  [station_col, time_col, "request_id"]].rename(columns={station_col: "station", time_col: "time"})
                event["phase"], event["amount"] = phase, amount
                events.append(event)
            event_frame = pd.concat(events, ignore_index=True)
            _write_date_fragments(event_frame, "time", cache / "events", f"start_{day}")
            stats = {"raw_rows": len(raw), "canonical_rows": len(clean),
                     "duplicate_rows": len(raw)-len(clean), "eligible_rows": int(clean.valid.sum()),
                     "incomplete_endpoint_rows": int((clean[["start", "end", "origin", "destination"]].isna().any(axis=1)).sum())}
            dump_json(audit_path, stats)
        rows += stats["canonical_rows"]
        duplicates += stats["duplicate_rows"]
        invalid += stats["incomplete_endpoint_rows"]
        eligible += stats["eligible_rows"]
        if index % 30 == 0:
            _progress(cache, "daily_trip_cleanup", completed=index+1, day=day)
    return {"raw_rows": rows+duplicates, "canonical_rows": rows,
            "duplicate_rows": duplicates, "eligible_rows": eligible,
            "incomplete_endpoint_rows": invalid}


def _ingest_inventory(paths, cache):
    for file_index, path in enumerate(paths):
        marker = cache / "source_complete" / f"inventory_{file_index:02}.json"
        if marker.exists():
            continue
        offset = 0
        for chunk_index, raw in enumerate(pd.read_csv(path, encoding="cp949", dtype=str, chunksize=CHUNK_ROWS)):
            raw.columns = raw.columns.str.strip()
            hours = pd.to_numeric(raw["시간대"], errors="coerce")
            time = pd.to_datetime(raw["일시"], errors="coerce") + pd.to_timedelta(hours, unit="h")
            time = time.where(hours.ge(0) & hours.lt(24) & hours.mod(1).eq(0))
            frame = pd.DataFrame({"station": raw["대여소번호"].map(station_id), "time": time,
                                  "inventory": pd.to_numeric(raw["거치대수량"], errors="coerce"),
                                  "source_file": str(path.resolve()),
                                  "source_row": np.arange(offset+1, offset+len(raw)+1)})
            offset += len(raw)
            _write_date_fragments(frame, "time", cache / "raw_inventory", f"f{file_index:02}_p{chunk_index:04}")
        dump_json(marker, {"rows": offset, "path": str(path)})
        _progress(cache, "inventory_sources", completed=file_index+1, total=len(paths), rows=offset)
    totals = {}
    for folder in sorted((cache / "raw_inventory").iterdir()):
        audit_path = cache / "inventory_audit" / f"{folder.name}.json"
        if audit_path.exists():
            audit = json.loads(audit_path.read_text(encoding="utf-8"))
        else:
            raw = pd.concat([pd.read_parquet(p) for p in sorted(folder.glob("*.parquet"))], ignore_index=True)
            clean, audit = clean_inventory(raw)
            (cache / "inventory").mkdir(exist_ok=True)
            clean.to_parquet(cache / "inventory" / f"{folder.name}.parquet", index=False)
            dump_json(audit_path, audit)
        for key, value in audit.items():
            totals[key] = totals.get(key, 0)+value
    return totals


def _ingest_stations(paths, cache):
    records = []
    for path in paths:
        match = re.search(r"\((\d{2})\.(\d{1,2})", path.name)
        if not match:
            raise ValueError(f"대여소 스냅샷 월을 읽을 수 없음: {path.name}")
        year, month = 2000+int(match[1]), int(match[2])
        snapshot = (pd.Timestamp(year=year, month=month, day=1)+pd.offsets.MonthEnd(0)).strftime("%Y-%m-%d")
        raw = pd.read_excel(path, header=None).iloc[:, :10].copy()
        raw.columns = ["station", "name", "district", "address", "lat", "lon", "installed", "lcd_racks", "qr_racks", "type"]
        raw["station"] = raw.station.map(station_id)
        for column in ("lat", "lon"):
            raw[column] = pd.to_numeric(raw[column], errors="coerce")
        valid = raw.station.notna() & raw.district.notna() & raw.lat.between(-90, 90) & raw.lon.between(-180, 180)
        clean = raw.loc[valid, ["station", "name", "district", "lat", "lon"]].copy()
        if clean.station.duplicated().any():
            raise ValueError(f"대여소 ID 중복: {path.name}")
        (cache / "stations").mkdir(exist_ok=True)
        clean.to_parquet(cache / "stations" / f"{snapshot}.parquet", index=False)
        records.append({"snapshot": snapshot, "snapshot_date_precision": "month_end_proxy_from_filename",
                        "source_file": str(path.resolve()), "valid_stations": len(clean),
                        "excluded_rows_including_headers": int((~valid).sum())})
    dump_json(cache / "station_manifest.json", records)
    return records


def prepare_rd1_data(root, cache_dir):
    """전체 원본의 해시·정제 일별 캐시 작성. 같은 출처면 중단 후 재개 가능."""
    root, cache = Path(root), Path(cache_dir)
    if (root / "origin_data").is_dir():
        root = root / "origin_data"
    cache.mkdir(parents=True, exist_ok=True)
    paths = {"trips": sorted(root.glob("공공자전거 대여이력 정보/**/*.csv")),
             "inventory": sorted(root.glob("공공자전거 대여가능 수량(1시간 단위)/**/*.csv")),
             "stations": sorted(root.glob("공공자전거 대여소 정보/*.xlsx"))}
    if any(not p for p in paths.values()):
        raise ValueError("대여이력·시간별수량·대여소 원본이 모두 필요합니다.")
    sources = [source_record(p) for group in paths.values() for p in group]
    signature = hashlib.sha256(json.dumps({"version": CACHE_VERSION, "sources": sources}, sort_keys=True).encode()).hexdigest()
    manifest_path = cache / "manifest.json"
    source_path = cache / "sources.json"
    if source_path.exists():
        old = json.loads(source_path.read_text(encoding="utf-8"))
        if old["source_signature"] != signature:
            raise ValueError("원본 또는 캐시 버전 변경: 새 cache_dir로 실행하세요.")
        sources = old["sources"]
        if manifest_path.exists():
            return json.loads(manifest_path.read_text(encoding="utf-8"))
    else:
        for record in sources:
            record["sha256"] = _hash_file(Path(record["path"]))
        dump_json(source_path, {"source_signature": signature, "sources": sources})
    _progress(cache, "source_hashes_complete", files=len(sources))
    stations = _ingest_stations(paths["stations"], cache)
    _ingest_trips(paths["trips"], cache)
    trip_audit = _finalize_trips(cache)
    inventory_audit = _ingest_inventory(paths["inventory"], cache)
    trip_dates = sorted(p.stem for p in (cache / "trips").glob("????-??-??.parquet"))
    stock_dates = sorted(p.stem for p in (cache / "inventory").glob("????-??-??.parquet"))
    manifest = {"version": CACHE_VERSION, "source_signature": signature, "sources": sources,
                "cache_dir": str(cache.resolve()), "trip_dates": trip_dates, "inventory_dates": stock_dates,
                "joined_dates": sorted(set(trip_dates) & set(stock_dates)), "trip_audit": trip_audit,
                "missing_inventory_dates": sorted(set(trip_dates)-set(stock_dates)),
                "inventory_audit": inventory_audit, "stations": stations,
                "snapshot_semantics_assumed": True, "timezone": "Asia/Seoul", "qpu_executed": False}
    dump_json(manifest_path, manifest)
    _progress(cache, "data_cache_complete", **trip_audit)
    return manifest


def load_station_snapshot(cache_dir, day):
    cache = Path(cache_dir)
    available = sorted(p.stem for p in (cache / "stations").glob("*.parquet"))
    selected = choose_snapshot(day, available)
    if selected is None:
        return pd.DataFrame(columns=["name", "district", "lat", "lon"]), {"status": "metadata_unavailable"}
    metadata = {"status": "available", "snapshot": selected,
                "days_difference": abs((pd.Timestamp(selected)-pd.Timestamp(day)).days),
                "retrospective_metadata_proxy": selected > str(day)[:10],
                "snapshot_date_precision": "month_end_proxy_from_filename"}
    return pd.read_parquet(cache / "stations" / f"{selected}.parquet").set_index("station"), metadata


def load_day_context(cache_dir, day):
    """당일 전체 창에 필요한 다음 날 05시까지와 과거 출발의 반납 사건 포함."""
    cache, anchor = Path(cache_dir), pd.Timestamp(day).normalize()
    stop = anchor+pd.Timedelta(days=1, hours=5)
    dates = [anchor.strftime("%Y-%m-%d"), (anchor+pd.Timedelta(days=1)).strftime("%Y-%m-%d")]
    parts = []
    for date in dates:
        primary = cache / "trips" / f"{date}.parquet"
        if primary.exists():
            parts.append(pd.read_parquet(primary))
        for path in sorted((cache / "cross_day_returns" / date).glob("*.parquet")):
            parts.append(pd.read_parquet(path))
    if not parts:
        raise ValueError(f"대여이력 없음: {day}")
    trips = pd.concat(parts, ignore_index=True).drop_duplicates("request_id")
    endpoint_in_context = trips.start.between(anchor, stop, inclusive="left") | trips.end.between(anchor, stop, inclusive="left")
    trips = trips.loc[endpoint_in_context].reset_index(drop=True)
    inventory = []
    for date in dates:
        path = cache / "inventory" / f"{date}.parquet"
        if path.exists():
            inventory.append(pd.read_parquet(path))
    stocks = pd.concat(inventory, ignore_index=True) if inventory else pd.DataFrame(columns=["station", "time", "inventory"])
    stocks = stocks.loc[stocks.time.ge(anchor) & stocks.time.le(stop)].reset_index(drop=True)
    return trips, stocks


def historical_od(cache_dir, day, min_count=10):
    """일별 parquet에서 필요한 과거만 SQL로 읽어 정확 중앙값을 계산한다."""
    cache, anchor = Path(cache_dir), pd.Timestamp(day).normalize()
    # v2는 완료 시각도 과거인지 검사한다. 이전 계산 파일을 재사용하지 않는다.
    target = cache / "historical_od" / f"{anchor:%Y-%m-%d}_n{min_count}_completed_v2.parquet"
    if target.exists():
        summary = pd.read_parquet(target)
    else:
        paths = [p.as_posix() for p in (cache / "trips").glob("????-??-??.parquet")
                 if anchor-pd.Timedelta(days=90) <= pd.Timestamp(p.stem) < anchor]
        if not paths:
            return {}
        relation = "read_parquet(["+",".join(_sql(p) for p in sorted(paths))+"])"
        bound = anchor-pd.Timedelta(days=28)
        sql = f"""WITH stats AS (
            SELECT origin, destination,
              count(*) FILTER (WHERE start >= TIMESTAMP {_sql(bound)}) AS n28,
              median(duration_seconds) FILTER (WHERE start >= TIMESTAMP {_sql(bound)}) AS median28,
              count(*) AS n90, median(duration_seconds) AS median90
            FROM {relation} WHERE valid AND start < TIMESTAMP {_sql(anchor)}
              AND "end" < TIMESTAMP {_sql(anchor)}
            GROUP BY origin, destination)
            SELECT origin, destination,
              CASE WHEN n28 >= {int(min_count)} THEN median28 ELSE median90 END AS seconds,
              CASE WHEN n28 >= {int(min_count)} THEN n28 ELSE n90 END AS n,
              CASE WHEN n28 >= {int(min_count)} THEN 28 ELSE 90 END AS window_days
            FROM stats WHERE n90 >= {int(min_count)}"""
        with _connect(cache) as con:
            summary = con.execute(sql).df()
        target.parent.mkdir(exist_ok=True)
        summary.to_parquet(target, index=False)
    return {(str(r.origin), str(r.destination)): {"seconds": float(r.seconds), "n": int(r.n), "window_days": int(r.window_days)}
            for r in summary.itertuples()}


def build_window_registry(cache_dir):
    """전체 날짜×24시간×보유 자치구 목록과 실패 사유를 먼저 고정한다."""
    cache = Path(cache_dir)
    manifest = json.loads((cache / "manifest.json").read_text(encoding="utf-8"))
    # 전체 재고가 누락된 날도 관측 여행이 있으면 모집단에 남겨 사유를 기록한다.
    dates = sorted(set(manifest["trip_dates"]) | set(manifest.get("inventory_dates", manifest["joined_dates"])))
    split = frozen_date_split(manifest["joined_dates"])
    all_districts = sorted(set().union(*(set(pd.read_parquet(p).district) for p in (cache / "stations").glob("*.parquet"))))
    rows = []
    inventory_times = set()
    snapshot_counts = {}
    for path in sorted((cache / "inventory").glob("????-??-??.parquet")):
        for time, count in pd.read_parquet(path, columns=["time"]).groupby("time").size().items():
            inventory_times.add(time)
            snapshot_counts[time] = int(count)
    for day in dates:
        stations, metadata = load_station_snapshot(cache, day)
        trip_path = cache / "trips" / f"{day}.parquet"
        if trip_path.exists():
            trips = pd.read_parquet(trip_path)
        else:
            trips = pd.DataFrame({"origin": pd.Series(dtype=str), "destination": pd.Series(dtype=str),
                                  "start": pd.Series(dtype="datetime64[ns]"), "valid": pd.Series(dtype=bool)})
        trips["district"] = trips.origin.map(stations.district if not stations.empty else {})
        trips["hour"] = trips.start.dt.hour
        observed = trips.groupby(["hour", "district"]).size()
        eligible = trips.loc[trips.valid & trips.origin.isin(stations.index) & trips.destination.isin(stations.index)].groupby(["hour", "district"]).size()
        for hour in range(24):
            anchor = pd.Timestamp(day)+pd.Timedelta(hours=hour)
            # 재고는 t0..+4h 다섯 점, 이동 사건만 +5h 직전까지 필요하다.
            context_ok = all(anchor+pd.Timedelta(hours=h) in inventory_times for h in range(5))
            # 관측 이동 문맥도 마지막 부분 날짜까지 존재해야 한다.
            end_day = (anchor+pd.Timedelta(hours=5)-pd.Timedelta(seconds=1)).strftime("%Y-%m-%d")
            context_ok &= end_day in manifest["trip_dates"]
            partition = "development" if split["cutoff_date"] is not None and day <= split["cutoff_date"] else "evaluation"
            embargo = partition == "development" and split["first_evaluation_date"] is not None and anchor+pd.Timedelta(hours=5) >= pd.Timestamp(split["first_evaluation_date"])
            if embargo:
                partition = "embargo"
            for district in all_districts:
                count = int(eligible.get((hour, district), 0))
                status = "eligible"
                if not trip_path.exists(): status = "missing_trip_context"
                elif metadata["status"] != "available": status = "metadata_unavailable"
                elif not context_ok: status = "missing_five_hour_context"
                elif embargo: status = "split_embargo"
                elif not count: status = "no_eligible_requests"
                rows.append({"date": day, "hour": hour, "district": district,
                             "eligible_requests": count, "observed_departures": int(observed.get((hour, district), 0)),
                             "status": status, "split": partition,
                             "metadata_snapshot": metadata.get("snapshot"),
                             "retrospective_metadata_proxy": metadata.get("retrospective_metadata_proxy"),
                             "observed_snapshot_count": snapshot_counts.get(anchor, 0)})
    result = pd.DataFrame(rows)
    result.to_csv(cache / "window_registry.csv", index=False, encoding="utf-8-sig")
    dump_json(cache / "date_split.json", {**split, "population_dates": dates,
                                          "actual_development_date_fraction": len(split["development_dates"])/max(1,len(manifest["joined_dates"])),
                                          "embargo_windows": int(result.split.eq("embargo").sum())})
    _progress(cache, "window_registry_complete", windows=len(result), eligible=int(result.status.eq("eligible").sum()))
    return result


def build_checkpoint_risk(cache_dir):
    """전체 시간별 고정 사건의 순유출 최대치와 부족량. 창의 S 확정 전 탐색용."""
    cache = Path(cache_dir)
    destination = cache / "checkpoint_risk.parquet"
    if destination.exists():
        return destination
    events = (cache / "events" / "*" / "*.parquet").as_posix()
    inventory = (cache / "inventory" / "????-??-??.parquet").as_posix()
    sql = f"""WITH grouped AS (
      SELECT station, time, phase, sum(amount) AS amount
      FROM read_parquet({_sql(events)}) GROUP BY station,time,phase),
      prefix AS (
      SELECT station,date_trunc('hour',time) AS checkpoint,
       -sum(amount) OVER (PARTITION BY station,date_trunc('hour',time)
                         ORDER BY time,phase ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW) AS demand
      FROM grouped), reserves AS (
      SELECT station,checkpoint,greatest(0,max(demand)) AS required_reserve
      FROM prefix GROUP BY station,checkpoint)
      SELECT i.station,i.time,i.inventory,coalesce(r.required_reserve,0) AS required_reserve,
       greatest(0,coalesce(r.required_reserve,0)-i.inventory) AS deficit
      FROM read_parquet({_sql(inventory)}) i LEFT JOIN reserves r
      ON i.station=r.station AND i.time=r.checkpoint"""
    with _connect(cache) as con:
        con.execute(f"COPY ({sql}) TO {_sql(destination.as_posix())} (FORMAT PARQUET)")
    _progress(cache, "checkpoint_risk_complete")
    return destination


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="RD1 원본 정제와 전체 입력창 등록만 실행합니다.")
    parser.add_argument("--root", type=Path, default=Path("origin_data"))
    parser.add_argument("--cache", type=Path, default=Path("outputs/rd1_data_cache"))
    args = parser.parse_args()
    prepare_rd1_data(args.root, args.cache)
    build_window_registry(args.cache)
    build_checkpoint_risk(args.cache)
