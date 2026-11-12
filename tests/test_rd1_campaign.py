"""실행군의 선택·재현성과 고난도 판정 단위를 검증한다."""
import pandas as pd

from bike_research.campaign import freeze_execution_cohort, assign_hardness


def test_frozen_selection_is_order_invariant_and_preserves_failures():
    rows = [dict(date=f'2025-06-{day:02d}', hour=8, district='강서구',
                 eligible_requests=64, status='eligible', split='development')
            for day in range(1, 26)]
    rows.append(dict(date='2025-06-26', hour=8, district='강서구',
                     eligible_requests=10000, status='missing_five_hour_context', split='development'))
    frame = pd.DataFrame(rows)
    a = freeze_execution_cohort(frame)
    b = freeze_execution_cohort(frame.sample(frac=1, random_state=7))
    assert a.to_dict('records') == b.to_dict('records')
    assert '2025-06-26' not in set(a.date)
    assert len(a) <= 5  # 규모층 3개와 월 최대 2개, 중복 가능
    assert set(a.selection_reason).issubset({'monthly_volume', 'monthly_scale_sample', 'monthly_volume;monthly_scale_sample'})


def test_hardness_requires_twenty_comparable_instances_and_one_second():
    rows = [dict(case_id=str(i), residual_lcc=33, fastest_proof_seconds=i / 10,
                 operational_optimal=True, operational_gap=0, full_exact_runs=True,
                 encoding_status='exact', variant='primary') for i in range(20)]
    result = assign_hardness(pd.DataFrame(rows))
    assert result.relative_hard.sum() == 2
    assert not result.unresolved.any()
    assert not assign_hardness(pd.DataFrame(rows[:19])).relative_hard.any()


def test_encoding_failure_is_not_classical_unresolved():
    frame = pd.DataFrame([dict(case_id='large', residual_lcc=1000,
        fastest_proof_seconds=None, operational_optimal=False, operational_gap=.2,
        full_exact_runs=False, encoding_status='limit_exceeded', variant='primary')])
    result = assign_hardness(frame)
    assert not result.unresolved.iloc[0]
    assert not result.relative_hard.iloc[0]


def test_unknown_residual_is_not_a_zero_residual_certificate():
    frame = pd.DataFrame([dict(case_id='failed', residual_lcc=None,
        fastest_proof_seconds=None, operational_optimal=True, operational_gap=0,
        full_exact_runs=False, encoding_status='limit_exceeded', variant='primary')])
    assert assign_hardness(frame).residual_bin.iloc[0] == 'unavailable'


def test_peak_selection_reads_daily_dataframe_and_hashes_real_sources(tmp_path):
    from run_rd1_research import _peak_windows
    monthly = tmp_path / '공공자전거 이용정보(월별)'
    daily = tmp_path / '공공자전거 이용정보(일별)'
    monthly.mkdir()
    daily.mkdir()
    pd.DataFrame({'대여일자': ['202506'], '이용건수': [190]}).to_csv(
        monthly / 'month.csv', index=False, encoding='cp949')
    pd.DataFrame({'대여일자': ['2025-06-05', '2025-06-10'], '이용건수': [100, 90]}).to_csv(
        daily / 'daily_2506.csv', index=False, encoding='cp949')
    registry = pd.DataFrame([dict(date=day, hour=hour, district='강서구', observed_departures=50)
        for day in ['2025-06-05', '2025-06-10'] for hour in [8, 9]])
    windows, info = _peak_windows(registry, tmp_path)
    assert len(windows) == 4
    assert len(info['sources']) == 2
    assert all(len(source['sha256']) == 64 for source in info['sources'])


def test_valid_greedy_lower_bound_can_close_known_upper_bound():
    from run_rd1_research import combine_operational_baselines
    benchmark = {'lower_bound': 0, 'upper_bound': 1, 'selected_candidate_ids': []}
    evaluations = {
        'classical_best': {'selected_count': 0, 'selected_ids': [], 'checkpoint_feasible': True},
        'greedy_id': {'selected_count': 1, 'selected_ids': ['v'], 'checkpoint_feasible': True},
    }
    combine_operational_baselines(benchmark, evaluations)
    assert benchmark['lower_bound'] == benchmark['upper_bound'] == 1
    assert benchmark['optimality_proven']
    assert evaluations['classical_best']['selected_ids'] == ['v']
