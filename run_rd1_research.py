"""실측 RD-1.0 로컬 연구 실행기. QPU·계정·노트북 실행 경로가 없다.

기본 실행은 전체 창 등록부에서 고정한 월별·규모별 탐색 실행군을 처리한다.
--all-windows는 등록된 모든 적격창을 실행한다(매우 긴 시간이 필요할 수 있다).
원본·과거 결과는 수정하지 않으며 완료 사례는 --resume으로 재사용한다.
"""
from __future__ import annotations

import argparse
import gc
import hashlib
import json
import platform
from pathlib import Path
import sys
import time
import traceback

import pandas as pd

from bike_research.campaign import (assign_hardness, canonical_hash,
                                    freeze_execution_cohort, select_general_group)
from bike_research.data import dump_json, monthly_ranking, daily_ranking, source_record

ROOT = Path(__file__).resolve().parent


def _code_hashes():
    paths = [Path(__file__), *sorted((ROOT / 'bike_research').glob('*.py'))]
    return {p.relative_to(ROOT).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in paths if p.name != 'qpu.py'}


def _peak_windows(registry, data_root):
    """명세의 월 최대 → 상위 2일 → 상위 2시간 → 최대 자치구."""
    ranking, sources = monthly_ranking(data_root)
    month = str(ranking.iloc[0]['month'])
    daily = daily_ranking(data_root, month)
    daily_path = next(data_root.glob(f'공공자전거 이용정보(일별)/*_{month[2:]}.csv'))
    sources.append(source_record(daily_path))
    for source in sources:
        digest = hashlib.sha256()
        with Path(source['path']).open('rb') as handle:
            for block in iter(lambda: handle.read(8*1024*1024), b''):
                digest.update(block)
        source['sha256'] = digest.hexdigest()
    windows = []
    for date in daily.head(2).date:
        day = pd.Timestamp(str(date)).strftime('%Y-%m-%d')
        part = registry.loc[registry.date.eq(day)]
        hours = (part.groupby('hour', as_index=False).observed_departures.sum()
                 .sort_values(['observed_departures', 'hour'], ascending=[False, True]).head(2))
        for hour in hours.hour:
            districts = part.loc[part.hour.eq(hour)].sort_values(
                ['observed_departures', 'district'], ascending=[False, True])
            if not districts.empty:
                windows.append({'date': day, 'hour': int(hour), 'district': districts.iloc[0].district})
    return windows, {'month': month, 'daily_top2': [str(v) for v in daily.head(2).date],
                     'sources': sources}


def _save_summary(output):
    rows = [json.loads(path.read_text(encoding='utf-8'))
            for path in sorted((output / 'cases').glob('*/result_summary.json'))]
    if not rows:
        return pd.DataFrame()
    frame = assign_hardness(pd.DataFrame(rows))
    frame.to_csv(output / 'summary.csv', index=False, encoding='utf-8-sig')
    dump_json(output / 'summary.json', json.loads(frame.to_json(orient='records', force_ascii=False)))
    dump_json(output / 'general_group.json', select_general_group(frame))
    return frame


def combine_operational_baselines(benchmark, evaluations):
    """검증된 탐욕 해도 같은 운영 모형의 하계다. 이미 닫힌 격차를 놓치지 않는다."""
    valid = [name for name, result in evaluations.items() if result['checkpoint_feasible']]
    if not valid:
        return
    winner = min(valid, key=lambda name: (-evaluations[name]['selected_count'],
                                         tuple(evaluations[name]['selected_ids'])))
    best = evaluations[winner]
    upper = benchmark['upper_bound']
    if best['selected_count'] > upper:
        raise RuntimeError('검증된 운영 해가 상계보다 큽니다.')
    benchmark.update(lower_bound=best['selected_count'], selected_candidate_ids=best['selected_ids'],
        relative_gap=(upper-best['selected_count'])/max(1, upper),
        optimality_proven=upper == best['selected_count'],
        includes_validated_operational_greedy_baselines=True)
    evaluations['classical_best'] = dict(best)


