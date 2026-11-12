# 따릉이 트럭 재배치 작업 선택 — Pasqal QPU·EMU_FRESNEL / 비가중 MIS

## 바로 실행

아직 지정 프로젝트에 설치하지 않았다면 이 코드 폴더에서 `python3 install_into_project.py`를 한 번 실행하세요. 기존 파일은 건드리지 않고 지정한 프로젝트 아래 `truck_mis_qpu` 폴더를 생성합니다. 같은 이름의 폴더가 이미 있으면 덮어쓰지 않습니다.

macOS에서 `run_qpu.command`를 실행하면 전용 가상환경 준비 → 원본 데이터 읽기 → 작업·충돌그래프 생성 → 원자 배치 검증 → 설정한 Pasqal 원격 장치 제출 → 결과 조회를 수행합니다. 현재 기본 실행 대상은 **EMU_FRESNEL 에뮬레이터**입니다. 계정의 대상 장치 이용 권한과 계약상 크레딧이 적용됩니다. Python 3.11 이상과 인터넷 연결이 필요합니다.

```bash
cd /Users/leeseungwoo/Desktop/backup/lsw2026/quantum_bike/truck_mis_qpu
./run_qpu.command
```

계정은 이전 노트북과 동일한 상위 프로젝트 `.env`의 `PASQAL_USERNAME`, `PASQAL_PASSWORD`, `PASQAL_PROJECT_ID`를 읽습니다. 환경변수가 있으면 해당 값을 우선합니다. 노트북을 실행할 필요가 없고 비밀번호를 코드에 복사하지 않습니다. 실제 QPU 이용 권한과 대기열·장비 상태는 계정에 따라 달라집니다.

