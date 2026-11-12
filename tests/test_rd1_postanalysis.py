"""Small summary-table checks only; never execute a campaign or solver."""
import numpy as np
import pandas as pd
import hashlib
import json

import analyze_rd1_results as analysis


def test_incomplete_generation_zero_is_missing_but_completed_zero_is_observed():
    frame = pd.DataFrame({
        'date': ['2025-01-01'] * 4,
        'left': [10, 0, 8, 0], 'right': [11, 9, 0, 0],
        'left_status': ['complete', 'generation_limit', 'complete', 'no_beneficial_candidate'],
        'right_status': ['complete', 'complete', 'data_ineligible', 'no_travel_eligible_candidate'],
    })
    result = analysis.generated_paired_statistics(
        frame, 'left', 'right', left_status='left_status', right_status='right_status')
    assert result['paired_rows'] == 2
    assert result['median_paired_difference'] == 0.5
    assert result['excluded_unpaired_or_nonfinite_rows'] == 2
    assert result['denominators']['cohort_rows'] == 4
    assert result['denominators']['excluded_generation_rows'] == 2
    assert result['denominators']['excluded_nonfinite_metric_rows'] == 0


def test_objective_missingness_is_separate_from_generation_failure():
    frame = pd.DataFrame({
        'date': ['2025-01-01'] * 5,
        'left': [0, 0, 3, 0, 5], 'right': [None, 4, np.inf, 0, -2],
        'generation_status': ['complete', 'complete', 'complete', 'generation_limit', 'complete'],
    })
    result = analysis.generated_paired_statistics(frame, 'left', 'right')
    assert result['paired_rows'] == 2
    assert result['median_paired_difference'] == -1.5
    assert result['denominators']['excluded_generation_rows'] == 1
    assert result['denominators']['excluded_nonfinite_metric_rows'] == 2
    assert result['denominators']['excluded_missing_date_rows'] == 0


def test_missing_date_and_unknown_generation_status_fail_closed():
    frame = pd.DataFrame({
        'date': [None, '2025-01-02'], 'left': [1, 0], 'right': [2, 0],
        'generation_status': ['complete', None],
    })
    result = analysis.generated_paired_statistics(frame, 'left', 'right')
    assert result['paired_rows'] == 0
    assert result['median_paired_difference'] is None
    assert result['denominators']['excluded_generation_rows'] == 1
    assert result['denominators']['excluded_missing_date_rows'] == 1


def case_row(date, variant, split, count, *, status='complete'):
    return {'date': date, 'hour': 8, 'district': 'test', 'case_id': date + variant,
            'variant': variant, 'split': split, 'generation_status': status,
            'candidates': count, 'operational_objective': count, 'benefit': count,
            'original_count': 0, 'original_benefit': 0, 'greedy_id_count': count,
            'greedy_id_benefit': count, 'greedy_benefit_count': count,
            'greedy_benefit_benefit': count}


def test_main_comparison_keeps_unmatched_window_in_denominator():
    frame = pd.DataFrame([
        case_row('2025-01-01', 'primary', 'development', 10),
        case_row('2025-01-01', 'benefit_filter_off', 'development', 12),
        case_row('2025-01-02', 'primary', 'development', 0, status='generation_limit'),
    ])
    result = analysis.collect_main_statistics(frame)['filter_off_candidates']
    assert result['paired_rows'] == 1
    assert result['median_paired_difference'] == 2
    assert result['denominators']['cohort_rows'] == 2
    assert result['denominators']['excluded_generation_rows'] == 1


def test_split_statistics_keep_evaluation_separate_and_short_series_descriptive():
    rows = []
    for day in range(1, 11):
        rows.append(case_row(f'2025-01-{day:02}', 'primary', 'development', 100))
        rows.append(case_row(f'2025-01-{day:02}', 'benefit_filter_off', 'development', 100))
    rows.extend([case_row('2025-11-01', 'primary', 'evaluation', 1),
                 case_row('2025-11-01', 'benefit_filter_off', 'evaluation', 1)])
    result = analysis.collect_statistics_by_split(pd.DataFrame(rows))
    development = result['development']['original_recommendations']
    evaluation = result['evaluation']['original_recommendations']
    assert development['median_paired_difference'] == 100
    assert development['ci95'] == [100., 100.]
    assert evaluation['median_paired_difference'] == 1
    assert evaluation['dates'] == 1
    assert evaluation['ci95'] is None
    assert evaluation['status'] == 'descriptive_only_fewer_than_10_dates'


def test_sensitivity_compares_only_frozen_windows_and_preserves_missing_variant():
    primary = pd.DataFrame([
        case_row('2025-01-01', 'primary', 'development', 10),
        case_row('2025-11-01', 'primary', 'evaluation', 20),
        case_row('2025-11-02', 'primary', 'evaluation', 1000),
    ])
    sensitivity = pd.DataFrame([
        case_row('2025-01-01', 'walk_200m', 'development', 7),
        case_row('2025-11-01', 'walk_200m', 'evaluation', 0, status='generation_limit'),
    ])
    windows = primary.iloc[:2][['date', 'hour', 'district', 'split']]
    result = analysis.collect_sensitivity_statistics(
        primary, sensitivity, windows, ['walk_200m', 'speed_1_0'])
    walk = result['variants']['walk_200m']
    assert result['expected_windows'] == 2
    assert walk['pooled']['candidates']['median_paired_difference'] == -3
    assert walk['pooled']['candidates']['denominators']['cohort_rows'] == 2
    assert walk['evaluation']['candidates']['paired_rows'] == 0
    assert walk['evaluation']['candidates']['ci95'] is None
    absent = result['variants']['speed_1_0']['pooled']['candidates']
    assert absent['paired_rows'] == 0
    assert absent['denominators']['excluded_generation_rows'] == 2


def test_repeated_windows_on_one_date_do_not_create_independent_date_blocks():
    frame = pd.DataFrame({'date': ['2025-01-01'] * 20, 'left': [1] * 20,
                          'right': [3] * 20, 'generation_status': ['complete'] * 20})
    result = analysis.generated_paired_statistics(frame, 'left', 'right')
    assert result['paired_rows'] == 20
    assert result['dates'] == 1
    assert result['ci95'] is None


def test_lf_normalization_preserves_json_values_and_matches_recorded_hash(tmp_path):
    folder = tmp_path / 'cases' / 'sample'
    folder.mkdir(parents=True)
    value = {'case_id': 'sample', 'text': 'escaped\r\nvalue', 'selection': ['a'], 'J': 0}
    serialized = json.dumps(value, ensure_ascii=False, indent=2).encode('utf-8')
    (folder / 'case.json').write_bytes(serialized.replace(b'\n', b'\r\n'))
    build = {'case_sha256': hashlib.sha256(serialized).hexdigest()}
    (folder / 'build_summary.json').write_text(json.dumps(build), encoding='utf-8')
    result = analysis.normalize_artifact_newlines(tmp_path)
    assert json.loads((folder / 'case.json').read_bytes()) == value
    assert (folder / 'case.json').read_bytes() == serialized
    assert result['bound_file_hashes_verified'] == 1
