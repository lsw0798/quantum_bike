"""완료된 실측 실행을 날짜 단위로 요약하고 로컬 보조 검증을 수행한다.

임의 비트 기준선은 실제 정확 그래프를 재사용한다. QPU 실행은 포함하지 않는다.
"""
from __future__ import annotations

import argparse
from datetime import datetime
import hashlib
import json
from pathlib import Path

import numpy as np
import pandas as pd

from bike_research.data import dump_json
from bike_research.evaluation import (paired_date_bootstrap, random_graph_baseline,
                                      event_path_sensitivity)
from bike_research.rd1_data import load_day_context, load_station_snapshot
from bike_research.benefit import build_window_context

ROOT = Path(__file__).resolve().parent
VALID_GENERATION_STATUSES = {'complete', 'no_beneficial_candidate', 'no_travel_eligible_candidate'}
WINDOW_KEYS = ['date', 'hour', 'district']


def load(path):
    return json.loads(Path(path).read_text(encoding='utf-8'))


def folder_for(root, row):
    return root / 'cases' / f'{row["date"]}_{int(row["hour"]):02}_{row["district"]}_{row["variant"]}'


def valid_generation_mask(frame, column='generation_status'):
    """생성 중단 시 폐기된 후보 0개를 실제 관측 0개로 세지 않는다."""
    values = frame.get(column, pd.Series(index=frame.index, dtype=object))
    return values.isin(VALID_GENERATION_STATUSES)


def generated_paired_statistics(frame, left, right, *, left_status='generation_status',
                                right_status=None):
    """같은 생성 완료 쌍만 비교하고 전체 실행군의 결측 분모를 보존한다.

    제외 사유 세 항목은 생성 미완료 → 수치 결측 → 날짜 결측 순으로
    서로 겹치지 않는다. 좌우 생성 상태 집계는 별도로 제공한다.
    """
    right_status = right_status or left_status
    generated = valid_generation_mask(frame, left_status) & valid_generation_mask(frame, right_status)
    a = pd.to_numeric(frame[left], errors='coerce')
    b = pd.to_numeric(frame[right], errors='coerce')
    finite = (np.isfinite(a) & np.isfinite(b)).fillna(False)
    dated = frame['date'].notna()
    masked = frame[['date']].copy()
    masked[left], masked[right] = a.where(generated), b.where(generated)
    result = paired_date_bootstrap(masked, left, right)
    result['denominators'] = {
        'cohort_rows': len(frame),
        'generation_complete_pair_rows': int(generated.sum()),
        'excluded_generation_rows': int((~generated).sum()),
        'excluded_nonfinite_metric_rows': int((generated & ~finite).sum()),
        'excluded_missing_date_rows': int((generated & finite & ~dated).sum()),
        'paired_rows': result['paired_rows'],
        'left_generation_status': frame.get(left_status, pd.Series(index=frame.index, dtype=object))
            .fillna('<missing>').astype(str).value_counts().to_dict(),
        'right_generation_status': frame.get(right_status, pd.Series(index=frame.index, dtype=object))
            .fillna('<missing>').astype(str).value_counts().to_dict(),
    }
    return result


def _scope_statistics(statistics, split):
    for result in statistics.values():
        result['population_scope'] = 'frozen_execution_cohort_not_Seoul_population'
        result['split'] = split
        result['interpretation_scope'] = ('pooled_exploratory_description'
                                          if split == 'pooled' else split + '_date_cohort')
    return statistics


def collect_main_statistics(frame, *, split='pooled'):
    """기존 통계 키를 유지하며 생성 상태와 짝이 없는 창을 분모에 남긴다."""
    primary = frame.loc[frame.variant.eq('primary')]
    statistics = {}
    for policy in ['original', 'greedy_id', 'greedy_benefit']:
        for reference, observed, metric in [(policy+'_count', 'operational_objective', 'recommendations'),
                                            (policy+'_benefit', 'benefit', 'benefit')]:
            statistics[policy+'_'+metric] = generated_paired_statistics(primary, reference, observed)
    off = frame.loc[frame.variant.eq('benefit_filter_off')]
    paired = primary.merge(off, on=WINDOW_KEYS, how='outer', suffixes=('_primary', '_off'),
                           validate='one_to_one')
    both_splits = paired.split_primary.notna() & paired.split_off.notna()
    if paired.loc[both_splits, 'split_primary'].ne(paired.loc[both_splits, 'split_off']).any():
        raise ValueError('같은 창의 조건들이 서로 다른 개발/평가 분할에 속합니다.')
    for metric in ['candidates', 'operational_objective', 'benefit']:
        result = generated_paired_statistics(paired, metric+'_primary', metric+'_off',
                    left_status='generation_status_primary', right_status='generation_status_off')
        result['interpretation'] = 'different_candidate_models_not_same_problem_solver_advantage'
        statistics['filter_off_'+metric] = result
    return _scope_statistics(statistics, split)


