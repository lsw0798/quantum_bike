# Bike Research Implementation Plan

> **For agentic workers:** Use executing-plans; user authorized implementation and local execution. Never execute notebook/QPU/authentication.

**Goal:** 실제 원자료에서 여러 규모 사례를 생성·실행하고, 검증 가능한 그래프와 실행하지 않은 QPU 노트북을 제공한다.

**Architecture:** 입력 정규화 → 후보/시간 재고 제약 → 충돌 그래프 및 선택적 정확 인코딩 → 고전 난도 측정 → 로컬 검증 → 수동 QPU 제출.

**Tech Stack:** Python, pandas, scipy MILP, networkx, nbformat, QoolQit/Pasqal.

**Spec:** 연구설계_의사결정별_문헌근거_2026-11-08.md 및 이번 사용자 지시.

## Global Constraints

- 원시 합성 이용·허위 충돌을 추가하지 않는다. 보조변수는 실제 자원과 구분한다.
- QPU 및 계정 연결은 실행하지 않는다. 자격증명을 출력·복제하지 않는다.
- 난도는 제한시간·알고리즘에 상대적으로 측정한다. 어려운 사례 미발견은 그대로 보고한다.
- 실제 수용 상한 미확인은 표기하고 가상의 capacity를 넣지 않는다.

## Review Focus

- 집합적 용량·공급 의존성: graph tests.
- 재고 누락·동시 사건·배경 흐름: data tests와 품질 로그.
- 변환 목적값·보조정점: encoding tests.
- 제출 차단·간선 보존: qpu tests.
- 시간초과·상하계·난도 오인: benchmark tests.

## Tasks

- [x] data.py/cases.py: 실제 사례 생성, 출처/파라미터 보존, 로컬 실행.
- [x] graph.py: 충돌 그래프, 전체 제약 검사, MILP·greedy 비교.
- [x] encoding.py: 한도 내 정확 비가중 인코딩, 초과 시 명시적 실패.
- [x] qpu.py/notebook: 공식 API, 실제 QPU backend, default-off, mock 검증.
- [x] 통합 실행, 실제 결과 보고서, 독립 코드 검토.

## Execution Notes

Git 저장소가 없는 작업 폴더에서 직접 작업한다. 사용자 지시로 QPU 노트북 전체 실행은 제외한다.

- 37개 사례 생성, 최대 17,934후보. 고전 시간초과 사례 미발견: 고난도 확보 요구는 미충족이며 보고서에 명시.
- 사건 재구성 모형 9개 불가능. 별도 관측 시점 모형을 조건부로 병행; 사이 시각 실행 가능성 보장 금지.
- 정확 인코딩의 지연은 원모형 해를 변환하는 기준법과 비교; 원사례 난도로 오인 금지.
- 독립 검토에서 중복 여행과 불완전 요청의 알려진 사건 누락을 발견하고 수정·단위검증.
- 최종 검토에서 stale exact_graph 재사용을 발견. 회귀 실패를 재현한 뒤 파생파일 교체·입력 해시 연결로 수정.
- 보고서 재실행의 고정 수치 문제 수정: 현재 결과 동적 계산, 최초 검증 수치는 날짜 명시.
- 기존 QPU 작업 복구·입력 해시 검사 추가. 최종 전체 테스트 43개 통과(14.27초). 노트북 형식/문법 통과, 실행하지 않음.
