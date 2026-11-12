"""고정된 월별 최대 이용 창에서 한 번에 한 조건만 바꿔 로컬 검증한다.

주 실행군 전체에 적용한 편익 필터 해제 비교와 별개인 제한된 민감도 실험이다.
QPU 실행 기능이 없으며 변경 조건마다 사례·그래프·고전 난도를 다시 계산한다.
"""
from pathlib import Path
import argparse
import hashlib
import json
import time

import pandas as pd

from bike_research.data import dump_json
from bike_research.rd1_data import load_day_context, load_station_snapshot, historical_od
from bike_research.rd1_cases import create_rd1_case
from bike_research.benefit import build_window_context, GenerationLimit
from bike_research.cases import neighbours
from bike_research.benchmark import DEFAULTS
from bike_research.campaign import canonical_hash
from run_rd1_research import ROOT, _run_one, _save_summary, _code_hashes


VARIANTS = {
    'walk_200m': {'radius_m': 200.0},
    'walk_750m': {'radius_m': 750.0},
    'walk_1000m': {'radius_m': 1000.0},
    'speed_1_0': {'walking_m_s': 1.0},
    'speed_1_4': {'walking_m_s': 1.4},
    'delay_5min': {'max_extra_seconds': 300},
    'delay_30min': {'max_extra_seconds': 1800},
    'od_min5': {'minimum_od_observations': 5},
    'od_min20': {'minimum_od_observations': 20},
    'distance_time_proxy': {'time_model': 'observed_duration_plus_straight_distance_delta'},
}


def main(args):
    args.output.mkdir(parents=True, exist_ok=True)
    cohort = pd.read_csv(args.main / 'execution_cohort.csv')
    cohort['month'] = cohort.date.str[:7]
    # 문제를 푼 뒤 결과가 좋은 사례를 고르지 않는다. 원래 요청 수만 사용한다.
    selected = (cohort.sort_values(['eligible_requests', 'date', 'hour', 'district'],
                                  ascending=[False, True, True, True])
                .groupby('month', sort=True).head(1).sort_values(['date', 'hour', 'district']))
    selection = selected.to_dict('records')
    specification = {'protocol': 'RD-1.0', 'variants': VARIANTS, 'windows': selection,
        'scope': 'monthly_largest_registered_window_in_frozen_main_cohort',
        'main_cohort_sha256': canonical_hash({'rows': cohort.to_dict('records')}),
        'code_sha256': _code_hashes(), 'qpu_executed': False}
    specification['driver_sha256'] = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
    digest = canonical_hash(specification)
    manifest = args.output / 'manifest.json'
    if manifest.exists():
        previous = json.loads(manifest.read_text(encoding='utf-8'))
        if not args.resume or previous['config_hash'] != digest:
            raise ValueError('기존 결과와 설정/코드가 다릅니다. 새 출력 경로를 사용하세요.')
    else:
        dump_json(manifest, {**specification, 'config_hash': digest,
                            'expected_variant_cases': len(selected)*len(VARIANTS)})
        selected.to_csv(args.output / 'execution_cohort.csv', index=False, encoding='utf-8-sig')
    started = time.perf_counter()
    for row in selection:
        day, hour, district = row['date'], int(row['hour']), row['district']
        trips, inventory = load_day_context(args.cache, day)
        stations, station_metadata = load_station_snapshot(args.cache, day)
        context_started, context_error = time.perf_counter(), None
        try:
            context = build_window_context(trips, inventory, stations, pd.Timestamp(day)+pd.Timedelta(hours=hour))
        except GenerationLimit as error:
            context, context_error = None, str(error)
        context_seconds = time.perf_counter()-context_started
        ods, nearby = {}, {}
        for variant, parameters in VARIANTS.items():
            case_started = time.perf_counter()
            key = f'{day}_{hour:02}_{district}_{variant}'
            folder = args.output / 'cases' / key
            if (folder / 'result_summary.json').exists():
                continue
            count, radius = parameters.get('minimum_od_observations', 10), parameters.get('radius_m', 500.0)
            if count not in ods:
                ods[count] = historical_od(args.cache, day, min_count=count)
            if radius not in nearby:
                nearby[radius] = neighbours(stations, radius_m=radius, alternatives=None)
            if context_error:
                case = {'case_id': 'RD1_'+key, 'requests': [], 'candidates': [],
                    'constraints': [], 'metadata': {'date': day, 'hour': hour, 'district': district,
                    'status': 'generation_limit', 'generation_limit_reason': context_error,
                    'generation_seconds': context_seconds}}
            else:
                case = create_rd1_case(trips, inventory, stations, day, hour, district, ods[count],
                                      {**parameters, '_window_context': context, '_neighbours': nearby[radius]})
            case['metadata'].update(variant=variant, split=row['split'], station_metadata=station_metadata,
                                    campaign_config_hash=digest)
            selection_row = {**row, 'selection_reason': 'monthly_volume_fixed_sensitivity'}
            result = _run_one(case, folder, DEFAULTS, selection_row)
            result['case_total_wall_seconds'] = time.perf_counter()-case_started
            dump_json(folder / 'result_summary.json', result)
            print(json.dumps({'case': key, 'candidates': result['candidates'],
                'encoding': result['encoding_status'], 'optimal': result['operational_optimal']}, ensure_ascii=False), flush=True)
            _save_summary(args.output)
    summary = _save_summary(args.output)
    dump_json(args.output / 'completion.json', {'completed_cases': len(summary),
        'expected_cases': len(selected)*len(VARIANTS), 'elapsed_seconds': time.perf_counter()-started,
        'qpu_executed': False})


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--main', type=Path, default=ROOT / 'outputs/rd1_main')
    parser.add_argument('--cache', type=Path, default=ROOT / 'outputs/rd1_data_cache')
    parser.add_argument('--output', type=Path, default=ROOT / 'outputs/rd1_sensitivity')
    parser.add_argument('--resume', action='store_true')
    main(parser.parse_args())