def _run_one(case, folder, benchmark_config, selection):
    from bike_research.benchmark import process_rd1_case, greedy_operational
    from bike_research.benefit import evaluate_selection

    metadata = case['metadata']
    row = {'case_id': case['case_id'], 'date': metadata['date'], 'hour': metadata['hour'],
           'district': metadata['district'], 'variant': metadata['variant'],
           'split': selection['split'], 'selection_reason': selection['selection_reason'],
           'generation_status': metadata['status'], 'requests': len(case['requests']),
           'candidates': len(case['candidates']), 'constraints': len(case['constraints']),
           'generation_seconds': metadata['generation_seconds'],
           'local_generation_seconds': metadata.get('local_generation_seconds', metadata['generation_seconds']),
           'shared_context_seconds': metadata.get('shared_context_seconds', 0),
           'travel_eligible_candidates': metadata.get('travel_eligible_candidates', 0),
           'encoding_status': 'not_attempted', 'residual_lcc': None,
           'fastest_proof_seconds': None, 'full_exact_runs': False,
           'operational_optimal': False, 'operational_gap': None,
           'operational_objective': None, 'operational_upper_bound': None,
           'exact_nodes': None, 'exact_edges': None, 'candidate_nodes': None,
           'candidate_edges': None, 'portfolio_solver_seconds': 0,
           'snapshot_semantics_assumed': True, 'qpu_executed': False}
    if metadata['status'] in ('generation_limit', 'data_ineligible'):
        dump_json(folder / 'case.json', case)
        row['failure_reason'] = metadata.get('generation_limit_reason', metadata['status'])
    else:
        result = process_rd1_case(case, folder, benchmark_config)
        row['encoding_status'] = 'exact' if result['status'] == 'ready' else result['status']
        row['failure_reason'] = result.get('reason', '')
        row['construction_seconds'] = result.get('construction_seconds', 0)
        row['structure_audit_seconds'] = result.get('structure_audit_seconds', 0)
        for filename, prefix in [('graph.json', 'candidate'), ('exact_graph.json', 'exact')]:
            path = folder / filename
            if path.exists():
                graph = json.loads(path.read_text(encoding='utf-8'))
                row[prefix+'_nodes'], row[prefix+'_edges'] = len(graph['nodes']), len(graph['edges'])
                if prefix == 'candidate':
                    row['candidate_representation'] = graph['metadata']['representation']
                del graph
        audit_path = folder / 'structure_audit.json'
        if audit_path.exists():
            audit = json.loads(audit_path.read_text(encoding='utf-8'))
            row['residual_lcc'] = audit['reduction']['largest_residual_component']
            row['easy_certificate_verified'] = audit.get('certificate_verified', False)
        benchmark = result.get('benchmark', {})
        row.update(operational_objective=benchmark.get('lower_bound'),
                   operational_upper_bound=benchmark.get('upper_bound'),
                   operational_gap=benchmark.get('relative_gap'),
                   operational_optimal=benchmark.get('optimality_proven', False),
                   full_exact_runs=benchmark.get('all_required_exact_runs_normal', False),
                   fastest_proof_seconds=benchmark.get('fastest_exact_median_seconds'),
                   portfolio_solver_seconds=benchmark.get('portfolio_solver_seconds', 0),
                   portfolio_model_seconds=benchmark.get('portfolio_model_seconds', 0),
                   portfolio_lift_decode_seconds=benchmark.get('portfolio_lift_decode_seconds', 0))
        policy_started = time.perf_counter()
        policies = {'original': [], 'greedy_id': greedy_operational(case),
                    'greedy_benefit': greedy_operational(case, by_benefit=True),
                    'classical_best': benchmark.get('selected_candidate_ids', [])}
        evaluations = {name: evaluate_selection(case, ids) for name, ids in policies.items()}
        for name, ids in policies.items():
            evaluations[name]['selected_ids'] = ids
        combine_operational_baselines(benchmark, evaluations)
        dump_json(folder / 'classical_benchmark.json', benchmark)
        row.update(operational_objective=benchmark['lower_bound'],
                   operational_gap=benchmark['relative_gap'],
                   operational_optimal=benchmark['optimality_proven'])
        dump_json(folder / 'operational_evaluation.json', evaluations)
        row['policy_evaluation_seconds'] = time.perf_counter()-policy_started
        best = evaluations['classical_best']
        row.update(baseline_j=best['baseline_j'], selected_j=best['selected_j'],
                   benefit=best['benefit'], event_feasible=best['event_feasible'],
                   strict_event_subgroup=best['strict_event_subgroup'],
                   event_baseline_feasible=best['event_baseline_feasible'],
                   total_walk_m=best['total_extra_walk_straight_m'],
                   operational_risk=best['baseline_j'] > 0,
                   checkpoint_feasible=best['checkpoint_feasible'])
        for name in ['original', 'greedy_id', 'greedy_benefit']:
            row[name+'_count'] = evaluations[name]['selected_count']
            row[name+'_benefit'] = evaluations[name]['benefit']
    dump_json(folder / 'result_summary.json', row)
    return row


