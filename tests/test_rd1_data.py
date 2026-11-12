"""RD1 데이터 규칙 검증용 작은 입력이며 연구 성능 사례가 아니다."""
import json
import pandas as pd

from bike_research.rd1_data import (
    clean_inventory, deduplicate_trips, choose_snapshot,
    frozen_date_split, estimate_historical_od, historical_od, load_day_context,
    build_window_registry, build_checkpoint_risk,
)


def test_inventory_conflict_is_missing_and_identical_duplicates_merge():
    raw = pd.DataFrame({
        'station': ['1', '1', '2', '2', '3', '4'],
        'time': pd.to_datetime(['2025-01-01'] * 6),
        'inventory': [4, 4, 2, 3, -1, 1.5],
    })
    clean, audit = clean_inventory(raw)
    assert clean[['station', 'inventory']].values.tolist() == [['1', 4]]
    assert audit['conflicting_keys'] == 1
    assert audit['invalid_rows'] == 2
    assert audit['identical_duplicate_rows'] == 1


def test_duplicate_lineage_and_unknown_identity_are_preserved():
    raw = pd.DataFrame({
        'bike_id': ['b', 'b', None, None], 'origin': ['1'] * 4,
        'destination': ['2'] * 4,
        'start': pd.to_datetime(['2025-01-01'] * 4),
        'end': pd.to_datetime(['2025-01-01 00:10'] * 4),
        'source_file': ['a'] * 4, 'source_row': [1, 2, 3, 4],
        'request_id': ['a:1', 'a:2', 'a:3', 'a:4'],
    })
    clean, duplicates = deduplicate_trips(raw)
    assert len(clean) == 3
    assert clean.iloc[0].source_lineage_count == 2
    assert set(duplicates.source_row) == {1, 2}


def test_station_snapshot_tie_chooses_past_and_enforces_183_days():
    snapshots = ['2025-01-01', '2025-01-03']
    assert choose_snapshot('2025-01-02', snapshots) == '2025-01-01'
    assert choose_snapshot('2026-01-01', snapshots) is None


def test_date_split_forced_development_and_five_hour_embargo():
    dates = ['2025-06-01', '2025-06-05', '2025-06-10', '2025-06-11']
    result = frozen_date_split(dates)
    assert result['cutoff_date'] == '2025-06-10'
    assert result['evaluation_dates'] == ['2025-06-11']
    assert result['embargo_first_hour'] == 19


def test_historical_od_never_uses_current_day_and_falls_back_to_90_days():
    rows = []
    for day, seconds, count in [('2025-01-10', 600, 10),
                                ('2025-03-09', 900, 2),
                                ('2025-03-10', 3, 100)]:
        for i in range(count):
            start = pd.Timestamp(day) + pd.Timedelta(minutes=i)
            rows.append({'origin': '1', 'destination': '2', 'start': start,
                         'end': start + pd.Timedelta(seconds=seconds), 'valid': True})
    # 자정 전에 시작했어도 분석일에 끝난 여행은 아직 이용 가능한 과거 완료 로그가 아니다.
    for _ in range(10):
        rows.append({'origin': '1', 'destination': '2', 'start': pd.Timestamp('2025-03-09 23:50'),
                     'end': pd.Timestamp('2025-03-10 00:20'), 'valid': True})
    estimate = estimate_historical_od(pd.DataFrame(rows), '2025-03-10')
    assert estimate[('1', '2')] == {'seconds': 600.0, 'n': 12, 'window_days': 90}