def collect_statistics_by_split(frame):
    return {split: collect_main_statistics(frame.loc[frame.split.eq(split)], split=split)
            for split in ['development', 'evaluation']}


def collect_sensitivity_statistics(primary, sensitivity, windows, variants):
    """사전 고정 월별 창에서 변형−주조건을 비교하며 누락 변형도 보존한다."""
    columns = WINDOW_KEYS + ['generation_status', 'candidates', 'operational_objective', 'benefit']
    frozen = windows[WINDOW_KEYS + ['split']].copy()
    if frozen.duplicated(WINDOW_KEYS).any():
        raise ValueError('민감도 고정 창에 중복이 있습니다.')
    reference = frozen.merge(primary.reindex(columns=columns), on=WINDOW_KEYS, how='left',
                             validate='one_to_one')
    results = {'scope': 'fixed_monthly_volume_sensitivity_cohort',
               'expected_windows': len(frozen), 'variants': {}}
    for variant in variants:
        condition = sensitivity.loc[sensitivity.variant.eq(variant)]
        for side in [primary, condition]:
            checked = frozen.merge(side[WINDOW_KEYS + ['split']], on=WINDOW_KEYS, how='inner',
                                   suffixes=('_frozen', '_observed'), validate='one_to_one')
            if checked.split_frozen.ne(checked.split_observed).any():
                raise ValueError('민감도 사례의 날짜 분할이 고정 창과 다릅니다.')
        paired = reference.merge(condition.reindex(columns=columns), on=WINDOW_KEYS, how='left',
                                 suffixes=('_primary', '_condition'), validate='one_to_one')
        results['variants'][variant] = {}
        for split in ['pooled', 'development', 'evaluation']:
            group = paired if split == 'pooled' else paired.loc[paired.split.eq(split)]
            statistics = {}
            for metric in ['candidates', 'operational_objective', 'benefit']:
                result = generated_paired_statistics(group, metric+'_primary', metric+'_condition',
                    left_status='generation_status_primary', right_status='generation_status_condition')
                result['interpretation'] = 'different_candidate_models_not_same_problem_solver_advantage'
                statistics[metric] = result
            results['variants'][variant][split] = _scope_statistics(statistics, split)
    return results


def normalize_artifact_newlines(folder):
    """저장된 LF 정규형 해시와 Windows 파일 바이트를 일치시킨다.

    JSON의 문자열 안 줄바꿈은 이스케이프되어 있으므로 실제 CRLF는 서식
    공백이다. 원자료와 숫자·선택·논리 그래프는 바꾸지 않는다.
    """
    records = []
    for path in sorted((folder / 'cases').glob('*/*.json')):
        raw = path.read_bytes()
        normalized = raw.replace(b'\r\n', b'\n')
        before = hashlib.sha256(raw).hexdigest()
        after = hashlib.sha256(normalized).hexdigest()
        if raw != normalized:
            temporary = path.with_suffix('.json.lf.tmp')
            temporary.write_bytes(normalized)
            temporary.replace(path)
        records.append({'path': str(path.relative_to(folder)), 'before_sha256': before,
                        'after_sha256': after, 'format_only_changed': before != after})
    checked = 0
    for summary_path in (folder / 'cases').glob('*/build_summary.json'):
        build = load(summary_path)
        for filename, key in [('case.json', 'case_sha256'), ('graph.json', 'candidate_graph_sha256'),
                              ('exact_graph.json', 'exact_graph_sha256')]:
            if key in build:
                actual = hashlib.sha256((summary_path.parent / filename).read_bytes()).hexdigest()
                if actual != build[key]:
                    raise ValueError(f'정규형 파일 해시 불일치: {summary_path.parent / filename}')
                checked += 1
    return {'files': records, 'bound_file_hashes_verified': checked,
            'policy': 'CRLF_to_LF_format_only_after_completed_computation'}


