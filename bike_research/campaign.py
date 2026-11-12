"""실측 실행군을 고정하고, 실제 풀이 결과로 난도를 분류한다.

연간 창 등록부와 실제 실행한 창은 다른 모집단이다. 실행하지 않은 창을
쉬움/어려움으로 채우지 않는다. 이 모듈에는 원격 실행 기능이 없다.
"""
from __future__ import annotations

import hashlib
import json
import math

import numpy as np
import pandas as pd


def canonical_hash(value: dict) -> str:
    """설정·대상 목록의 순서와 값을 보존하는 재현용 해시."""
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True,
                     separators=(',', ':'), allow_nan=False).encode('utf-8')
    return hashlib.sha256(raw).hexdigest()


def size_bin(value: int) -> str:
    """2의 거듭제곱 구간. 실제 축약 0과 생략 여부는 별도 필드로 보존한다."""
    if value <= 0:
        return 'certified_zero'
    lower = 2 ** int(math.log2(value))
    return f'{lower}-{lower * 2 - 1}'


def freeze_execution_cohort(registry: pd.DataFrame, seed: int = 20261109,
                            peak_windows: list[dict] | None = None) -> pd.DataFrame:
    """풀이 결과를 보기 전에 월·원래 요청 수로 실행 대상을 고정한다.

    각 월의 원래 요청 수 규모층마다 최대 3개를 표본 추출하고 월 최대
    이용 창 2개와 명세의 피크 창을 더한다. 이것은 계산 예산을 위한
    탐색 실행군이다. RD1의 사후 잔여 그래프 규모별 일반군과 혼동하지 않는다.
    각 선택 창에서는 요청을 자르지 않고 모든 적격 후보를 생성한다.
    """
    columns = ['date', 'hour', 'district']
    source = registry.loc[registry.status.eq('eligible')].copy()
    source = source.sort_values(columns).reset_index(drop=True)
    if source.empty:
        return source.assign(selection_reason=pd.Series(dtype=str))
    source['month'] = source.date.str[:7]
    source['request_scale_bin'] = source.eligible_requests.map(size_bin)
    rng = np.random.default_rng(seed)
    reasons: dict[int, set[str]] = {}

    def include(indices, reason):
        for index in indices:
            reasons.setdefault(int(index), set()).add(reason)

    for _, month in source.groupby('month', sort=True):
        top = month.sort_values(['eligible_requests', *columns],
                                ascending=[False, True, True, True]).head(2)
        include(top.index, 'monthly_volume')
        for _, group in month.groupby('request_scale_bin', sort=True):
            chosen = rng.choice(group.index.to_numpy(), size=min(3, len(group)), replace=False)
            include(chosen, 'monthly_scale_sample')
    for window in peak_windows or []:
        mask = np.ones(len(source), dtype=bool)
        for key in columns:
            mask &= source[key].eq(window[key]).to_numpy()
        include(source.index[mask], 'predefined_peak')
    result = source.loc[sorted(reasons)].copy()
    result['selection_reason'] = [';'.join(sorted(reasons[i])) for i in result.index]
    return result.sort_values(columns).reset_index(drop=True)


def assign_hardness(summary: pd.DataFrame) -> pd.DataFrame:
    """인코딩 실패와 측정 누락은 고전 알고리즘의 미해결로 세지 않는다."""
    result = summary.copy()
    if result.empty:
        return result.assign(relative_hard=False, unresolved=False)
    result['residual_bin'] = result.residual_lcc.map(
        lambda n: 'unavailable' if pd.isna(n) else size_bin(int(n)))
    result['relative_hard'] = False
    result['hardness_bin_n'] = 0
    result['unresolved'] = (
        result.full_exact_runs.fillna(False).astype(bool)
        & ~result.operational_optimal.fillna(False).astype(bool)
        & result.operational_gap.fillna(0).gt(0)
        & result.encoding_status.isin(['exact', 'encoded', 'exact_candidate_graph'])
    )
    # 변형마다 입력 모형이 다르므로 같은 규모라도 섞어서 순위를 만들지 않는다.
    for _, indices in result.groupby(['variant', 'residual_bin']).groups.items():
        group = result.loc[indices]
        comparable = group.loc[group.full_exact_runs.fillna(False).astype(bool)]
        result.loc[indices, 'hardness_bin_n'] = len(comparable)
        if len(comparable) < 20 or group.residual_bin.iloc[0] == 'certified_zero':
            continue
        measured = comparable.fastest_proof_seconds.dropna()
        if measured.empty:
            continue
        cutoff = float(measured.quantile(.9))
        mask = (group.fastest_proof_seconds.ge(max(1.0, cutoff))
                & group.operational_optimal.fillna(False).astype(bool)
                & group.full_exact_runs.fillna(False).astype(bool))
        result.loc[group.index[mask], 'relative_hard'] = True
    return result


def select_general_group(summary: pd.DataFrame, seed: int = 20261109) -> list[str]:
    """측정된 주 조건의 잔여 규모층마다 일반군 최대 3개를 고정 추출한다."""
    frame = summary.loc[summary.variant.eq('primary')
                        & summary.encoding_status.eq('exact')].sort_values('case_id')
    rng = np.random.default_rng(seed)
    selected = []
    for _, group in frame.groupby('residual_bin', sort=True):
        selected.extend(rng.choice(group.case_id.to_numpy(), min(3, len(group)), replace=False))
    return sorted(selected)
