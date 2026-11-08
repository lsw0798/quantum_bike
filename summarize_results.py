"""저장된 실제 실행 결과를 짧은 보고서로 합친다. 원격 호출은 없다."""
from pathlib import Path
import json
import pandas as pd

# 프로젝트 루트 경로
ROOT = Path(__file__).resolve().parent


def main():
    """실행군별 요약을 합쳐 CSV와 실행보고서.md를 저장한다."""
    frames, rows, budgets = [], [], []

    # 실행군별로 summary.csv와 manifest.json을 읽는다.
    for label, folder in [("기본", ROOT / "outputs"),
                          ("확대", ROOT / "outputs/stress"),
                          ("QPU 로컬 점검", ROOT / "outputs/qpu_demo")]:
        # 아직 실행하지 않은 실행군은 건너뛴다.
        path = folder / "summary.csv"
        if not path.exists():
            continue

        # 요약과 제한시간을 읽고 실행군 이름을 붙인다.
        frame = pd.read_csv(path)
        manifest = json.loads((folder / "manifest.json").read_text(encoding="utf-8"))
        budgets.append(f"{label} {manifest['parameters']['time_limit']}초")
        frame["run_group"] = label
        frames.append(frame)

        # 보고서 표에 들어갈 행을 만든다.
        rows.append(f"| {label} | {len(frame)} | {int(frame.candidates.max()):,} | "
                    f"{sum(frame.operational_status.eq('optimal'))} | "
                    f"{sum(frame.operational_status.eq('infeasible'))} | "
                    f"{sum(frame.operational_status.eq('limit_reached'))} |")

    # 전체 결과와 난도 순위를 CSV로 저장한다.
    combined = pd.concat(frames, ignore_index=True)
    combined.to_csv(ROOT / "outputs/all_results.csv", index=False, encoding="utf-8-sig")
    ranking = combined.sort_values(["graph_gap", "graph_seconds"], ascending=False)
    ranking.to_csv(ROOT / "outputs/hardness_ranking.csv", index=False, encoding="utf-8-sig")

    # 보고서 본문에 쓸 통계를 계산한다.
    hard_count = int(combined.graph_status.eq("limit_reached").sum())
    maximum_candidates = int(combined.candidates.max())
    selected_dates = ", ".join(sorted(combined.date.astype(str).unique()))
    selected_districts = ", ".join(sorted(combined.district.astype(str).unique()))

    # 제한시간 초과 사례가 있는지에 따라 결론 문구를 정한다.
    description = (
        f"그래프 MILP 제한시간 초과 {hard_count}건. 특정 구현·예산에 상대적인 결과다."
        if hard_count else
        "**고전적으로 어려운 사례는 이번 실행에서 확보하지 못했다.** "
        f"이번 결과의 최대 후보 수는 {maximum_candidates:,}개다. "
        "그래프가 크다는 이유로 고난도라고 표시하지 않았다.")

    # 보고서 본문
    report = f"""# 실측 사례·MIS 파이프라인 실행 보고서

## 수행 내용

실제 origin_data로 사례 생성 → 노드·엣지 및 운영 제약 저장 → 고전 비교를 실행했다.
QoolQit/Pasqal 코드는 기존 quantum_bike_1105.ipynb에 이어 작성했다.
**계정 인증·원격 장치 조회·에뮬레이터·QPU 제출은 모두 실행하지 않았다.**

## 실제 결과

현재 저장 결과의 날짜는 **{selected_dates}**, 지역은 **{selected_districts}**이다.
월·일 이용량과 시간·지역별 혼잡을 사용한 표본이며 일반 서울 이용의 대표 표본은 아니다.
각 실행의 파라미터는 해당 manifest.json에 보존된다.

| 실행군 | 사례 수 | 최대 후보 수 | 운영모형 최적해 증명 | 운영모형 불가능 | 운영모형 시간초과 |
|---|---:|---:|---:|---:|---:|
{chr(10).join(rows)}

{description}

풀이당 제한시간: {', '.join(budgets)}.
규모·시간·상하계·격차는 outputs/all_results.csv와 hardness_ranking.csv에 있다.
실행 불가능은 관측/모형 불일치이며 계산 고난도로 세지 않았다.

## 모형에서 구별한 것

- **사건 재구성:** 시간별 수량을 정시 초기값으로 가정하고 알려진 대여·반납을 누적한다.
  큰 사례 일부의 재고 경로가 맞지 않았다. 관측 시점·누락 사건·미관측 운영 이동 등 원인은 미확정이다.
- **관측 시점 모형:** 실제 시간별 관측 시점에만 재고 제약을 건 별도 조건부 모형.
  관측 사이의 실행 가능성을 보장하지 않는다. 사건 제약도 감사용으로 보존한다.
- **충돌 그래프:** 같은 요청과 증명 가능한 자원 충돌을 연결한다.
  집합적 수량·공급 의존성이 남으면 pairwise_relaxation으로 표시하고 원제약을 다시 검사한다.
- **정확 인코딩:** 작은 입력의 전체 선언 제약을 보조 노드가 있는 비가중 MIS로 변환한다.
  한도 초과 시 중단하며 제약을 삭제하지 않는다. 보조 노드는 가상 이용자가 아니다.

최초 작성일(2026-11-08)의 검증 예에서는 16후보 사례가 1,359노드·12,325엣지로 변환됐다.
그 원모형 최적 추천 2개를 독립집합 699개(offset 697 포함)로 옮겨 검증했다.
다른 조건으로 재실행했을 때의 인코딩 크기·복원 결과는 각 encoding_status.json을 따른다.
확장 그래프의 일반 MILP 지연은 **인코딩 비용**이며 원사례의 본질적 난도 증거가 아니다.

## 파일을 읽는 순서

| 파일 | 역할 |
|---|---|
| data_process.py | 파라미터와 전체 로컬 실행 순서 |
| bike_research/data.py | 원본 읽기·정규화·중복 표시·캐시·출처 |
| bike_research/cases.py | 실제 요청의 대체 이동과 시간별 재고 제약 |
| bike_research/graph.py | 노드·엣지, MILP·탐욕법, 운영 가능성 검사 |
| bike_research/encoding.py | 크기 제한을 둔 정확 인코딩·복호화·원해 변환 |
| bike_research/qpu.py | 로컬 배치·실제 Hamiltonian 검사·명시적 QPU 제출 |
| quantum_bike_1105.ipynb | 기존 계정 설정을 재사용하는 검토용 실행 순서 |

사례별 outputs/cases/<case_id>/에 case.json, graph.json, nodes.csv, edges.csv,
고전 풀이 결과, encoding_status.json을 저장했다. 작은 사례에는 exact_graph.json도 있다.
확대군과 QPU 점검군은 각각 outputs/stress, outputs/qpu_demo에 있다.

## 재실행과 QPU 사용

로컬 데이터 처리: python data_process.py

확대 탐색:
python data_process.py --months 202506 --days 2 --hours 2 --request-limits 1200 --alternatives 3 --radius-m 750 --inventory-models checkpoints --time-limit 15 --exact-candidate-limit 0 --output-dir outputs/stress

보고서 갱신: python summarize_results.py

로컬 의존성은 requirements-local.txt, QPU 환경은 requirements-qpu.txt에 있다.
작업 폴더의 .venv-qpu에 QoolQit 1.5.0을 설치했다.
노트북 커널은 .venv-qpu/Scripts/python.exe 환경을 선택한다.

노트북은 인증·제출 플래그가 기본 False다. 실제 제출 시에는 사용자가 해당 플래그와
실제 장치명을 직접 설정해야 한다. 저장된 제출 기록이 있으면 자동 재제출하지 않는다.
일반 그래프가 장치에 맞지 않으면 중단하며 엣지를 바꾸거나 에뮬레이터로 대체하지 않는다.

최초 작성일의 **3노드 점검 그래프**는 로컬 프로그램 생성과 오프라인 AnalogDevice
컴파일·최종 상호작용 검사를 통과했다. 장치 최대 시간의 80%라는 명시적 시험 설정이며
최적 펄스라는 뜻은 아니다. 실제 계정의 QPU 접근·장치 적합성·성능은 미확인이다.
13노드의 기본 duration=40 컴파일 실패 기록도 보존했다.

## 검증과 남은 한계

최초 작성 시 전체 테스트 통과 결과는 outputs/validation.json에 보존했다.
집합적 용량, 공급 의존성, 중복 여행, 반납 정보가 없지만 알려진 출발 사건,
정확 변환·복원, 제출 차단을 검사했다. 노트북은 형식·문법만 검증했고 실행하지 않았다.

실제 반납 상한·개인 ID·추천 수락·실패 수요는 현재 자료로 확정하지 않았다.
보행 반경·속도·추가시간은 공개된 시나리오 파라미터다.
목적값은 허용 추천 수이며 수급 개선량과 동일하지 않다.
새 사례를 많이 찾거나 크기만 늘려도 고난도 확보가 보장되지 않는다.

방법의 근거·미확인 항목은 기존 연구설계_의사결정별_문헌근거_2026-11-08.md를 따른다.
QoolQit API는 https://docs.pasqal.com/qoolqit/fundamentals/execution/execution/ 를 확인했다.
"""

    # 보고서를 저장하고 처리 건수를 출력한다.
    (ROOT / "실행보고서.md").write_text(report, encoding="utf-8")
    print(f"report written; {len(combined)} cases; graph_timeouts={hard_count}")


# 스크립트로 직접 실행될 때만 진입한다.
if __name__ == "__main__":
    main()
