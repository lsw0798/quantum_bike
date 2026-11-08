"""원본 파일 읽기. 인코딩·식별자·출처를 한 곳에서 관리한다."""
from pathlib import Path
import hashlib
import json
import re

import numpy as np
import pandas as pd

# CSV를 나눠 읽을 때 한 번에 불러올 행 수
CHUNK_SIZE = 200_000


def station_id(value):
    """00102와 102.0은 102로 통일. ST-...는 별도 식별체계이므로 거절."""
    text = str(value).strip()

    # 숫자 형식이면 정수 문자열로 통일하고, 아니면 None을 반환한다.
    return str(int(float(text))) if re.fullmatch(r"\d+(\.0+)?", text) else None


def read_chunks(path, **kwargs):
    """cp949 CSV를 청크 단위로 읽고, 열 이름의 공백을 지워 하나씩 내보낸다."""
    for frame in pd.read_csv(path, encoding="cp949", chunksize=CHUNK_SIZE, **kwargs):
        frame.columns = frame.columns.str.strip()
        yield frame


def source_record(path):
    """파일의 절대 경로·크기·수정 시각을 출처 기록으로 만든다."""
    path = Path(path)
    return {"path": str(path.resolve()), "bytes": path.stat().st_size,
            "mtime_ns": path.stat().st_mtime_ns}


