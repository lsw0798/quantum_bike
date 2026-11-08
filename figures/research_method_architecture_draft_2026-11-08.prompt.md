# 실험방법 아키텍처 이미지 생성 기록

- 생성 도구: 내장 image_gen
- 그림: research_method_architecture_draft_2026-11-08.png
- 상태: 연구 흐름 설계안. 실험조건 확정본이 아님.
- 미확정: 운영 제약의 모델링 범위, 난도 선정 기준.
- 현재 실험 상태: 고전 예비실험 수행, 고난도 사례 미확보, QPU 미실행.
- 그림의 '노드: 이동 대안'은 후보 충돌 그래프를 뜻함. 정확 인코딩을 적용하면 추가 보조 노드가 생길 수 있음.
- 난도별 자동 선정과 최종 운영 재현 평가는 목표 실험 흐름이며, 현재 구현과 검증 완료 여부를 별도로 확인해야 함.

## 최종 생성 프롬프트

Create a simple, scientifically careful Korean academic methodology architecture diagram, as an AI-generated raster image for a bike-sharing research paper discussion. Use-case: infographic-diagram. Landscape canvas, approximately 1800 x 1100. White background, flat crisp boxes and arrows, no shadows or gradients, no photos, no decorative futuristic quantum imagery. Navy typography, subtle blue and teal fills. Orange dashed small callouts mark two open design decisions. Very legible Korean sans-serif text. Generous white space. This is a DESIGN PROPOSAL, not evidence that experiments succeeded.

Title verbatim: "따릉이 재배치 연구 — 실험방법 설계안"
Subtitle verbatim: "실측 운영 기록 → 충돌 없는 이동 대안 → 고전·QPU 비교"

Compose an easy-to-follow diagram with the following layout and exact Korean labels. Upper row has four evenly spaced boxes connected LEFT TO RIGHT. Each box has a small stage number.
Box 1 title "① 실측 데이터"
body three short lines:
"대여·반납 이력"
"대여소 정보"
"시간별 대여가능 수량"

Box 2 title "② 사례·후보 생성"
body:
"자료 정합성 검사"
"시간·지역별 사례 구성"
"대체 이동 후보 생성"

Box 3 title "③ MIS 그래프 구성"
body:
"노드: 이동 대안"
"엣지: 동시 선택 불가"
"운영 제약의 표현 범위 검증"
Directly under this box attach an ORANGE DASHED small annotation:
"확정 필요: 운영 제약 범위"

Box 4 title "④ 난도별 사례 선정"
body:
"고전 예비실험으로 난도 측정"
"쉬움·중간·어려움 사례 선정"
"비교할 논리 그래프 고정"
Directly under this box attach an ORANGE DASHED small annotation:
"확정 필요: 난도 선정 기준"

From box 4 draw a clear routed downward arrow to a centered horizontal label above the second row:
"동일한 논리 MIS 문제"
From that label split with two arrows to two equal-sized SECOND ROW branch boxes side-by-side, with no arrow between them:
Left branch heading "⑤ 고전 MIS"
body:
"정확 알고리즘"
"휴리스틱 알고리즘"
Right branch heading "⑤ QPU MIS"
body:
"장치 배치·문제 보존 확인"
"QoolQit · Pasqal QPU"
Use identical visual importance for both branches. No assertion of quantum advantage.

Both branch boxes connect downward and merge into a single wide THIRD ROW box:
heading "⑥ 공통 검증·평가"
body arranged as two clean lines:
"독립집합 검증 · 운영 제약 검증 · 운영 재현 평가"
"해 품질 · 수급 개선 가능성 · 계산 비용 · 처리 규모"

Small footer at the bottom, exact:
"현재: 고전 예비실험만 수행 · 고난도 사례 미확보 · QPU 미실행"
Below or beside it in smaller but readable dark gray:
"선택 개수와 수급 개선 효과는 별도로 평가"

Ensure the arrow sequence is unambiguous: 1→2→3→4, then 4→shared frozen graph label→both solver branches→common evaluation. Do not draw shortcut arrows or loops. All labels above must be rendered correctly in Korean; no random text, equations, quantitative claims, fabricated data or logos. The two orange callouts explicitly distinguish unresolved design choices. This should remain a simple research flow diagram rather than a dense poster.