def test_sql_od_and_cross_month_context(tmp_path):
    (tmp_path / 'trips').mkdir()
    (tmp_path / 'inventory').mkdir()
    (tmp_path / 'cross_day_returns' / '2025-02-01').mkdir(parents=True)
    past = pd.DataFrame({
        'request_id': ['old'+str(i) for i in range(10)], 'origin': ['1']*10,
        'destination': ['2']*10, 'start': pd.to_datetime(['2025-01-31 12:00']*10),
        'end': pd.to_datetime(['2025-01-31 12:10']*10), 'valid': [True]*10,
        'duration_seconds': [600.0]*10,
    })
    unfinished = past.copy()
    unfinished['request_id'] = ['unfinished'+str(i) for i in range(10)]
    unfinished['start'] = pd.Timestamp('2025-01-31 23:50')
    unfinished['end'] = pd.Timestamp('2025-02-01 00:20')
    unfinished['duration_seconds'] = 1800.0
    past = pd.concat([past, unfinished], ignore_index=True)
    past.to_parquet(tmp_path / 'trips' / '2025-01-31.parquet', index=False)
    today = past.iloc[:1].copy()
    today['request_id'] = 'new'
    today['start'] = pd.Timestamp('2025-02-01 12:00')
    today['end'] = pd.Timestamp('2025-02-01 12:20')
    today['duration_seconds'] = 1200.0
    today.to_parquet(tmp_path / 'trips' / '2025-02-01.parquet', index=False)
    crossing = past.iloc[:1].copy()
    crossing['request_id'] = 'long-crossing'
    crossing['end'] = pd.Timestamp('2025-02-01 00:01')
    crossing['valid'] = False
    crossing.to_parquet(tmp_path / 'cross_day_returns' / '2025-02-01' / 'previous.parquet', index=False)
    pd.DataFrame({'station': ['1'], 'time': [pd.Timestamp('2025-02-01')],
                  'inventory': [3]}).to_parquet(tmp_path / 'inventory' / '2025-02-01.parquet', index=False)
    assert historical_od(tmp_path, '2025-02-01')[('1', '2')]['seconds'] == 600.0
    context, inventory = load_day_context(tmp_path, '2025-02-01')
    assert set(context.request_id) == {'new', 'long-crossing'}
    assert len(inventory) == 1


def test_registry_requires_five_stock_points_not_a_sixth(tmp_path):
    for name in ['trips', 'inventory', 'stations']:
        (tmp_path / name).mkdir()
    manifest = {'joined_dates': ['2025-01-01'], 'trip_dates': ['2025-01-01']}
    (tmp_path / 'manifest.json').write_text(json.dumps(manifest), encoding='utf-8')
    pd.DataFrame({'station': ['1', '2'], 'district': ['A', 'A'],
                  'lat': [37., 37.], 'lon': [127., 127.]}).to_parquet(
        tmp_path / 'stations' / '2025-01-01.parquet', index=False)
    pd.DataFrame({'origin': ['1', '1'], 'destination': ['2', '2'],
                  'start': pd.to_datetime(['2025-01-01 19:00', '2025-01-01 20:00']),
                  'valid': [True, True]}).to_parquet(tmp_path / 'trips' / '2025-01-01.parquet', index=False)
    pd.DataFrame({'station': ['1']*24,
                  'time': pd.date_range('2025-01-01', periods=24, freq='h'),
                  'inventory': [1]*24}).to_parquet(tmp_path / 'inventory' / '2025-01-01.parquet', index=False)
    registry = build_window_registry(tmp_path).set_index('hour')
    assert registry.loc[19, 'status'] == 'eligible'
    assert registry.loc[20, 'status'] == 'missing_five_hour_context'
    # 하루 전체 재고가 빠져도 실제 여행이 있는 날짜를 분모에서 지우지 않는다.
    manifest['trip_dates'].append('2025-01-02')
    (tmp_path / 'manifest.json').write_text(json.dumps(manifest), encoding='utf-8')
    trips = pd.read_parquet(tmp_path / 'trips' / '2025-01-01.parquet')
    trips['start'] = trips.start+pd.Timedelta(days=1)
    trips.to_parquet(tmp_path / 'trips' / '2025-01-02.parquet', index=False)
    registry = build_window_registry(tmp_path)
    assert len(registry) == 48
    assert set(registry.loc[registry.date.eq('2025-01-02'), 'status']) == {'missing_five_hour_context'}


def test_hourly_reserve_uses_event_prefix_and_departure_first(tmp_path):
    (tmp_path / 'events' / '2025-01-01').mkdir(parents=True)
    (tmp_path / 'inventory').mkdir()
    pd.DataFrame({'station': ['1']*3,
                  'time': pd.to_datetime(['2025-01-01 00:10']*3),
                  'phase': [0, 0, 1], 'amount': [-1, -1, 3]}).to_parquet(
        tmp_path / 'events' / '2025-01-01' / 'x.parquet', index=False)
    pd.DataFrame({'station': ['1'], 'time': [pd.Timestamp('2025-01-01')],
                  'inventory': [1]}).to_parquet(tmp_path / 'inventory' / '2025-01-01.parquet', index=False)
    risk = pd.read_parquet(build_checkpoint_risk(tmp_path))
    assert risk.required_reserve.tolist() == [2]
    assert risk.deficit.tolist() == [1]