def run(args):
    from bike_research.rd1_data import (prepare_rd1_data, build_window_registry,
        load_day_context, load_station_snapshot, historical_od)
    from bike_research.rd1_cases import create_rd1_case, DEFAULT_CONFIG
    from bike_research.benefit import build_window_context, GenerationLimit
    from bike_research.cases import neighbours
    from bike_research.benchmark import DEFAULTS

    output, cache = args.output.resolve(), args.cache.resolve()
    output.mkdir(parents=True, exist_ok=True)
    started = time.perf_counter()
    manifest_path = cache / 'manifest.json'
    data_manifest = (json.loads(manifest_path.read_text(encoding='utf-8')) if manifest_path.exists()
                     else prepare_rd1_data(ROOT / 'origin_data', cache))
    registry_path = cache / 'window_registry.csv'
    registry = pd.read_csv(registry_path) if registry_path.exists() else build_window_registry(cache)
    registry.to_csv(output / 'window_registry.csv', index=False, encoding='utf-8-sig')
    if args.prepare_only:
        return
    config = {'protocol': 'RD-1.0', 'candidate': DEFAULT_CONFIG, 'benchmark': DEFAULTS,
              'seed': 20261109, 'all_windows': args.all_windows,
              'variants': ['primary', 'benefit_filter_off'], 'qpu_executed': False,
              'input_source_signature': data_manifest['source_signature'],
              'input_content_sha256': canonical_hash({'sources': data_manifest['sources']}),
              'execution_scope': 'all_eligible_windows' if args.all_windows else 'frozen_monthly_size_exploration',
              'pilot': args.pilot}
    config_hash = canonical_hash(config)
    run_manifest_path = output / 'manifest.json'
    if run_manifest_path.exists():
        old = json.loads(run_manifest_path.read_text(encoding='utf-8'))
        if not args.resume or old['config_hash'] != config_hash:
            raise ValueError('기존 결과를 덮어쓰지 않습니다. 동일 설정이면 --resume, 새 설정이면 새 출력 경로를 사용하세요.')
        cohort = pd.read_csv(output / 'execution_cohort.csv')
        if old['code_sha256'] != _code_hashes():
            raise ValueError('완료 결과와 코드 해시가 다릅니다. 새 출력 경로를 사용하세요.')
    else:
        peaks, peak_info = _peak_windows(registry, ROOT / 'origin_data')
        if args.all_windows:
            cohort = registry.loc[registry.status.eq('eligible')].copy()
            cohort['selection_reason'] = 'all_eligible'
        else:
            cohort = freeze_execution_cohort(registry, peak_windows=peaks)
        if args.pilot:
            cohort = registry.loc[registry.status.eq('eligible') & registry.date.eq('2025-06-05')
                                  & registry.hour.eq(8) & registry.district.eq('강서구')].copy()
            cohort['selection_reason'] = 'development_integration_pilot'
        cohort.to_csv(output / 'execution_cohort.csv', index=False, encoding='utf-8-sig')
        dump_json(run_manifest_path, {'config': config, 'config_hash': config_hash,
            'code_sha256': _code_hashes(), 'registry_windows': len(registry),
            'registry_eligible_windows': int(registry.status.eq('eligible').sum()),
            'selected_windows': len(cohort), 'expected_variant_cases': 2*len(cohort),
            'peak_rule': peak_info, 'platform': platform.platform(),
            'python': sys.version, 'qpu_executed': False})
    completed, failed = 0, 0
    for day, day_windows in cohort.groupby('date', sort=True):
        print(json.dumps({'stage': 'day', 'day': day, 'windows': len(day_windows)}, ensure_ascii=False), flush=True)
        trips, inventory = load_day_context(cache, day)
        stations, station_metadata = load_station_snapshot(cache, day)
        od = historical_od(cache, day)
        nearby = neighbours(stations, radius_m=500, alternatives=None)
        for hour, hour_windows in day_windows.groupby('hour', sort=True):
            anchor = pd.Timestamp(day) + pd.Timedelta(hours=int(hour))
            context_error = None
            context_started = time.perf_counter()
            try:
                context = build_window_context(trips, inventory, stations, anchor)
            except GenerationLimit as error:
                context = None
                context_error = str(error)
            context_seconds = time.perf_counter()-context_started
            for selection in hour_windows.to_dict('records'):
                for variant in config['variants']:
                    parameters = {'benefit_filter': variant == 'primary',
                                  '_window_context': context, '_neighbours': nearby}
                    # 완료 여부를 조회하려고 후보를 다시 생성하지 않도록 경로 키를 먼저 만든다.
                    key = f'{day}_{int(hour):02}_{selection["district"]}_{variant}'
                    folder = output / 'cases' / key
                    if (folder / 'result_summary.json').exists():
                        completed += 1
                        continue
                    before = time.perf_counter()
                    try:
                        if context_error:
                            case = {'case_id': 'RD1_'+key, 'requests': [], 'candidates': [],
                                'constraints': [], 'metadata': {'date': day, 'hour': int(hour),
                                'district': selection['district'], 'status': 'generation_limit',
                                'generation_limit_reason': context_error,
                                'generation_seconds': context_seconds}}
                        else:
                            case = create_rd1_case(trips, inventory, stations, day, int(hour),
                                                   selection['district'], od, parameters)
                        case['metadata'].update(variant=variant, split=selection['split'],
                                                station_metadata=station_metadata,
                                                campaign_config_hash=config_hash)
                        row = _run_one(case, folder, DEFAULTS, selection)
                        row['case_total_wall_seconds'] = time.perf_counter()-before
                        dump_json(folder / 'result_summary.json', row)
                        completed += 1
                        print(json.dumps({'stage': 'case_complete', 'case': key,
                            'candidates': row['candidates'], 'encoding': row['encoding_status'],
                            'optimal': row['operational_optimal'], 'seconds': round(time.perf_counter()-before, 2)},
                            ensure_ascii=False), flush=True)
                        del case
                    except Exception as error:
                        failed += 1
                        dump_json(folder / 'execution_error.json', {'error': str(error),
                            'type': type(error).__name__, 'traceback': traceback.format_exc(),
                            'window': selection, 'variant': variant, 'qpu_executed': False})
                        print(json.dumps({'stage': 'execution_error', 'case': key, 'error': str(error)}, ensure_ascii=False), flush=True)
                        # 구현 오류를 수백 번 복제하지 않고 고쳐 새 실행을 시작한다.
                        raise
                    finally:
                        gc.collect()
                    dump_json(output / 'progress.json', {'completed_cases': completed,
                        'failed_cases': failed, 'expected_cases': 2*len(cohort),
                        'last_case': key, 'elapsed_seconds': time.perf_counter()-started,
                        'qpu_executed': False})
            del context
        _save_summary(output)
        del trips, inventory, stations, od, nearby
        gc.collect()
    summary = _save_summary(output)
    dump_json(output / 'completion.json', {'completed_cases': len(summary),
        'expected_cases': 2*len(cohort), 'failed_cases': failed,
        'relative_hard_cases': int(summary.relative_hard.sum()) if len(summary) else 0,
        'unresolved_cases': int(summary.unresolved.sum()) if len(summary) else 0,
        'elapsed_seconds': time.perf_counter()-started, 'qpu_executed': False})


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--cache', type=Path, default=ROOT / 'outputs/rd1_data_cache')
    parser.add_argument('--output', type=Path, default=ROOT / 'outputs/rd1_main')
    parser.add_argument('--resume', action='store_true')
    parser.add_argument('--prepare-only', action='store_true')
    parser.add_argument('--all-windows', action='store_true')
    parser.add_argument('--pilot', action='store_true')
    run(parser.parse_args())