원격 제출 없이 먼저 확인하려면:

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python run.py prepare
```

`prepare`에는 인증·원격 호출이 없습니다. `run`은 장치 사양을 읽은 뒤에 추가 검증합니다. EMU_FRESNEL 모드는 FRESNEL 물리 사양으로 컴파일하고 실행 요청은 EMU_FRESNEL로 전송합니다. 같은 입력과 설정으로 재실행하면 제출 영수증을 확인하여 기존 작업을 조회합니다. 새 반복 실험은 설정의 `output_dir`를 별도 폴더로 지정하세요. 기존 영수증을 삭제해서 재제출하지 마세요.

원격 실행은 비동기 작업입니다. 30초 이내에 끝나지 않으면 입력·작업 ID를 보존하고 종료합니다. 출력된 경로로 나중에 조회할 수 있습니다.

```bash
.venv/bin/python run.py fetch --run-dir outputs/실행해시
```

## 연구에서 정확히 푸는 문제

- 노드: **시작시각·수거소·배치소·운반량이 정해진 하나의 왕복 작업**입니다.
- 작업 경로: 출발점 → 과잉소에서 수거 → 부족소에 배치 → 출발점으로 복귀합니다. 각 작업은 빈 트럭으로 시작·종료합니다.
- 엣지: 두 작업의 시간 구간이 겹치거나 같은 대여소를 사용하면 연결합니다.
- 목적: 충돌하지 않는 **작업 개수 최대화(MIS)**입니다. 모든 작업은 같은 수량을 옮기므로 운반 대수 최대화와 같습니다.
- 조건: 트럭 1대, 정적 재고, 계획기간 중 대여소별 최대 한 작업, 고정 수량입니다. 이 축소 모형에서 독립집합이 실행 가능한 작업 조합에 대응합니다.
- 거리·재고는 후보 가능 여부와 소요시간에 반영합니다. 거리 최소화·서로 다른 가중치의 MWIS를 동시에 푼다고 해석하지 않습니다.
- 트럭 초기 보유 재고의 직접 배치, 자유로운 연속 방문 경로, 대여소 반복 이용은 현재 구현의 범위 밖입니다. 운영 정책으로 확정한 가정이 아니라 QPU 실험용 제한입니다.

QPU 모드에서는 고전 컴퓨터가 입력 생성, 원자 좌표 배치, 펄스 컴파일, 표본 해석을 수행하고 QPU가 양자 동역학을 실행합니다. EMU_FRESNEL 모드에서는 고전 컴퓨터가 같은 펄스의 양자 동역학을 시뮬레이션하므로 결과는 에뮬레이션 표본입니다. 두 모드 모두 변분 최적화·고전 조합 풀이기와의 반복 루프·불가능 표본 수리는 없습니다. 표본의 유효성을 검사하고 가장 많은 작업이 선택된 표본을 고르는 단계는 결과 판독입니다.

## 기본 설정과 수정 위치

`config.toml` 한 곳에서 연구 설정을 변경합니다.

| 항목 | 기본값 | 의미 |
|---|---:|---|
| `data.snapshot_timestamp` | 2025-07-15 08:00, 서울시각 | 과거 실측 재고로 구성한 실험 |
| `data.mode` | historical_median | 직전 14일 같은 시간대 재고 중앙값을 내림 |
| `model.origin_station_id` | 102 | 망원역 1번출구 앞 |
| `model.radius_km` | 2 | 출발점 주변 탐색 반경 |
| `model.bikes_per_task` | 2 | 모든 작업의 운반량 |
| `model.truck_capacity` | 20 | 트럭 용량 가정 |
| `model.horizon_min` | 120 | 계획기간 |
| `model.start_step_min` | 30 | 후보 시작시각 격자 |
| `model.max_tasks` | 8 | QPU 파일럿 입력 크기 |
| `model.candidate_seed` | 42 | 후보 표본 추출 재현성 |
| `qpu.device_name` | EMU_FRESNEL | 현재 지정한 원격 에뮬레이터 |
| `qpu.shots` | 100 | 최초 실행의 측정 횟수 |
| `qpu.amplitude_factor` | 6.5 | 최종 detuning 대비 펄스 최대 진폭 배수 |

2026-11-11 장치 확인 시 FRESNEL의 최소 평균 진폭은 0.565487 rad/µs였습니다. 기본 8개 노드 사례는 `amplitude_factor = 6.5`로 이 제한을 만족하도록 설정했습니다. FRESNEL_CAN1은 최소 평균 진폭 0.314159 rad/µs에 대해 계수 4.0으로 검증한 바 있습니다. 이 계수는 중간 펄스 세기를 정하며, 최종 진폭은 수치 오차 범위에서 0입니다. 최종 MIS 에너지 조건과 장치 제약은 컴파일 시 다시 검사합니다. 계수를 생략하면 기존 동작인 1.0을 사용합니다.

첫 실행은 8개 노드·100회 측정의 파일럿입니다. 큰 사례로 변경하면 직접 원자 배치가 실패할 수 있습니다. **실패 사례의 노드·엣지를 삭제하거나 다른 문제로 자동 대체하지 않습니다.** 전체 후보 수와 추출 비율을 저장하므로 작은 부분문제의 결과를 전체 운영 문제의 최적해로 보고하면 안 됩니다.

`historical_median` 목표는 관측 재고 기반의 실험 기준입니다. 잠재 수요나 Schuijbroek 등의 서비스 수준 최적 재고를 추정한 값이 아닙니다. `mode = "fixed"`와 `fixed_target`으로 고정 목표 실험도 할 수 있습니다.

원자료의 `거치대수량`은 시간별 대여 가능 자전거 수로 읽습니다. 설치 거치대 수를 물리적 수용량으로 처리하지 않습니다. 재고가 설치 거치대 수보다 큰 관측을 임의로 잘라내지 않습니다. 시간별 자료이므로 실제 실행 시각의 현장 재고와는 다릅니다.

사용자가 정제한 재고·목표를 쓰려면 `[data]`에 `stations_csv = "/절대경로/stations.csv"`를 지정합니다. UTF-8 CSV 필수 열은 `station_id,name,latitude,longitude,bikes,target`입니다. 선택 열 `timestamp`가 있으면 설정 시각과 일치해야 합니다.

## 원자 배치 검증

논리 충돌그래프가 중성원자 장치에서 항상 그대로 구현되는 것은 아닙니다. 코드는 직접 배치를 시도하고 모든 원자 쌍의 상호작용을 검사합니다. 최종 detuning이 **비엣지 상호작용 전체 합보다 크고 최소 엣지 상호작용보다 작은지** 확인합니다. 이는 이상적인 최종 Hamiltonian의 바닥상태가 MIS에 해당하도록 하는 보수적 충분조건입니다. 통과하지 못했다고 원리적으로 불가능한 그래프라는 뜻은 아닙니다.

실제 장치의 C6, 좌표, 최종 명령 파형으로 같은 조건을 다시 확인하고 제출합니다. 유한 시간·잡음이 있는 실제 QPU에서 최적해가 나오는 것을 보장하지 않습니다. 가중치를 지원하지 않는 문제로 몰래 바꾸거나 엣지 누락을 허용하지 않습니다.

## 결과 파일

실행별 `outputs/<입력해시>/`에 다음 자료가 남습니다.

- `case.json`: 대여소·작업·충돌 엣지, 원본 파일 SHA-256, 제외 기록과 후보 축소율
- `manifest.json`: 설정, 사례 해시와 고정된 비트·노드 대응
- `coordinates.json`, `preparation.json`: 원자 좌표와 로컬 검증
- `sequence.json`, `device.json`, `compiled_certificate.json`: 제출 당시 실제 장치의 펄스·사양·검증
- `qpu_submission.json`: 실제 장치·측정수·제출 상태·작업 ID 및 컴파일 검증
- `emulator_submission.json`: EMU_FRESNEL의 제출 상태·작업 ID·컴파일 장치 및 `backend = "EMULATOR"` 기록
- `submission_guard.json`: QPU·에뮬레이터 프로세스가 같은 폴더에 중복 제출하지 않도록 하는 공통 예약 기록
- `raw_counts.json`: 원격 장치가 반환한 원시 비트열별 빈도
- `evaluation.json`: 실행 장치·백엔드 종류·실행 가능률과 모든 원시 표본의 운영 제약 검사
- `best_plan.json`, `best_plan.csv`: 관측된 최선의 실행 가능한 작업 일정

실행 가능 표본이 없으면 실패로 기록합니다. 보고되는 계획은 **관측된 최선**이며 고전 최적값 대비 최적성은 별도 평가해야 합니다. 제출 응답이 끊겨 성공 여부가 불명확한 경우에는 자동 재제출을 막습니다. 그때는 Pasqal 콘솔에서 같은 시간대 작업을 확인해야 합니다.

## 코드 구성과 테스트

- `truck_mis/data.py`: 파일 읽기, 과거 자료만 사용한 목표 재고
- `truck_mis/domain.py`: 작업·충돌그래프 생성, 독립적인 운영 제약 검사
- `truck_mis/embedding.py`: 논리 그래프의 직접 원자 배치와 에너지 조건
- `truck_mis/backend.py`: QoolQit/Pulser 컴파일, 실제 Pasqal 제출·조회
- `truck_mis/cli.py`: 전체 실행 흐름과 결과 저장
- `truck_mis/settings.py`, `storage.py`: 설정 검증과 원자적 파일 저장
- `tests/`: 모든 작은 조합의 그래프-운영 동치 검증, 원본 입력, 인증·제출을 모사한 테스트

```bash
.venv/bin/python -m pytest -q
```

테스트는 실제 계정에 접속하거나 QPU 작업을 제출하지 않습니다. 제작 시 31개 테스트와 실제 기본 사례의 오프라인 컴파일(AnalogDevice, 8개 원자, 4,800 ns)을 검증했습니다. 이는 실제 계정의 Fresnel 실행 결과가 아니며 실제 사양은 `run` 시 조회합니다. `validation_report.json`에 검증 범위를 기록했습니다. 기존 `bike_research` 모듈·노트북을 변경하거나 import하지 않아 이전 연구와 분리됩니다.

## API 근거

- [QoolQit 실행](https://docs.pasqal.com/qoolqit/fundamentals/execution/execution/)
- [QoolQit 장치](https://docs.pasqal.com/qoolqit/reference/devices/)
- [Pulser QPU 실행 가이드](https://pulser.readthedocs.io/en/stable/quick_run_guide.html)
- [Pasqal Cloud SDK](https://pasqal-io.github.io/pasqal-cloud/getting-started/)
- 코드 호환 기준: QoolQit 1.5.0, Pasqal Cloud 0.24.0, Pulser 1.10.0.
