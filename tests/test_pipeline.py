"""로컬 파이프라인의 재실행 동작을 검증한다."""
import json
from data_process import process_case


def test_rerun_removes_old_exact_graph_and_binds_input_hash(tmp_path):
    """재실행 시 이전 정확 그래프를 지우고, 그래프를 입력 해시에 묶는다."""
    case = {
        'case_id': 'unit',
        'metadata': {'date': '2025-01-01', 'hour': 8, 'district': 'test', 'request_limit': 1},
        'requests': [{'request_id': 'r'}],
        'candidates': [{'id': 'v', 'request_id': 'r'}],
        'constraints': [],
        'inventory_audit': {'baseline_feasible': True},
    }

    # 이전 실행에서 남은 파일을 흉내 낸다.
    (tmp_path / 'exact_graph.json').write_text('{"obsolete":true}')
    (tmp_path / 'exact_graph_benchmark.json').write_text('{}')

    # 후보 한도 0으로 실행하면 정확 인코딩을 시도하지 않는다.
    result = process_case(case, tmp_path, 1, 0)

    # 이전 파일이 지워졌는지 확인한다.
    assert not (tmp_path / 'exact_graph.json').exists()
    assert not (tmp_path / 'exact_graph_benchmark.json').exists()

    # 그래프의 해시가 이번 입력의 해시와 같은지 확인한다.
    graph = json.loads((tmp_path / 'graph.json').read_text(encoding='utf-8'))
    assert graph['metadata']['case_sha256'] == result['input_sha256']