def freeze_small_graphs(primary, seed=20261109):
    """향후 QPU 논리 규모군을 정하되 실제 배치 가능성은 주장하지 않는다."""
    frame = primary.loc[primary.split.eq('evaluation') & primary.encoding_status.eq('exact')
                        & primary.exact_nodes.between(1, 80)].sort_values('case_id').copy()
    frame['node_band'] = ((frame.exact_nodes.astype(int)-1)//20).astype(int)
    rng, selected = np.random.default_rng(seed), []
    # 고난도/일반 각 최대 6개. 어떤 그룹이 비어도 다른 그룹으로 채우지 않는다.
    for hard in [True, False]:
        group = frame.loc[(frame.relative_hard | frame.unresolved).eq(hard)]
        buckets = {band: list(rng.permutation(part.index)) for band, part in group.groupby('node_band')}
        count = 0
        while count < 6 and any(buckets.values()):
            for band in range(4):
                if buckets.get(band) and count < 6:
                    selected.append(buckets[band].pop())
                    count += 1
    return frame.loc[selected].reset_index(drop=True)


def main(args):
    args.output.mkdir(parents=True, exist_ok=True)
    completion = load(args.main / 'completion.json')
    if completion['completed_cases'] != completion['expected_cases'] or completion.get('failed_cases', 0):
        raise ValueError('완료되지 않은 주 실행을 전체 완료 결과로 요약할 수 없습니다.')
    normalization = {'main': normalize_artifact_newlines(args.main)}
    sensitivity_completion = None
    if (args.sensitivity / 'completion.json').exists():
        sensitivity_completion = load(args.sensitivity / 'completion.json')
        if (sensitivity_completion['completed_cases'] != sensitivity_completion['expected_cases']
                or sensitivity_completion.get('failed_cases', 0)):
            raise ValueError('완료되지 않은 민감도 실행을 전체 완료 결과로 요약할 수 없습니다.')
        normalization['sensitivity'] = normalize_artifact_newlines(args.sensitivity)
    dump_json(args.output / 'artifact_integrity.json', normalization)
    main_frame = pd.DataFrame(load(args.main / 'summary.json'))
    primary = main_frame.loc[main_frame.variant.eq('primary')].copy()
    dump_json(args.output / 'statistics.json', collect_main_statistics(main_frame))
    dump_json(args.output / 'statistics_by_split.json', collect_statistics_by_split(main_frame))
    sensitivity_statistics = {'status': 'sensitivity_not_completed', 'variants': {}}
    if sensitivity_completion is not None:
        sensitivity_frame = pd.DataFrame(load(args.sensitivity / 'summary.json'))
        sensitivity_windows = pd.read_csv(args.sensitivity / 'execution_cohort.csv')
        sensitivity_manifest = load(args.sensitivity / 'manifest.json')
        sensitivity_statistics = collect_sensitivity_statistics(primary, sensitivity_frame,
                                    sensitivity_windows, sensitivity_manifest['variants'])
    dump_json(args.output / 'sensitivity_statistics.json', sensitivity_statistics)
    keys = WINDOW_KEYS

    small = freeze_small_graphs(primary)
    small.to_csv(args.output / 'future_logical_size_cohort.csv', index=False, encoding='utf-8-sig')
    random_results = []
    for row in small.to_dict('records'):
        folder = folder_for(args.main, row)
        graph, case = load(folder / 'exact_graph.json'), load(folder / 'case.json')
        batches = [random_graph_baseline(graph, case, shots=500, seed=20261109+i) for i in range(5)]
        random_results.append({'case_id': row['case_id'], 'date': row['date'],
            'logical_nodes': row['exact_nodes'], 'classical_lower_bound': row['operational_objective'],
            'classical_optimum': row['operational_objective'] if row['operational_optimal'] else None,
            'batches': batches, 'hardware_certified': False, 'qpu_executed': False})
    dump_json(args.output / 'random_baselines.json', random_results)

    # 월 최대 원래 이용 창을 풀이 결과와 무관하게 선택한다. 주 추천은 고정한다.
    cohort = pd.read_csv(args.main / 'execution_cohort.csv')
    cohort['month'] = cohort.date.str[:7]
    selected = (cohort.sort_values(['eligible_requests', *keys], ascending=[False, True, True, True])
                .groupby('month', sort=True).head(1).sort_values(keys))
    events = []
    for window in selected.to_dict('records'):
        row = {**window, 'variant': 'primary'}
        folder = folder_for(args.main, row)
        case = load(folder / 'case.json')
        if case['metadata']['status'] not in VALID_GENERATION_STATUSES:
            events.append({'date': row['date'], 'hour': row['hour'], 'district': row['district'],
                           'status': 'generation_incomplete'})
            continue
        solution = load(folder / 'classical_benchmark.json')['selected_candidate_ids']
        trips, stock = load_day_context(args.cache, row['date'])
        stations, _ = load_station_snapshot(args.cache, row['date'])
        context = build_window_context(trips, stock, stations,
                    pd.Timestamp(row['date'])+pd.Timedelta(hours=int(row['hour'])))
        events.append({'case_id': case['case_id'], 'date': row['date'], 'hour': row['hour'],
                       'district': row['district'], 'status': 'complete',
            'departure_first': event_path_sensitivity(context, case, solution),
            'return_first': event_path_sensitivity(context, case, solution, returns_first=True),
            'return_delay_60s': event_path_sensitivity(context, case, solution, return_delay_seconds=60)})
    dump_json(args.output / 'event_sensitivity.json', events)

    source_manifest = load(args.cache / 'manifest.json')
    evidence = {'generated_at': datetime.now().astimezone().isoformat(),
        'main_completion': completion, 'main_manifest': load(args.main / 'manifest.json'),
        'source_audit': {'trip': source_manifest['trip_audit'], 'inventory': source_manifest['inventory_audit']},
        'variants': {}, 'qpu_executed': False, 'qpu_cost': 0,
        'postprocessor_sha256': hashlib.sha256(Path(__file__).read_bytes()).hexdigest()}
    for variant, group in main_frame.groupby('variant'):
        evidence['variants'][variant] = {
            'cases': len(group), 'dates': group.date.nunique(), 'candidate_max': int(group.candidates.max()),
            'nonempty_cases': int(group.candidates.gt(0).sum()),
            'generation_status': group.generation_status.value_counts().to_dict(),
            'encoding_status': group.encoding_status.value_counts().to_dict(),
            'optimality_proven': int(group.operational_optimal.sum()),
            'relative_hard': int(group.relative_hard.sum()), 'unresolved': int(group.unresolved.sum()),
            'strict_event_subgroup': int(group.strict_event_subgroup.fillna(False).sum()),
            'event_feasible': int(group.event_feasible.fillna(False).sum()),
            'negative_benefit': int(group.benefit.lt(0).sum()),
            'positive_benefit': int(group.benefit.gt(0).sum()),
            'zero_benefit': int(group.benefit.eq(0).sum()),
            'full_exact_runs': int(group.full_exact_runs.sum()),
            'generation_seconds': float(group.generation_seconds.sum()),
            'portfolio_solver_seconds': float(group.portfolio_solver_seconds.sum())}
    sensitivity_path = args.sensitivity / 'completion.json'
    if sensitivity_path.exists():
        evidence['sensitivity_completion'] = load(sensitivity_path)
    dump_json(args.output / 'evidence.json', evidence)
    print(json.dumps({'main_cases': len(main_frame), 'random_graph_cases': len(small),
                      'event_sensitivity_cases': len(events), 'qpu_executed': False}, ensure_ascii=False))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--main', type=Path, default=ROOT / 'outputs/rd1_main')
    parser.add_argument('--sensitivity', type=Path, default=ROOT / 'outputs/rd1_sensitivity')
    parser.add_argument('--cache', type=Path, default=ROOT / 'outputs/rd1_data_cache')
    parser.add_argument('--output', type=Path, default=ROOT / 'outputs/rd1_postanalysis')
    main(parser.parse_args())