def dump_json(path, value):
    """값을 UTF-8 JSON으로 저장한다. NaN/Infinity는 허용하지 않는다."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False),
                    encoding="utf-8")


def monthly_ranking(root):
    """월별 집계 CSV의 이용건수를 합산한다. 일별 폴더의 중복 월별 파일 제외."""
    parts, sources = [], []

    # 파일마다 출처를 기록하고, 청크별로 대여 월의 이용건수를 합산한다.
    for path in sorted(root.glob("공공자전거 이용정보(월별)/*.csv")):
        sources.append(source_record(path))
        for frame in read_chunks(path, usecols=lambda c: c.strip() in ("대여일자", "이용건수")):
            frame["count"] = pd.to_numeric(frame["이용건수"], errors="raise")
            parts.append(frame.groupby("대여일자")["count"].sum())

    # 청크별 합계를 다시 월 단위로 합친다.
    result = pd.concat(parts).groupby(level=0).sum().rename("trips").reset_index()
    result.columns = ["month", "trips"]
    result["month"] = result["month"].astype(str)

    # 이용건수 내림차순, 동률이면 월 오름차순으로 정렬한다.
    return result.sort_values(["trips", "month"], ascending=[False, True]), sources


def daily_ranking(root, month):
    """지정한 월의 일별 이용건수를 합산해 많은 날짜 순으로 반환한다."""
    path = next(root.glob(f"공공자전거 이용정보(일별)/*_{month[2:]}.csv"))
    parts = []

    # 청크별로 대여일의 이용건수를 합산한다.
    for frame in read_chunks(path, usecols=lambda c: c.strip() in ("대여일자", "이용건수")):
        frame["count"] = pd.to_numeric(frame["이용건수"], errors="raise")
        parts.append(frame.groupby("대여일자")["count"].sum())

    # 청크별 합계를 다시 날짜 단위로 합친다.
    result = pd.concat(parts).groupby(level=0).sum().rename("trips").reset_index()
    result.columns = ["date", "trips"]

    # 이용건수 내림차순, 동률이면 날짜 오름차순으로 정렬한다.
    return result.sort_values(["trips", "date"], ascending=[False, True])


def load_stations(root, month):
    """해당 월에 쓸 대여소 메타데이터를 읽어 대여소 번호를 인덱스로 반환한다."""
    # 과거 지리 정보는 회고적 대리값이다. 미래 시점의 재고 정보는 사용하지 않는다.
    suffix = "25.6" if int(month[-2:]) <= 6 else "25.12"
    path = next(root.glob(f"공공자전거 대여소 정보/*{suffix}*.xlsx"))

    # 헤더 없이 읽고 앞 10개 열에 이름을 붙인다.
    raw = pd.read_excel(path, header=None)
    raw = raw.iloc[:, :10].copy()
    raw.columns = ["station", "name", "district", "address", "lat", "lon",
                   "installed", "lcd_racks", "qr_racks", "type"]

    # 번호와 좌표를 정규화하고, 값이 없는 행을 제거한다.
    raw["station"] = raw.station.map(station_id)
    raw["lat"] = pd.to_numeric(raw.lat, errors="coerce")
    raw["lon"] = pd.to_numeric(raw.lon, errors="coerce")
    raw = raw.dropna(subset=["station", "lat", "lon"])

    # 같은 번호가 있으면 어느 행이 맞는지 알 수 없으므로 중단한다.
    if raw.station.duplicated().any():
        raise ValueError("중복 대여소 번호: 메타데이터를 확인하세요.")
    return raw.set_index("station"), source_record(path)


def load_trips(root, month, dates, cache_dir):
    """선정일과 전일/익일에 걸친 운행을 읽는다. source_row는 헤더 제외 1-based."""
    # 해당 월의 대여이력 파일은 정확히 하나여야 한다.
    paths = sorted(root.glob(f"공공자전거 대여이력 정보/**/*_{month[2:]}.csv"))
    if len(paths) != 1:
        raise ValueError(f"월 {month}: 대여이력 파일 개수 {len(paths)}")

    # 앞뒤 달 파일도 함께 읽어 월 경계를 넘는 이용이 선정일에 포함되게 한다.
    all_paths = sorted(root.glob("공공자전거 대여이력 정보/**/*.csv"))
    i = all_paths.index(paths[0])
    paths = all_paths[max(0, i-1):min(len(all_paths), i+2)]

    # 선정일과 그 전일·익일을 읽을 날짜로 잡는다.
    wanted = set()
    for day in dates:
        for offset in (-1, 0, 1):
            wanted.add((pd.Timestamp(day)+pd.Timedelta(days=offset)).strftime("%Y-%m-%d"))

    # 원본 파일과 날짜로 캐시 키를 만들고, 캐시가 있으면 그대로 사용한다.
    signature = hashlib.sha256(json.dumps(
        {"v": 2, "sources": [source_record(p) for p in paths], "dates": sorted(wanted)},
        sort_keys=True).encode()).hexdigest()[:20]
    cache = cache_dir / f"trips_{signature}.parquet"
    if cache.exists():
        return mark_duplicate_trips(pd.read_parquet(cache)), [source_record(p) for p in paths]

    # 원본 한글 열 이름을 내부 이름으로 바꾸는 대응표
    names = {"대여일시": "start", "반납일시": "end", "대여 대여소번호": "origin",
             "반납대여소번호": "destination", "자전거번호": "bike_id"}
    parts = []

    # 파일별 반복
    for path in paths:
        offset = 0

        # 청크별로 원본 행 번호를 붙이고 필요한 날짜의 행만 남긴다.
        for frame in read_chunks(path, dtype=str, usecols=lambda c: c.strip() in names):
            frame = frame.rename(columns=names)
            frame["source_row"] = np.arange(offset+1, offset+len(frame)+1)
            offset += len(frame)
            mask = frame.start.str[:10].isin(wanted) | frame.end.str[:10].isin(wanted)
            frame = frame.loc[mask].copy()

            # 남은 행이 없으면 다음 청크로 넘어간다.
            if frame.empty:
                continue

            # 출처를 기록하고 대여소 번호와 시각을 정규화한다.
            frame["source_file"] = str(path.resolve())
            frame["origin"] = frame.origin.map(station_id)
            frame["destination"] = frame.destination.map(station_id)
            frame["start"] = pd.to_datetime(frame.start, errors="coerce")
            frame["end"] = pd.to_datetime(frame.end, errors="coerce")
            frame["request_id"] = path.stem + ":" + frame.source_row.astype(str)
            parts.append(frame)

    # 합친 뒤, 출발·도착이 모두 있고 시간 순서가 맞는 행만 유효로 표시한다.
    result = pd.concat(parts, ignore_index=True)
    result["valid"] = (result.origin.notna() & result.destination.notna()
                       & result.start.notna() & result.end.notna() & (result.end > result.start))

    # 다음 실행을 위해 캐시로 저장한다.
    cache_dir.mkdir(parents=True, exist_ok=True)
    result.to_parquet(cache, index=False)
    return mark_duplicate_trips(result), [source_record(p) for p in paths]


def mark_duplicate_trips(frame):
    """동일 물리 여행의 중복 행은 출처를 유지하되 요청·사건에서 한 번만 사용."""
    frame = frame.copy()

    # 자전거·시각·대여소가 같은 행 중 첫 행만 남기고 중복으로 표시한다.
    frame["duplicate_observation"] = frame.duplicated(
        ["bike_id", "start", "end", "origin", "destination"], keep="first")

    # 중복 행은 삭제하지 않고 유효 표시만 끈다.
    frame["valid"] = frame["valid"] & ~frame["duplicate_observation"]
    return frame


def load_inventory(root, month, dates, cache_dir):
    """선정일의 시간별 대여 가능 수량(재고 스냅샷)을 읽어 검증한다."""
    path = next(root.glob(f"공공자전거 대여가능 수량(1시간 단위)/**/data_{month[2:]}.csv"))

    # 원본 파일과 날짜로 캐시 키를 만들고, 캐시가 있으면 그대로 사용한다.
    signature = hashlib.sha256(json.dumps(
        {"v": 1, "source": source_record(path), "dates": sorted(dates)},
        sort_keys=True).encode()).hexdigest()[:20]
    cache = cache_dir / f"inventory_{signature}.parquet"
    if cache.exists():
        return pd.read_parquet(cache), source_record(path)
    parts = []

    # 청크별로 선정일만 남기고, 일시와 시간대를 합쳐 스냅샷 시각을 만든다.
    for frame in read_chunks(path, dtype=str):
        frame = frame.loc[frame["일시"].str[:10].isin(dates)].copy()
        frame["station"] = frame["대여소번호"].map(station_id)
        frame["time"] = pd.to_datetime(frame["일시"], errors="coerce") + pd.to_timedelta(
            pd.to_numeric(frame["시간대"], errors="coerce"), unit="h")
        frame["inventory"] = pd.to_numeric(frame["거치대수량"], errors="coerce")
        parts.append(frame[["station", "time", "inventory"]])
    result = pd.concat(parts, ignore_index=True).dropna()

    # 같은 대여소·시각의 스냅샷이 중복되면 임의로 합치지 않고 중단한다.
    if result.duplicated(["station", "time"]).any():
        raise ValueError("재고 스냅샷 중복: 임의 평균/합산하지 않습니다.")

    # 음수 재고는 관측 오류이므로 중단한다.
    if (result.inventory < 0).any():
        raise ValueError("음수 관측 재고")

    # 다음 실행을 위해 캐시로 저장한다.
    result.to_parquet(cache, index=False)
    return result, source_record(path)
