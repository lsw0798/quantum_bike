"""완료된 RD-1.0 로컬 실험 산출물로 한국어 연구결과 DOCX를 작성한다.

분석 모듈을 import하지 않으며 저장된 JSON/CSV만 읽는다. 미완료 실행은
문서 생성 전에 거부한다. 문서 작성에는 Codex 번들 Python을 사용한다.
"""
from __future__ import annotations

import csv
import hashlib
import json
import math
import re
import statistics
from collections import Counter
from pathlib import Path

from docx import Document
from docx.enum.table import WD_TABLE_ALIGNMENT, WD_CELL_VERTICAL_ALIGNMENT
from docx.enum.text import WD_ALIGN_PARAGRAPH, WD_TAB_ALIGNMENT, WD_TAB_LEADER
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.opc.constants import RELATIONSHIP_TYPE
from docx.shared import Cm, Pt, RGBColor

ROOT = Path(__file__).resolve().parent
OUT = ROOT / '연구결과_보고서.docx'
QA = ROOT / 'outputs' / 'rd1_report_qa'
FONT = '맑은 고딕'


def read_json(path):
    return json.loads(Path(path).read_text(encoding='utf-8-sig'))


def fmt(value, digits=0):
    if value is None or isinstance(value, float) and not math.isfinite(value):
        return 'NA'
    if isinstance(value, bool):
        return '예' if value else '아니오'
    if isinstance(value, (int, float)):
        return f'{value:,.{digits}f}'
    return str(value)


def values(rows, field):
    return [float(r[field]) for r in rows if isinstance(r.get(field), (int, float))
            and not isinstance(r.get(field), bool) and math.isfinite(float(r[field]))]


def med(rows, field, digits=1):
    v = values(rows, field)
    return fmt(statistics.median(v), digits) if v else 'NA'


def count(rows, field, value=True):
    return sum(r.get(field) == value for r in rows)


def denom(n, d):
    return f'{n:,}/{d:,} ({100*n/d:.1f}%)' if d else '0/0 (NA)'


def add_reference_links(paragraph, text):
    """원문 URL을 클릭 가능한 검정 링크로 보존한다."""
    paragraph.clear()
    previous = 0
    for match in re.finditer(r'https?://[^\s;]+', text):
        paragraph.add_run(text[previous:match.start()])
        hyperlink = OxmlElement('w:hyperlink')
        rel = paragraph.part.relate_to(match.group(), RELATIONSHIP_TYPE.HYPERLINK, is_external=True)
        hyperlink.set(qn('r:id'),rel)
        run = OxmlElement('w:r')
        props = OxmlElement('w:rPr')
        size = OxmlElement('w:sz')
        size.set(qn('w:val'),'17')
        props.append(size)
        color = OxmlElement('w:color')
        color.set(qn('w:val'),'000000')
        props.append(color)
        underline = OxmlElement('w:u')
        underline.set(qn('w:val'),'single')
        props.append(underline)
        run.append(props)
        token = OxmlElement('w:t')
        token.text = match.group()
        run.append(token)
        hyperlink.append(run)
        paragraph._p.append(hyperlink)
        previous = match.end()
    paragraph.add_run(text[previous:])


def extent(rows, field, digits=0):
    v = values(rows, field)
    return f'{fmt(min(v), digits)}–{fmt(max(v), digits)}' if v else 'NA'


def assert_complete(folder):
    completion = read_json(folder / 'completion.json')
    records = read_json(folder / 'summary.json')
    if completion['completed_cases'] != completion['expected_cases'] or len(records) != completion['completed_cases']:
        raise ValueError(f'미완료 실행: {folder}')
    if completion.get('failed_cases', 0):
        raise ValueError(f'실행 오류가 남아 있음: {folder}')
    if len({r['case_id'] for r in records}) != len(records):
        raise ValueError(f'중복 사례 ID: {folder}')
    if any(r.get('qpu_executed') is not False for r in records):
        raise ValueError('로컬 고전 실험 범위 확인 실패')
    return completion, records


class Report:
    def __init__(self):
        self.doc = Document()
        sec = self.doc.sections[0]
        sec.page_width, sec.page_height = Cm(21), Cm(29.7)
        sec.top_margin, sec.bottom_margin = Cm(2.1), Cm(1.9)
        sec.left_margin, sec.right_margin = Cm(2.2), Cm(2.2)
        sec.footer_distance = Cm(0.8)
        grid = sec._sectPr.find(qn('w:docGrid'))
        if grid is not None:
            sec._sectPr.remove(grid)
        # 기본 Word 템플릿의 Title 아래 파란 구분선을 제거한다.
        for style in self.doc.styles:
            for border in style.element.xpath('.//w:pBdr'):
                border.getparent().remove(border)
        for name in ['Normal', 'Title', 'Subtitle', 'Heading 1', 'Heading 2', 'Heading 3', 'Caption']:
            style = self.doc.styles[name]
            style.font.name = FONT
            style.font.color.rgb = RGBColor(0, 0, 0)
            snap = OxmlElement('w:snapToGrid')
            snap.set(qn('w:val'),'0')
            style.element.get_or_add_pPr().append(snap)
            fonts = style.element.get_or_add_rPr().rFonts
            fonts.set(qn('w:eastAsia'), FONT)
            for attribute in ['asciiTheme','hAnsiTheme','eastAsiaTheme','cstheme']:
                fonts.attrib.pop(qn('w:'+attribute),None)
        normal = self.doc.styles['Normal']
        normal.font.size = Pt(10.5)
        normal.paragraph_format.line_spacing = Pt(15)
        normal.paragraph_format.space_after = Pt(5)
        self.doc.styles['Title'].font.size = Pt(24)
        self.doc.styles['Title'].font.bold = True
        self.doc.styles['Title'].paragraph_format.line_spacing = 1.05
        self.doc.styles['Subtitle'].font.size = Pt(12)
        for name, size in [('Heading 1', 17), ('Heading 2', 12.5), ('Heading 3', 11)]:
            self.doc.styles[name].font.size = Pt(size)
            self.doc.styles[name].font.bold = True
            self.doc.styles[name].paragraph_format.line_spacing = 1.1
            self.doc.styles[name].paragraph_format.space_before = Pt(12)
            self.doc.styles[name].paragraph_format.space_after = Pt(7)
            self.doc.styles[name].paragraph_format.keep_with_next = True
        self.doc.styles['Caption'].font.size = Pt(9)
        self.doc.styles['Caption'].paragraph_format.line_spacing = Pt(12)
        self.doc.styles['Caption'].paragraph_format.space_before = Pt(4)
        self.doc.styles['Caption'].paragraph_format.space_after = Pt(7)
        footer = sec.footer.paragraphs[0]
        footer.alignment = WD_ALIGN_PARAGRAPH.CENTER
        field = OxmlElement('w:fldSimple')
        field.set(qn('w:instr'), 'PAGE')
        footer._p.append(field)
        self.doc.core_properties.title = '실측 따릉이 자료 기반 추천 선택과 정확 비가중 MIS의 고전 검증'
        self.doc.core_properties.subject = 'RD-1.0 회고적 조건부 추천 실험 연구결과'
        self.doc.core_properties.author = '연구 결과 보고서'
        self.table_number = 0
        self.text_audit = []

    def p(self, text, style=None):
        self.text_audit.append(text)
        p = self.doc.add_paragraph(text, style)
        p.paragraph_format.widow_control = True
        return p

    def h(self, text, level=1):
        if level > 1:
            text = re.sub(r'^(\d+)\s+(\d+)\s+',r'\1.\2 ',text)
        self.text_audit.append(text)
        p = self.doc.add_heading(text, level)
        targets = {'1 서론':'sec1','2 관련 연구':'sec2','3 연구 모형':'sec3',
                   '4 실험 방법':'sec4','5 실제 자료 분석 결과':'sec5',
                   '6 논의':'sec6','7 결론':'sec7','참고문헌':'refs',
                   '부록 재현 및 산출물 안내':'appendix'}
        if text in targets:
            identifier = str(list(targets).index(text)+1)
            start = OxmlElement('w:bookmarkStart')
            start.set(qn('w:id'),identifier)
            start.set(qn('w:name'),targets[text])
            end = OxmlElement('w:bookmarkEnd')
            end.set(qn('w:id'),identifier)
            p._p.insert(1 if p._p.pPr is not None else 0,start)
            p._p.append(end)
        return p

    def toc(self, text, bookmark):
        p = self.p(text)
        p.paragraph_format.space_after = Pt(4)
        p.paragraph_format.tab_stops.add_tab_stop(Cm(16.5), WD_TAB_ALIGNMENT.RIGHT, WD_TAB_LEADER.DOTS)
        p.add_run('\t')
        field = OxmlElement('w:fldSimple')
        field.set(qn('w:instr'), 'PAGEREF '+bookmark+' \\h')
        p._p.append(field)

    def page(self, title=None):
        if title:
            heading = self.h(title)
            heading.paragraph_format.page_break_before = True
        else:
            self.doc.add_page_break()

    def equation(self, text):
        p = self.doc.add_paragraph()
        p.alignment = WD_ALIGN_PARAGRAPH.CENTER
        math_node = OxmlElement('m:oMath')
        run = OxmlElement('m:r')
        token = OxmlElement('m:t')
        token.text = text
        run.append(token)
        math_node.append(run)
        p._p.append(math_node)
        self.text_audit.append(text)

    def table(self, caption, headers, rows, widths=None):
        self.table_number += 1
        cap = self.p(f'표 {self.table_number}  {caption}', 'Caption')
        cap.paragraph_format.keep_with_next = True
        table = self.doc.add_table(rows=1, cols=len(headers))
        table.alignment = WD_TABLE_ALIGNMENT.CENTER
        table.autofit = False
        widths = widths or [16.6 / len(headers)] * len(headers)
        for cell, label in zip(table.rows[0].cells, headers):
            cell.text = str(label)
        repeat = OxmlElement('w:tblHeader')
        table.rows[0]._tr.get_or_add_trPr().append(repeat)
        for row in rows:
            cells = table.add_row().cells
            for cell, text in zip(cells, row):
                cell.text = str(text)
        for i, row in enumerate(table.rows):
            no_split = OxmlElement('w:cantSplit')
            row._tr.get_or_add_trPr().append(no_split)
            for j, cell in enumerate(row.cells):
                cell.width = Cm(widths[j])
                cell.vertical_alignment = WD_CELL_VERTICAL_ALIGNMENT.CENTER
                tcpr = cell._tc.get_or_add_tcPr()
                shade = OxmlElement('w:shd')
                shade.set(qn('w:fill'), 'E5E5E5' if i == 0 else ('F7F7F7' if i % 2 == 0 else 'FFFFFF'))
                tcpr.append(shade)
                margins = OxmlElement('w:tcMar')
                for side in ['top', 'left', 'bottom', 'right']:
                    el = OxmlElement('w:' + side)
                    el.set(qn('w:w'), '75' if side in ('top','bottom') else '90')
                    el.set(qn('w:type'), 'dxa')
                    margins.append(el)
                tcpr.append(margins)
                borders = OxmlElement('w:tcBorders')
                for side in ['top', 'left', 'bottom', 'right']:
                    el = OxmlElement('w:' + side)
                    for k, v in [('val', 'single'), ('sz', '4'), ('color', 'D9D9D9')]:
                        el.set(qn('w:' + k), v)
                    borders.append(el)
                tcpr.append(borders)
                for p in cell.paragraphs:
                    p.paragraph_format.space_after = Pt(1)
                    p.paragraph_format.space_before = Pt(1)
                    p.paragraph_format.line_spacing = Pt(12)
                    if j > 0 and len(cell.text) < 35:
                        p.alignment = WD_ALIGN_PARAGRAPH.CENTER
                    for r in p.runs:
                        r.font.size = Pt(9)
                        r.font.bold = i == 0
                        r.font.color.rgb = RGBColor(0, 0, 0)
        spacer = self.p('')
        spacer.paragraph_format.space_after = Pt(0)
        spacer.paragraph_format.line_spacing = Pt(5)
        return table

    def save(self):
        QA.mkdir(parents=True, exist_ok=True)
        self.doc.save(OUT)
        (QA / 'report_text.txt').write_text('\n\n'.join(self.text_audit), encoding='utf-8')


def build():
    main_dir, sens_dir = ROOT/'outputs/rd1_main', ROOT/'outputs/rd1_sensitivity'
    cm, rows = assert_complete(main_dir)
    cs, sensitivity = assert_complete(sens_dir)
    manifest = read_json(main_dir/'manifest.json')
    data = read_json(ROOT/'outputs/rd1_data_cache/manifest.json')
    environment = read_json(ROOT/'outputs/rd1_environment.json')
    verification = read_json(ROOT/'outputs/rd1_verification.json')
    if verification['tests']['exit_code'] != 0 or not all(verification['core_code_hash_checks'].values()) or not verification['main_and_sensitivity_core_identical']:
        raise ValueError('최종 테스트 또는 실행 코드 동일성 검증 실패')
    split = read_json(ROOT/'outputs/rd1_data_cache/date_split.json')
    with (ROOT/'outputs/rd1_data_cache/window_registry.csv').open(encoding='utf-8-sig',newline='') as handle:
        registry_counts = Counter((r['status'],r['split']) for r in csv.DictReader(handle))
    post = ROOT/'outputs/rd1_postanalysis'
    # 후분석도 존재해야 최종 보고서를 생성한다. 누락을 0으로 치환하지 않는다.
    evidence = read_json(post/'evidence.json')
    if evidence['main_manifest']['config_hash'] != manifest['config_hash'] or evidence['main_manifest']['code_sha256'] != manifest['code_sha256']:
        raise ValueError('후분석과 본 실행의 설정 또는 코드 버전이 일치하지 않음')
    if evidence['main_completion']['completed_cases'] != len(rows) or evidence.get('qpu_executed') is not False:
        raise ValueError('후분석의 완료 수 또는 실행 범위 확인 실패')
    stats = read_json(post/'statistics.json')
    split_stats = read_json(post/'statistics_by_split.json')
    sensitivity_stats = read_json(post/'sensitivity_statistics.json')
    random = read_json(post/'random_baselines.json')
    event_sens = read_json(post/'event_sensitivity.json')
    benefit_audit = read_json(ROOT/'outputs/rd1_result_audit/final_benefit_audit.json')
    if benefit_audit['status'] != 'verified' or benefit_audit['population']['completed_variant_cases'] != len(rows):
        raise ValueError('편익 독립 검산의 상태 또는 실행 분모 불일치')
    negative_example = next(item for item in benefit_audit['audited_negative_cases'] if item['folder']=='2025-01-24_15_노원구_primary')
    if negative_example['selected_count'] != negative_example['candidate_count'] or set(negative_example['candidate_individual_benefits'].values()) != {1}:
        raise ValueError('음수 편익 설명 사례의 전제 불일치')
    positive_point = next(item for item in negative_example['changed_shortfall_points'] if item['joint_benefit_at_point']>0)
    negative_points = [item for item in negative_example['changed_shortfall_points'] if item['joint_benefit_at_point']<0]
    structure = read_json(main_dir/'postanalysis_structure/structure_diagnostics.json')
    current_manifest_hash = hashlib.sha256((main_dir/'manifest.json').read_bytes()).hexdigest()
    if structure['provenance']['manifest_sha256'] != current_manifest_hash or structure['provenance']['expected_cases'] != len(rows):
        raise ValueError('구조 사후 진단이 현재 주 실행과 일치하지 않음')
    example_dir = main_dir/'cases/2025-01-03_08_강서구_primary'
    example_case = read_json(example_dir/'case.json')
    example_graph = read_json(example_dir/'graph.json')
    example_groups = {}
    for candidate in example_case['candidates']:
        example_groups.setdefault(candidate['request_id'],[]).append(candidate)
    example_request, alternatives = next((key,value[:2]) for key,value in example_groups.items() if len(value)>=2)
    example_edge = next(edge for edge in example_graph['edges'] if {edge['source'],edge['target']}=={v['id'] for v in alternatives})
    if not any(reason.startswith('same_request:') for reason in example_edge.get('reasons',[])):
        raise ValueError('설명 사례의 동일 요청 엣지가 검증되지 않음')
    solver_versions = {'highs':set(),'cpsat':set()}
    run_counts, overruns = Counter(), 0
    for path in sorted((main_dir/'cases').glob('*/classical_benchmark.json')):
        benchmark = read_json(path)
        overruns += benchmark.get('budget_overrun_count',0)
        for run in benchmark.get('runs',[]):
            run_counts[run.get('algorithm','unknown')] += 1
            version = run.get('solver_version')
            if version:
                kind = 'cpsat' if run.get('algorithm')=='cpsat_graph' else 'highs'
                solver_versions[kind].add(str(version))
    sensitivity_limits = sensitivity_overruns = 0
    for path in sorted((sens_dir/'cases').glob('*/classical_benchmark.json')):
        for run in read_json(path).get('runs',[]):
            sensitivity_overruns += bool(run.get('budget_overrun'))
            sensitivity_limits += run.get('status')=='limit_reached' and not run.get('budget_overrun',False)
    primary = [r for r in rows if r['variant'] == 'primary']
    off = [r for r in rows if r['variant'] == 'benefit_filter_off']
    if len(primary) != manifest['selected_windows'] or len(off) != len(primary):
        raise ValueError('주 조건과 필터 해제의 실행 창 수가 manifest와 일치하지 않음')
    generation_complete = {'complete','no_beneficial_candidate','no_travel_eligible_candidate'}
    generated_primary = [r for r in primary if r.get('generation_status') in generation_complete]
    generated_off = [r for r in off if r.get('generation_status') in generation_complete]
    for row in primary:
        evaluation_path = main_dir/'cases'/f'{row["date"]}_{int(row["hour"]):02}_{row["district"]}_primary'/'operational_evaluation.json'
        if evaluation_path.exists():
            operation = read_json(evaluation_path)['classical_best']
            row['total_arrival_seconds'] = operation.get('total_extra_arrival_seconds')
            row['max_arrival_seconds'] = operation.get('max_extra_arrival_seconds')
    eligible_results = [r for r in primary if r.get('operational_objective') is not None]
    nonempty = [r for r in generated_primary if r.get('candidates',0)>0]
    nonempty_results = [r for r in nonempty if r.get('operational_objective') is not None]
    development = [r for r in primary if r['split']=='development']
    evaluation = [r for r in primary if r['split']=='evaluation']
    peak_example = next((r for r in primary if r['date']=='2025-06-05' and r['hour']==8 and r['district']=='강서구'),None)
    exact = [r for r in primary if r['encoding_status'] == 'exact']
    nonempty_exact = [r for r in exact if r.get('candidates',0)>0]
    logical_nonempty_exact = [r for r in exact if (r.get('exact_nodes') or 0)>0]
    report = Report()
    p, h, table = report.p, report.h, report.table
    raw = data['trip_audit']
    if 'raw_rows' not in raw or 'eligible_rows' not in raw:
        daily_audits = [read_json(path) for path in (ROOT/'outputs/rd1_data_cache/trip_audit').glob('*.json')]
        raw = {**raw, **{key:sum(item[key] for item in daily_audits) for key in ['raw_rows','eligible_rows']}}
    inv = data['inventory_audit']
    n, ne = len(primary), len(eligible_results)
    opt = count(primary, 'operational_optimal')
    hard = count(primary, 'relative_hard')
    unresolved = count(primary, 'unresolved')
    negative_primary = sum(value<0 for value in values(eligible_results,'benefit'))
    negative_off = sum(value<0 for value in values(off,'benefit'))
    fails = Counter(r['encoding_status'] for r in primary)
    fails_off = Counter(r['encoding_status'] for r in off)
    title = p('실측 따릉이 자료 기반\n추천 선택과 정확 비가중 MIS의\n고전 검증', 'Title')
    title.paragraph_format.space_before = Pt(25)
    title.paragraph_format.space_after = Pt(20)
    p('2025년 관측자료를 이용한 RD 1 회고적 조건부 추천 실험', 'Subtitle')
    p('연구결과 보고서  |  2026년 11월 9일')
    h('초록', 2)
    p(f'본 연구는 서울 공공자전거의 실제 완료 이용과 시간별 대여가능 수량을 연결하여, 이용자의 출발 또는 반납 대여소 변경 대안 중 선언한 재고 제약을 만족하는 최대 개수의 추천을 선택하였다. 원본 이용 {raw["raw_rows"]:,}행을 물리 여행 키로 중복 제거하여 {raw["canonical_rows"]:,}행을 보존하고, 연간 {manifest["registry_windows"]:,}개 시간·자치구 창을 등록하였다. 이 중 사전에 고정한 {n:,}개 창에 주 조건과 편익 필터 해제를 각각 적용했으며, 월별 규모가 큰 창에서는 추가 이동 조건을 한 번에 하나씩 바꾸었다.')
    p(f'주 조건의 정확 그래프 확보는 {len(exact):,}/{n:,}개였고 논리 노드가 하나 이상인 그래프는 {len(logical_nonempty_exact):,}개였다. 운영 최적성 확인은 {opt:,}개, 상대적 고난도는 {hard:,}개, 운영 미해결은 {unresolved:,}개였다. 분류 표본 부족은 쉬움 인증으로 세지 않았다. 단독 편익 필터를 적용해도 집합 BENEFIT가 음수인 사례는 {negative_primary:,}개였다. 이는 개별 후보의 개선 조건이 추천 집합의 개선을 보장하지 않음을 보여준다. 체크포인트와 사건별 재고 검사도 별도로 해석하였다.')
    p('결과는 관측 수량을 정시 사건 직전의 순간재고로 해석하고 선택된 추천을 모두 실행한다는 조건에 따른다. 실제 수락률, 반납 상한, 실패 수요 및 운영자 대응은 관측하지 않았다. 따라서 실제 서비스 개선 또는 온라인 운영 가능성을 입증한 결과로 해석하지 않는다. 본 실행 범위는 로컬 고전 검증이며 양자 장치 실행과 성능 비교는 수행하지 않았다.')
    p('주제어  공공자전거  추천 선택  재고 제약  최대 독립집합  정확 인코딩  고전 최적화')

    report.page('목차')
    for line,bookmark in [('1 서론','sec1'),('2 관련 연구','sec2'),('3 연구 모형','sec3'),('4 실험 방법','sec4'),
                          ('5 실제 자료 분석 결과','sec5'),('6 논의','sec6'),('7 결론','sec7'),
                          ('참고문헌','refs'),('부록 재현 및 산출물 안내','appendix')]:
        report.toc(line,bookmark)
    h('1 서론')
    p('공공자전거의 지역별 수급 불균형은 이용자가 원하는 시각과 장소에서 자전거를 이용할 수 있는지에 영향을 준다. 이용자의 출발 또는 반납 대여소를 주변 대안으로 유도하는 방식은 운영자 이동과 함께 검토할 수 있는 수단이다. 그러나 이용자가 실제로 수락하는지, 변경 경로가 다른 이용의 재고와 충돌하는지, 추천의 수가 늘면 수급 지표도 개선되는지는 서로 다른 질문이다. 기존 이용자 유인 연구가 사용자 비용과 반응을 모형화한 것처럼, 관측하지 않은 반응을 실제 행동으로 간주하지 않는 구분이 필요하다[1,3].')
    p('본 연구의 질문은 세 가지다. 첫째, 실제 자료와 명시한 이동 조건으로 생성한 추천 대안이 얼마나 남는가. 둘째, 재고와 요청 배타성을 보존한 비가중 최대 독립집합으로 정확하게 표현할 수 있는가. 셋째, 같은 운영 모형을 푼 고전 해의 개수, 부족량 지표, 사건별 재고 검증 결과가 어떠한가. 고난도 여부는 크기나 그래프 밀도로 결정하지 않고 실제 풀이와 인증 결과로 판단한다[6].')
    p('기여 범위는 실측 입력의 추적, 후보 자격과 개수 목적의 분리, 정확화 단계의 실패 보존, 원모형과 보조 그래프의 동시 검증이다. 완료 이용을 회고적으로 재구성하므로 잠재 수요를 추정하거나 인센티브 정책을 현장 배치한 연구는 아니다. 연구설계에서 정한 기준과 이번 실행에서 확인한 사실을 구별하여 제시한다.')

    report.page('2 관련 연구')
    h('2 1 이용자 추천과 재고 경로', 2)
    p('Singla 등은 사용자 반응과 이동 부담을 고려하여 자전거 공유의 균형을 유도하는 추천 체계를 제시하였다[1]. 이 연구는 대안을 권유하는 구조를 제공하지만, 그 수락 모형이나 가격 목적이 서울 관측자료에서 확인된 것은 아니다. Angelopoulos 등은 요청·차량·주차공간의 조합과 충돌 관계를 이용한 할당을 다루었다[2]. 이들의 예약 자원 및 가중 모형은 집계 재고에 근거한 본 연구의 비가중 목적과 구별된다.')
    p('Pfrommer 등은 동적 재고와 이동 경로를 고려한 재배치 및 가격 유인을 논의하였다[3]. Schuijbroek 등은 재고의 시간 경로와 서비스 수준을 다루었다[4]. 본 연구는 이 관점을 활용하되, 원래 완료 여행의 누적 순유출로 부족량 기준을 구성하는 자체 규약을 사용한다. 본 보고서의 L, J, BENEFIT 공식이 해당 문헌에서 그대로 검증되었다는 주장은 하지 않는다. Schuijbroek 논문의 공개 저자 원고는 발행판보다 이전 버전이라는 점도 구별한다.')
    h('2 2 최대 독립집합의 난도와 비교', 2)
    p('단위 원판 그래프의 최대 독립집합 난도는 사례 구조와 알고리즘에 의존하며, 노드 수 또는 밀도 하나로 보편적으로 판정할 수 없다[6]. 대규모 독립집합에서는 정확 축약과 전문 휴리스틱이 강한 기준선이 될 수 있다[7]. 따라서 일반 정수계획과 CP SAT뿐 아니라 전문 비교군의 구현 여부를 명시하고, 원래 운영 모형을 쉽게 풀 수 있는 경우를 보조 인코딩의 지연과 분리한다.')
    table('선행연구를 전용한 범위', ['근거', '채택한 관점', '본 연구에서 별도 확인할 내용'], [
        ('Singla 등[1]', '대체 이용 추천과 사용자 부담', '수락률과 금전 효과'),
        ('Angelopoulos 등[2]', '요청 대안의 충돌과 할당', '집계 재고의 고차 제약'),
        ('Pfrommer·Schuijbroek 등[3,4]', '동적 재고와 기준 경로', '서울 관측값의 시간 의미'),
        ('Andrist·Lamm 등[6,7]', '구조 진단과 강한 고전 기준선', '실제 사례별 난도'),
        ('Cawley·Talbot[9]', '선정과 평가의 분리', '시간 분할과 사전 실행군')], [3.7,5.5,7.4])
    p('후보 반경, 시간 한도, 반복 수, 최소 OD 표본 수와 통계 구간은 본 연구가 채택한 규약이다. Ban과 Hyun의 거리 수준은 민감도 설계의 참고이며 서울 전체 이용자의 수락 상한을 의미하지 않는다[5].')

    report.page('3 연구 모형')
    h('3 1 관측자료와 조건부 해석', 2)
    p('시각은 Asia/Seoul로 통일하고 각 정시 t₀부터 60분 동안 출발한 적격 여행을 요청으로 사용한다. 원래 이용시간은 0분 초과 120분 이하이며, 이 범위 밖 여행도 알려진 배경 사건으로 유지한다. 추천의 마지막 반납은 t₀+4시간 이내로 제한하고, 수급 평가에 필요한 다음 한 시간까지 읽기 위해 t₀+5시간 문맥을 확보한다. 출발 자치구가 분석 단위이지만, 연결된 지역 밖의 관측 사건도 배경 재고 계산에서 배제하지 않는다.')
    p('시간별 수량 B는 보간하지 않는다. 공식 제공 주기는 한 시간이지만 해당 값이 순간값인지 구간 요약인지가 확정되지 않았으므로 정시 사건 직전의 수량이라는 가정을 둔다[10]. 모든 관측점을 가진 실제 대여소 집합 S를 후보 필터 전에 고정한다. 같은 사례의 알고리즘과 이동 민감도는 이 집합을 공유한다. 실제 반납 수용 상한은 확인되지 않았으므로 빈자리·반납 실패·포화·교착 개선은 NA이다.')
    h('3 2 추천 후보 생성', 2)
    p('원래 대여소 위치를 보행 출발지와 최종 목적지의 대리값으로 사용한다. 주변 실제 대여소의 출발·반납 조합을 열거하되 원래 조합은 제외하고 가장 가까운 일부만 남기는 절단은 적용하지 않는다. 거리는 대권거리이며 실제 도로의 도보거리와 다르다. 승차시간은 해당 날짜 이전 OD 여행으로 추정한다. 2025년 1월 1일 이전 자료가 없어 연초의 과거 관측량 부족으로 후보가 배제될 수 있으며 이를 수요 없음이나 운영의 쉬움으로 해석하지 않는다.')
    table('주 조건의 이동 규약', ['항목', '주 조건', '해석'], [
        ('요청창과 평가', '60분 / 이후 4시간', '이후 5시간까지 문맥 확보'),
        ('총 보행과 끝점 반경', '각각 500m', '직선거리 근사와 정책값'),
        ('보행 속도와 추가 지연', '1.2m/s / 최대 15분', '관측된 개인별 속도 아님'),
        ('승차시간', '과거 28일 OD 중앙값', '10건 미만이면 90일로 확장'),
        ('근거 부족', 'OD 10건 미만 제외', '가상 여행으로 대체하지 않음'),
        ('실행 조건', '선택 추천의 전부 실행', '실제 수락률 및 효과 상한 아님')], [4.1,5.0,7.5])
    p('새 대여시각은 원래 대여에 출발 보행시간을 더하고, 새 반납은 추정 승차시간을 더하여 계산한다. 최종 도착에는 반납 후 보행도 포함한다. 시간은 초 단위 올림을 적용한다. 대여소 정보는 183일 이내의 가장 가까운 보유 스냅샷을 사용하며, 미래 스냅샷이면 회고적 메타데이터 대리값임을 표시한다.')

    report.page('3 연구 모형의 목적과 정확 대응')
    h('3 3 부족량 기준과 비가중 목적', 2)
    p('대여소 s의 시점 h 이후 한 시간 동안 완료 로그의 누적 출발 D와 반납 A를 계산한다. 같은 시각이면 출발을 먼저 처리한다. 기준 재고 L은 해당 관측 사건 순서를 외부 추가 공급 없이 지원하기 위한 초기 수량이며 잠재 수요나 최적 목표 재고를 뜻하지 않는다. 후보 v의 사건 차이 δ는 원래 출발 취소와 반납 취소, 변경 출발과 반납을 함께 반영한다.')
    report.equation('L(s,h) = max{0, max[u∈[0,1h)] (D(s,h,u) − A(s,h,u))}')
    report.equation('I(s,h,x) = B(s,h) + Σ[v] δ(s,v,h) x(v)')
    report.equation('J(x) = Σ[s,h] max{0, L(s,h) − I(s,h,x)}')
    report.equation('BENEFIT(x) = J(0) − J(x)      max Σ[v] x(v)')
    report.equation('Σ[v∈V(r)] x(v) ≤ 1      I(s,h,x) ≥ 0      x(v) ∈ {0,1}')
    p('주 후보는 단독 BENEFIT가 양수인 이동 적격 대안이다. 목적은 편익 가중합이 아닌 추천 개수이며, 동일 완료 여행에서 파생한 대안은 최대 하나만 선택한다. 모든 체크포인트 재고 하한을 지키며 J는 관측점별 자전거 부족량 합으로 해석한다. 실제 실패 건수나 인과효과가 아니다. 단독 편익이 양수여도 집합 편익은 0 또는 음수일 수 있어 최종 해마다 다시 계산한다.')
    p('실제 사용자 식별자가 없어 서로 다른 여행의 동일 개인 여부와 개인의 동시간 배정은 검증하지 못한다. 원본 자전거 ID는 물리 여행 중복 식별에 사용하지만 대체 여행에 특정 자전거를 가상 배정하지 않는다. 집계 재고 제약은 개별 자전거의 연속 경로나 예약 동시성을 인증하지 않는다.')
    h('3 4 정확 그래프와 원모형 검증', 2)
    p('같은 요청의 대안은 clique를 이룬다. 집계 재고에는 셋 이상이 함께 선택되어야 위반되는 제약과 다른 후보의 공급으로 구제되는 의존성이 존재할 수 있으므로, 후보 간 금지 엣지만으로 항상 동치가 되지는 않는다. 후보 그래프의 동치가 검증되지 않으면 원제약의 CNF와 보조 정점을 이용하여 정확 비가중 MIS로 변환한다. 원모형의 실행 가능 증인이 있고 후보 수 N, 절 수 m, W=N+1인 구성에서는 α(G)=Wm+OPT의 대응을 사용한다.')
    p('보조 정점은 실제 요청 또는 자전거가 아니다. 원시 독립집합 크기에서 임의로 상수를 빼지 않고, 복호화한 배정의 절과 원제약을 다시 검사한다. 정확화의 지지 크기·절·노드·엣지·시간 한도를 넘으면 실패로 보존한다. 이 실패는 계산적 고난도 인증이나 물리 장치 배치 불가능의 증명이 아니다.')
    v0 = alternatives[0]
    example_rows = [('관측 여행',f'{v0["original_origin"]} → {v0["original_destination"]}',f'{v0["original_start"][11:19]} → {v0["original_end"][11:19]}','해당 없음')]
    example_rows += [(f'파생 후보 {index+1}',f'{v["origin"]} → {v["destination"]}',f'{v["start"][11:19]} → {v["end"][11:19]}',f'{v["extra_walk_straight_m"]:.1f}m / {v["individual_benefit"]}') for index,v in enumerate(alternatives)]
    table('2025년 1월 3일 강서구의 실제 요청과 파생 후보', ['구분','대여소 ID','대여시각 → 반납시각','보행 / 단독 편익'],example_rows,[2.7,3.8,5.9,4.2])
    p(f'원본 여행에서 파생한 두 후보 {alternatives[0]["id"]}와 {alternatives[1]["id"]} 사이에 동일 요청 엣지가 실제 저장되어 있다. 따라서 둘을 함께 추천할 수 없다. 첫 행만 관측 여행이며 후보의 변경 경로·시각·편익은 의사결정 대안의 계산값이다.')

    report.page('4 실험 방법')
    h('4 1 모집단과 실행 표본', 2)
    p('연간 전체 날짜와 24개 정시, 25개 자치구의 조합을 먼저 등록했다. 자료 연결과 문맥 조건을 만족한 창의 수와 제외 이유를 남겼다. 등록 모집단과 실제 고전 풀이를 수행한 실행군은 별도이다. 이번 실행군은 원래 요청 수의 2의 거듭제곱 구간별로 월마다 최대 3개, 월별 이용 규모 상위 창, 사전 정의된 피크 창을 합친 고정 탐색 표본이다. 풀이 결과를 보고 표본을 바꾸지 않았다. 서울 전체의 평균 또는 무작위 대표표본으로 해석하지 않는다.')
    p(f'날짜 순서 분할의 개발 종료일은 {split["cutoff_date"]}이고 그 다음 날짜부터 평가 자료로 구분했다. 이미 개발에 사용한 날짜는 개발 측에 유지했다. 개발 창의 이후 5시간 문맥이 첫 평가일에 닿는 경계 창은 배제했다. 실제 경계 제외는 {split.get("embargo_windows", 0):,}개이며, 같은 날짜의 모든 자치구와 변형을 같은 쪽에 둔다. 평가 날짜의 완료 이동은 회고적 입력으로 사용하지만 그 결과로 후보 조건을 재조정하지 않는다[9].')
    table('모집단과 실행군을 구별하는 단위', ['단위', '정의', '사용 목적'], [
        ('등록 창', '연간 날짜 × 24시간 × 25자치구', '자료 적격성과 제외 사유'),
        ('실행 창', '결과를 보기 전 고정된 탐색 표본', '후보·정확화·고전 검증'),
        ('주 조건 사례', '실행 창마다 편익 필터 적용', '주 결과와 난도 판단'),
        ('필터 해제 사례', '같은 창에서 편익 조건만 해제', '후보 자격 효과 검증'),
        ('추가 민감도 사례', '월별 선택 창 × 10개 변형', '이동 가정 변화의 제한 분석')], [3.4,7.2,6.0])
    h('4 2 자료 보존과 실패 처리', 2)
    p('여행은 자전거 ID, 출발·반납 시각과 대여소로 구성한 물리 키로 중복을 제거하고 원본 출처를 보존한다. 한쪽 끝점만 확인되는 여행은 알려진 사건을 배경에 남긴다. 재고의 동일 키 중복과 충돌을 별도 검사하며 음수 또는 비정수 수량을 보정해 만들지 않는다. 후보 생성은 창당 600초와 4GiB 한도를 두고, 중단된 사례에서 제약을 삭제하거나 일부 후보만 성공 사례로 기록하지 않는다.')
    p('원본 파일, 설정과 분석 코드의 해시를 저장하여 입력과 실행 규약을 확인할 수 있게 했다. 비교에서 누락, 설치 실패, 생성 한도, 정확화 한도, 풀이 시간초과는 서로 다른 상태다. 과거 개발 사례의 결과는 이번 RD 1 규약의 실행 성과에 합산하지 않았다.')

    report.page('4 고전 검증과 평가 규약')
    h('4 3 풀이기와 고난도 정의', 2)
    p('원래 운영 모형은 HiGHS MILP로, 정확 MIS는 HiGHS MILP와 OR Tools CP SAT로 각각 풀었다[11,12]. 각 정식화는 단일 스레드, 회당 60초, 5개 고정 시드 20261109–20261113을 사용했다. CP SAT의 linearization_level=2는 개발 사례에서 확인한 뒤 모든 본 실행에 고정한 구현 규약이며 문헌의 처방값이 아니다. 평가 날짜 결과를 본 뒤 조정하지 않았다. 모델 생성과 lift는 풀이 시간과 구분하고 포트폴리오 비용은 실행된 정식화와 반복을 합산한다.')
    p('두 그래프 정확법에는 같은 열린 이웃을 갖는 비인접 정점의 정확 축약을 적용했다. 이 정점들은 최적 독립집합에서 모두 포함하거나 모두 제외할 수 있으므로, 내부 풀이에서 집합 크기를 가중치로 갖는 하나의 정점으로 묶고 원래 ID 전체로 복원한다. 이는 고전 전처리이며 저장된 원래 그래프와 추천 목적은 비가중으로 유지된다. 축약 시간은 60초 예산에 포함한다. 일반적인 축약의 필요성은 Lamm 등[7]을 참고했지만 이 대응의 구현과 증명은 별도 수학적 판단이다.')
    table('실행 예산과 실패 경계', ['대상', '예산 또는 조건', '결과에서의 구별'], [
        ('구조 진단', '5초', '요청 상계·정확 축약의 인증'),
        ('정확 인코딩', '60초 / 지지 16 / 절 5,000', '한도 초과는 정확화 실패'),
        ('정확 그래프', '노드 12,000 / 엣지 500,000', '노드 삭제로 적합화하지 않음'),
        ('각 정확법', '60초 × 5시드 / 1스레드', '정상 제한시간과 실행 오류'),
        ('전문 MIS 비교군', 'ReduMIS는 Windows POSIX 지원 제약으로 미실행', '시간초과가 아닌 비교군 누락')], [4.0,6.4,6.2])
    p('상대적 고난도는 구조 감사의 잔여 최대 성분 크기 구간에 20개 이상이 있을 때, 가장 빠른 정확법의 증명시간 중앙값이 상위 10%이며 1초 이상인 경우다. 20개 미만 구간은 분류 표본 부족이며 쉬움 인증이 아니다. 이 크기는 내부 false twin 축약 크기와 다르다. 포트폴리오 하한에는 검증된 탐욕 해와 별도 비용도 포함한다. 운영 미해결은 지정 정확법의 정상 실행 후 같은 추천 단위의 상하한 차이가 양수인 경우다. 원모형 최적성이 확인되면 그래프 지연만으로 운영 미해결이 되지 않는다.')
    h('4 4 운영 및 통계 평가', 2)
    p('원래 이동 유지, 후보 ID순 탐욕, 단독 편익 내림차순 탐욕, 검증된 고전 최선해를 같은 S와 관측점에서 비교한다. 추천 수, J와 BENEFIT, 직선 추가 보행, 체크포인트·사건별 재고 통과를 함께 제시한다. 엄격 사건 하위군은 기준선과 선택 해의 사건 제약 통과 및 모든 관측점 잔차 0을 동시에 요구한다. 이 하위군도 사건 순서와 순간값 해석에 조건부이다.')
    p('날짜별 대응 차이는 날짜 블록을 재표집하는 2,000회 bootstrap 95% 구간으로 보조 요약한다. 날짜가 10개 미만이면 기술통계로 한정한다. 독립 비트 0.5의 무작위 선택과 공통 수선은 실제 그래프의 알고리즘 기준선이며 합성 이용자를 생성한 것이 아니다. 일반화 범위는 고정 실행군에 제한하고, 장치 성능이나 양자 우위에 관한 결론은 내리지 않는다[8].')

    report.page('5 실제 자료 분석 결과')
    h('5 1 원자료 감사와 모집단', 2)
    table('실제 원자료 처리 결과', ['항목', '관측값', '처리 또는 의미'], [
        ('원본 여행 행', fmt(raw['raw_rows']), '완료 이용 원자료'),
        ('중복 제거 후 여행 행', fmt(raw['canonical_rows']), '물리 여행 키 기준'),
        ('제거한 중복 행', fmt(raw['duplicate_rows']), '출처를 보존한 중복 제거'),
        ('요청 시간 조건 적격 여행', fmt(raw['eligible_rows']), '0분 초과 120분 이하 등'),
        ('불완전 끝점 여행', fmt(raw['incomplete_endpoint_rows']), '알려진 한쪽 사건은 유지'),
        ('재고 유효 키', fmt(inv['valid_keys']), '대여소와 관측 시각'),
        ('재고 관측 날짜', fmt(len(data['inventory_dates'])), '365일 중 관측이 있는 날짜'),
        ('재고 무효 / 충돌 / 동일중복', f'{inv["invalid_rows"]} / {inv["conflicting_keys"]} / {inv["identical_duplicate_rows"]}', '임의 보정하지 않음'),
        ('재고가 없는 날짜', '2025년 1월 9일과 10일', '등록부에서 제외 사유 보존')], [5.2,4.9,6.5])
    p('원자료의 여행 수와 관측 재고의 행 수는 서로 다른 단위이다. 완료 여행이 관측되었다고 해서 실패 요청이나 운영자의 보충 이동까지 완전하게 기록되었다는 뜻은 아니다. 날짜 전체가 없는 두 날 이외에도 개별 시간의 관측 누락을 문맥 적격성 검사에 반영했다. 대여소 메타데이터는 6월과 12월 스냅샷을 날짜 근접 규칙으로 연결했으며, 파일의 월말 날짜를 대리 날짜로 사용한 정밀도 한계도 기록했다.')
    table('등록 창과 실제 실행 범위', ['항목', '개수'], [
        ('연간 등록 창', fmt(manifest['registry_windows'])),
        ('자료 적격 창', fmt(manifest['registry_eligible_windows'])),
        ('적격 창 개발 / 평가', f'{registry_counts[("eligible","development")]:,} / {registry_counts[("eligible","evaluation")]:,}'),
        ('사전 고정 실행 창 / 서로 다른 날짜', f'{n:,}개 창 / {len({r["date"] for r in primary}):,}일'),
        ('주 조건과 필터 해제 총 사례', fmt(len(rows))),
        ('주 조건 개발 / 평가 사례', f'{count(primary, "split", "development"):,} / {count(primary, "split", "evaluation"):,}'),
        ('추가 이동 민감도 사례', fmt(len(sensitivity)))], [10.5,6.1])
    p(f'주 실행과 추가 민감도는 각각 저장된 완료 수 {cm["completed_cases"]:,}개와 {cs["completed_cases"]:,}개를 기준으로 집계하였다. 자료 적격 전체 창을 모두 고전 풀이한 결과는 아니며, 선택한 실행군의 성공과 실패를 동일 분모에 유지했다.')

    report.page('5 후보 생성과 정확화 결과')
    h('5 2 후보와 그래프 규모', 2)
    table('주 조건과 필터 해제의 실제 비교', ['지표', '주 조건', '편익 필터 해제'], [
        ('실행 사례', fmt(len(primary)), fmt(len(off))),
        ('후보 수 중앙값', med(generated_primary,'candidates'), med(generated_off,'candidates')),
        ('후보 수 범위', extent(generated_primary,'candidates'), extent(generated_off,'candidates')),
        ('완료 사례 중 후보 0개', fmt(sum(r['candidates']==0 for r in generated_primary)), fmt(sum(r['candidates']==0 for r in generated_off))),
        ('정확 그래프 확보', denom(len(exact),n), denom(count(off,'encoding_status','exact'),len(off))),
        ('후보 있는 사례 중 정확화', denom(len(nonempty_exact),len(nonempty)),denom(sum(r['encoding_status']=='exact' and r['candidates']>0 for r in generated_off),sum(r['candidates']>0 for r in generated_off))),
        ('정확 그래프 노드 범위', extent(primary,'exact_nodes'), extent(off,'exact_nodes')),
        ('원모형 최적성 확인', denom(opt,n), denom(count(off,'operational_optimal'),len(off))),
        ('후보 있는 사례의 최단 증명시간 중앙값 초', med(nonempty,'fastest_proof_seconds',4), med([r for r in generated_off if r['candidates']>0],'fastest_proof_seconds',4))], [7.0,4.8,4.8])
    p('요청·후보 규모의 요약에는 생성 완료 사례만 포함했다. 생성 중단 기록의 0을 실제 후보 0개로 세지 않는다. 실패는 전체 실행 분모에 남긴다. 후보 수 감소는 자격을 통과한 선택지가 줄었다는 의미이며 수락이나 실제 실패 수요 감소가 아니다. 필터 해제는 후보 모형 자체를 변경하므로 동일 문제에서의 알고리즘 성능 차이로 해석하지 않는다.')
    p(f'두 조건의 저장된 알고리즘 반복 기록은 원모형 HiGHS {run_counts["highs_operational"]:,}회, 그래프 HiGHS {run_counts["highs_graph"]:,}회, 그래프 CP SAT {run_counts["cpsat_graph"]:,}회였다. 빈 후보·그래프의 자명한 해 반환도 포함하므로 모두 풀이기 내부 호출을 뜻하지는 않는다. 예산 초과 {overruns:,}회는 60초 비교에서 제외하며 정확 그래프 부재를 풀이 성공으로 채우지 않았다.')
    h('5 3 단계별 실패와 인증', 2)
    failure_labels = {
        'exact':('정확 그래프 확보','선언 모형과 동치인 그래프'),
        'encoding_limit_exceeded':('정확 인코딩 한도 초과','지지 크기·절·노드·시간 한도 등'),
        'graph_generation_failed':('그래프 생성 실패','필수 엣지 한도 등 상세 원인 보존'),
        'encoding_failed':('정확화 실패','정확화 한도 또는 인증 실패'),
        'not_attempted':('미시도','후보 생성 등 선행 단계에서 중단'),
        'no_beneficial_candidate':('편익 후보 없음','적격 후보 0개')}
    table('주 조건과 필터 해제의 정확화 상태', ['상태','주 조건','필터 해제','해석'], [(failure_labels.get(key,(key,''))[0],fmt(fails[key]),fmt(fails_off[key]),failure_labels.get(key,(key,'사례별 실패 로그 참조'))[1]) for key in sorted(set(fails)|set(fails_off))], [4.6,2.0,2.2,7.8])
    generation_counts = Counter(r['generation_status'] for r in primary)
    generation_labels = {'complete':'후보 생성 완료','no_beneficial_candidate':'양의 편익 후보 없음','no_travel_eligible_candidate':'이동 적격 후보 없음','generation_limit':'생성 예산 초과','data_ineligible':'자료 부적격'}
    off_generation_counts = Counter(r['generation_status'] for r in off)
    p('후보 생성 단계의 주 조건은 '+', '.join(f'{generation_labels.get(key,key)} {value:,}개' for key,value in sorted(generation_counts.items()))+'이며 필터 해제는 '+', '.join(f'{generation_labels.get(key,key)} {value:,}개' for key,value in sorted(off_generation_counts.items()))+'였다. 생성 상태와 정확화 상태를 구별한다.')
    p(f'사전 난도 규칙에서 주 조건의 상대적 고난도는 {hard:,}개, 운영 미해결은 {unresolved:,}개였다. 규모 구간당 20개 미만 또는 필요한 실행·구조가 없는 사례는 상대 분류 대상이 아니므로, 고난도 0개가 모든 사례의 쉬움 인증을 뜻하지 않는다. 정확화 중단을 어려운 그래프의 성공 표본으로 세지 않으며 완화 그래프의 목적값을 정확 운영 최적값으로 사용하지 않는다.')

    report.page('5 운영 결과와 고전 기준선')
    h('5 4 검증된 해의 추천 수와 재고 지표', 2)
    result_groups = [eligible_results,nonempty_results]
    outcomes = [
        ('운영 목적값이 기록된 사례',denom(ne,n),denom(len(nonempty_results),len(nonempty))),
        ('선택 추천 수 중앙값과 범위',*[f'{med(g,"operational_objective")} / {extent(g,"operational_objective")}' for g in result_groups]),
        ('BENEFIT 중앙값과 범위',*[f'{med(g,"benefit")} / {extent(g,"benefit")}' for g in result_groups]),
        ('BENEFIT 양수 / 0 / 음수',*[' / '.join(fmt(sum(v>0 if sign==1 else v<0 if sign==-1 else v==0 for v in values(g,'benefit'))) for sign in [1,0,-1]) for g in result_groups]),
        ('체크포인트 통과',*[denom(count(g,'checkpoint_feasible'),len(g)) for g in result_groups]),
        ('기준선 사건 재고 통과',*[denom(count(g,'event_baseline_feasible'),len(g)) for g in result_groups]),
        ('선택 해 사건 재고 통과',*[denom(count(g,'event_feasible'),len(g)) for g in result_groups]),
        ('엄격 사건 하위군',*[denom(count(g,'strict_event_subgroup'),len(g)) for g in result_groups]),
        ('합계 직선 추가 보행 중앙값 m',*[med(g,'total_walk_m') for g in result_groups]),
        ('합계 추가 도착시간 중앙값 초',*[med(g,'total_arrival_seconds') for g in result_groups]),
        ('실제 반납 용량 및 실패 개선','NA','NA')]
    table('전체 및 후보가 있는 주 조건의 운영 평가', ['지표','전체 주 조건','후보 1개 이상'],outcomes,[7.0,4.8,4.8])
    p('체크포인트를 통과해도 관측 시점 사이에서 재고가 음수가 될 수 있다. 선택 해의 사건 검증 통과와 원래 경로의 통과를 함께 제시한 이유는 관측 로그 또는 시간 의미에 이미 존재한 불일치를 추천의 효과와 구분하기 위해서다. 재구성 잔차를 가상의 운영자 이동으로 메우지 않았다.')
    evaluation_nonempty = [r for r in evaluation if r.get('generation_status') in generation_complete and r['candidates']>0]
    evaluation_negative = sum(v<0 for v in values(evaluation_nonempty,'benefit'))
    p(f'날짜 분할별 운영 최적성 확인은 개발 {denom(count(development,"operational_optimal"),len(development))}, 평가 {denom(count(evaluation,"operational_optimal"),len(evaluation))}였다. 평가 측의 후보 존재 창에서도 집합 BENEFIT 악화는 {denom(evaluation_negative,len(evaluation_nonempty))}였다. 전체 표는 두 분할을 합친 기술통계이며 평가 측 결과로 조건을 조정하지 않았다.')
    table('동일 주 조건에서 운영 기준선의 중앙값', ['정책', '추천 수', 'BENEFIT'], [
        ('원래 이동 유지', med(eligible_results,'original_count'), med(eligible_results,'original_benefit')),
        ('후보 ID순 탐욕', med(eligible_results,'greedy_id_count'), med(eligible_results,'greedy_id_benefit')),
        ('단독 편익순 탐욕', med(eligible_results,'greedy_benefit_count'), med(eligible_results,'greedy_benefit_benefit')),
        ('검증된 고전 최선해', med(eligible_results,'operational_objective'), med(eligible_results,'benefit'))], [8.0,4.3,4.3])
    if peak_example is not None:
        p(f'피크 규칙과 연결된 개발 사례인 2025년 6월 5일 08시 강서구는 후보 {peak_example["candidates"]:,}개에서 추천 {fmt(peak_example["operational_objective"])}개를 선택했다. 운영 최적성 확인은 {fmt(peak_example["operational_optimal"])}, BENEFIT는 {fmt(peak_example["benefit"])}, 사건 재고 통과는 {fmt(peak_example["event_feasible"])}였다. 추천 수의 증명, 부족량 변화, 사건 실행 가능성은 서로 다른 판정이다. 중앙값과 이 설명 사례를 서울 전체의 평균 효과로 일반화하지 않는다.')
    else:
        p('고전 최선해는 추천 개수를 우선하므로 탐욕법보다 BENEFIT가 높아야 한다는 보장은 없다. 중앙값은 사례 단위 기술통계이며 서울 전체의 평균 효과가 아니다. 최적값 0에서는 상대 품질 비율 대신 절대 개수를 해석한다.')

    report.page('5 민감도와 보조 분석')
    h('5 5 이동 조건의 민감도', 2)
    labels = {'walk_200m':'보행 200m','walk_750m':'보행 750m','walk_1000m':'보행 1,000m','speed_1_0':'속도 1.0m/s','speed_1_4':'속도 1.4m/s','delay_5min':'지연 5분','delay_30min':'지연 30분','od_min5':'OD 최소 5건','od_min20':'OD 최소 20건','distance_time_proxy':'거리차 시간 근사'}
    sensrows = []
    for variant,label in labels.items():
        group = [r for r in sensitivity if r['variant']==variant]
        generated = [r for r in group if r.get('generation_status') in generation_complete]
        evaluated_dates = sensitivity_stats['variants'][variant]['evaluation']['operational_objective']['dates']
        sensrows.append((label,f'{len(generated)}/{len(group)}',med(generated,'candidates'),med(generated,'operational_objective'),f'{count(group,"encoding_status","exact")}/{len(group)}',fmt(evaluated_dates)))
    table('월별 고정 창에서 한 조건씩 변경한 기술통계', ['변형', '생성 완료\n/ 등록', '후보\n중앙값', '추천\n중앙값', '정확화', '평가\n날짜'], sensrows, [4.4,2.4,2.6,2.6,2.4,2.2])
    p(f'추가 민감도 {len(sensitivity)}개 중 운영 최적성 확인은 {count(sensitivity,"operational_optimal")}개였다. 정상적으로 제한시간에 도달한 풀이 반복은 {sensitivity_limits}회, 예산 초과 반복은 {sensitivity_overruns}회로 별도 집계했다. 각 변형은 최대 12개여서 규모 구간당 20개 조건을 충족할 수 없다. 상대적 난도는 분류 표본 부족인 NA이며, 저장된 relative_hard=0가 모든 사례의 쉬움 인증은 아니다.')
    p('추가 민감도는 월별 원래 요청 수가 가장 큰 창을 결과 확인 전에 고정한 제한 분석이다. 규모 중앙값은 생성 완료 사례만 사용한다. 10개 변형은 한 항목만 바꾸며 반경 변화 시 끝점 반경과 총 보행 한도를 함께 바꾼다. 4시간 반납 종료와 실제 대여소 집합은 유지한다. 개발·평가를 합친 표는 기술통계이다. 평가 날짜가 10개 미만인 민감도는 구간에 근거한 일반화 없이 개별 사례와 범위로 해석한다.')
    p('원래 여행의 이용시간과 직선거리 차이를 이용하는 시간 근사는 과거 OD 표본 부족을 다르게 취급하는 변형이다. 그 결과를 관측 승차시간 근거가 강화된 것으로 해석할 수 없다. 개별 변형의 입력과 평가 결과는 별도로 저장했으며, 민감도 결과를 보고 주 조건을 다시 선택하지 않았다.')

    report.page('5 날짜 비교와 보조 검증')
    h('5 6 평가 날짜의 블록 대응 비교', 2)
    comparison_rows = []
    for key,label in [
        ('greedy_id_recommendations','고전 최선 − ID 탐욕 추천 수'),
        ('greedy_benefit_recommendations','고전 최선 − 편익 탐욕 추천 수'),
        ('greedy_benefit_benefit','고전 최선 − 편익 탐욕 BENEFIT'),
        ('filter_off_candidates','필터 해제 − 주 조건 후보 수'),
        ('filter_off_operational_objective','필터 해제 − 주 조건 추천 수'),
        ('filter_off_benefit','필터 해제 − 주 조건 BENEFIT')]:
        item = split_stats['evaluation'][key]
        ci = item.get('ci95')
        comparison_rows.append((label,f'{item["paired_rows"]}/{item["denominators"]["cohort_rows"]}',fmt(item['dates']),fmt(item['median_paired_difference'],1),
                                f'[{ci[0]:,.1f}, {ci[1]:,.1f}]' if ci else 'NA'))
    table('평가 측 대응 차이의 중앙값과 날짜 블록 95% 구간', ['차이의 방향과 지표','유효 쌍\n/ 전체','날짜','중앙값','95% 구간'], comparison_rows, [6.8,2.3,1.2,2.0,4.3])
    p('평가 해석은 개발과 분리한 위 표를 우선한다. 개발·평가를 합친 결과는 탐색적 기술통계이다. 유효 쌍은 양쪽 생성 완료와 해당 수치가 있는 창만 사용하며 제외 창은 분모에 남겼다. 구간은 날짜 블록 재표집의 불확실성으로, 관측 편향이나 순간재고 가정의 불확실성을 제거하지 않는다. 필터 조건 간 차이는 동일 문제의 알고리즘 우위가 아니다.')
    h('5 7 무작위 기준선과 사건 순서', 2)
    if random:
        shots = sum(batch['shots'] for item in random for batch in item['batches'])
        random_rows = []
        for name,label in [('raw','원시 무작위 비트'),('repaired','공통 수선 후')]:
            good = sum(batch[name]['valid_samples'] for item in random for batch in item['batches'])
            best = [max((batch[name]['best_candidate_count'] for batch in item['batches']
                         if batch[name]['best_candidate_count'] is not None),default=None) for item in random]
            finite = [v for v in best if v is not None]
            random_rows.append((label,denom(good,shots),fmt(statistics.median(finite),1) if finite else 'NA'))
        table(f'평가 측 실제 정확 그래프 {len(random)}개의 무작위 기준선', ['표본 처리','유효 표본 / 전체','사례별 최고 추천 중앙값'], random_rows, [4.2,6.2,6.2])
        p(f'각 그래프에서 독립 비트 확률 0.5로 500표본씩 5회 생성했으며 총 {shots:,}표본을 검사했다. 1–80노드 조건은 내부 축약 그래프가 아닌 저장된 원래 정확 그래프에 적용했다. 무효 표본도 분모에 남겼으며 큰 사례 전체를 대표하지 않는다.')
    else:
        p('평가 측 사전 크기 조건 1–80노드를 충족한 정확 논리 그래프가 없어 무작위 기준선의 실행 대상은 0개였다. 이 공백은 표본을 교체하거나 합성 그래프를 추가하여 채우지 않았다.')
    complete_events = [item for item in event_sens if item.get('status')=='complete']
    event_rows = []
    for key,label in [('departure_first','출발 우선'),('return_first','반납 우선'),('return_delay_60s','반납 처리 지연 60초')]:
        group = [item[key] for item in complete_events]
        event_rows.append((label,denom(count(group,'event_feasible'),len(group)),denom(count(group,'strict_event_subgroup'),len(group))))
    table('월별 창의 고정 추천에 대한 사건 재검사', ['시간 규약','사건 재고 통과','엄격 하위군'], event_rows, [6.0,5.3,5.3])
    p(f'사건 민감도 등록 {len(event_sens)}개 중 재검사가 완료된 {len(complete_events)}개를 표의 분모로 사용했다. 추천 집합은 고정했다. 순서와 지연에 따른 변화는 재최적화 정책의 성능이나 실제 처리 지연의 관측치가 아니다.')

    report.page('6 논의')
    h('6 1 결과의 의미와 한계', 2)
    p('저장된 해를 독립 검산하였다. 후보가 있는 요청 수 R이 상한이므로 실행 가능한 q=R은 최적성 인증이다. q<R은 최적성 증명과 재고 외 추가 자원 제약 부재를 함께 확인할 때만 재고 제한으로 판정한다. 이 사후 진단으로 실험군이나 난도 규칙을 바꾸지 않았다.')
    structure_rows = []
    for variant,label in [('primary','주 조건'),('benefit_filter_off','필터 해제')]:
        for partition,partlabel in [('development','개발'),('evaluation','평가')]:
            group = next(g for g in structure['groups'] if g['variant']==variant and g['split']==partition)
            structure_rows.append((f'{label} {partlabel}',fmt(group['nonempty_cases']),fmt(group['request_bound_certificate_cases']),fmt(group['inventory_limited_cases']),fmt(group['category_counts'].get('unproven_below_request_bound',0))))
    table('후보 요청이 있는 사례의 사후 구조 진단', ['조건과 분할','비어 있지 않음','요청 상한 도달','재고 제한','상한 미달 미증명'],structure_rows,[4.7,3.0,3.0,2.6,3.3])
    p(f'주 조건의 집합 BENEFIT 악화는 {denom(negative_primary,len(nonempty))}의 후보 존재 창에서 발생했고 필터 해제에서는 {negative_off:,}개였다. 독립 검산한 개발 사례인 2025년 1월 24일 15시 노원구는 후보 {negative_example["candidate_count"]}개가 각각 단독 편익 +1이지만 모두 선택하면 J가 {negative_example["baseline_j"]}에서 {negative_example["selected_j"]}로 증가했다. {positive_point["station_id"]} 대여소 16시의 공급 +{positive_point["selected_delta"]}는 부족 {positive_point["baseline_shortfall"]}의 해소에서 개선이 포화된다. {negative_points[0]["station_id"]}의 17시·18시에는 각각 인출 {-negative_points[0]["selected_delta"]}가 여유 {negative_points[0]["B"]-negative_points[0]["L"]}를 넘어 부족이 1씩 생겨, 합계 편익은 {negative_example["positive_joint_point_benefit"]}{negative_example["negative_joint_point_benefit"]:+d}={negative_example["joint_benefit"]}이 된다. 개수 최적성이 집합 개선을 보장하지 않는 실제 반례다.')
    p('시간별 수량의 순간값 해석과 미관측 배경 공급은 핵심 한계다. 완료 여행에서 계산한 부족량은 실제 실패 시도 수가 아니며 고정된 배경 아래의 값이므로 현장 인과효과로 읽을 수 없다. 공급 의존성 때문에 전부 실행 가정도 효과의 수학적 상한을 보장하지 않는다.')
    twin_examples = [r for r in structure['representatives'] if r.get('twin_after_nodes') is not None]
    if twin_examples:
        exemplar = twin_examples[0]
        p(f'사후 설명 사례인 {exemplar["date"]} {int(exemplar["hour"]):02d}시 {exemplar["district"]}의 {"주 조건" if exemplar["variant"]=="primary" else "필터 해제"}는 후보 {exemplar["candidate_count"]:,}개에서 정확 그래프 {exemplar["exact_nodes"]:,}노드가 되었고, 고전 내부 축약은 {exemplar["twin_after_nodes"]:,}개 대표 정점을 사용했다. 검산된 추천 수는 {exemplar["validated_recommendations"]:,}개였다. 이러한 크기 변화는 표현과 전처리 비용이며 운영 고난도의 증거가 아니다.')
    else:
        p('정확 보조 그래프의 팽창은 표현 비용이다. 원모형이 쉽게 증명되면 보조 그래프의 지연만으로 운영 고난도를 주장할 수 없다. 고전의 정확 축약도 비교군의 일부이다[7].')
    h('6 2 후속 검증의 우선순위', 2)
    p('후속 연구는 재고의 시간 의미, 실제 반납 상한, 운영자 이동과 실패 기록을 먼저 확인해야 한다. 이동 부담과 수락은 현장 자료로 추정할 대상이다. 현재 누락된 전문 MIS 방법을 지원 환경에서 같은 입력·예산으로 검증하고, 인코딩 개선과 운영 난도를 구별해야 한다. 개발 단계의 예산 초과 진단은 별도 보존했으며 본 성능 집계에 합산하지 않았다. 반복 시드를 새로운 운영 사례로 세거나 현재 결과를 하드웨어 우위로 해석하지 않는다[7–9].')
    h('7 결론')
    hard_off, unresolved_off = count(off,'relative_hard'),count(off,'unresolved')
    hardness_conclusion = ('이번 실행군에서 사전 규칙에 따른 고난도 또는 미해결 사례를 확보하지 못했다.'
                          if hard+hard_off+unresolved+unresolved_off==0 else
                          '확보한 사례는 사전 규칙과 이 실행군 안에서 해석해야 한다.')
    p(f'운영 최적성은 주 조건 {opt:,}/{n:,}개, 필터 해제 {count(off,"operational_optimal"):,}/{len(off):,}개에서 확인되었다. 상대적 고난도는 각각 {hard:,}개와 {hard_off:,}개, 운영 미해결은 {unresolved:,}개와 {unresolved_off:,}개였다. {hardness_conclusion} 후보가 있는 주 조건 {len(nonempty):,}개 중 정확화는 {len(nonempty_exact):,}개, 비어 있지 않은 정확 그래프는 {len(logical_nonempty_exact):,}개였다. 주 조건의 집합 지표 악화 {negative_primary:,}개는 개수 최적성과 수급 개선이 다름을 보여준다. 결과는 선택 실행군과 변환 한도에 한정되며 서울 전체에 고난도가 없다는 증명, 실제 서비스 개선 또는 양자 우위의 증거가 아니다.')

    report.page('참고문헌')
    references = [
        '[1] Singla, A., Santoni, M., Bartók, G., Mukerji, P., Meenen, M., & Krause, A. (2015). Incentivizing Users for Balancing Bike Sharing Systems. AAAI, 29(1), 723–729. https://doi.org/10.1609/aaai.v29i1.9251  원문 https://las.inf.ethz.ch/files/singla15incentivizing.pdf',
        '[2] Angelopoulos, A., Gavalas, D., Konstantopoulos, C., Kypriadis, D., & Pantziou, G. (2016). Incentivization Schemes for Vehicle Allocation in One-Way Vehicle Sharing Systems. IEEE ISC2, 1–7. https://doi.org/10.1109/ISC2.2016.7580857  원문 https://corelab.ntua.gr/~angelop/Publications/Incentivization_Schemes_One_Way_VSS.pdf',
        '[3] Pfrommer, J., Warrington, J., Schildbach, G., & Morari, M. (2014). Dynamic Vehicle Redistribution and Online Price Incentives in Shared Mobility Systems. IEEE TITS, 15(4), 1567–1578. https://doi.org/10.1109/TITS.2014.2303986  저자 원고 https://arxiv.org/abs/1304.3949',
        '[4] Schuijbroek, J., Hampshire, R. C., & van Hoeve, W.-J. (2017). Inventory rebalancing and vehicle routing in bike sharing systems. European Journal of Operational Research, 257(3), 992–1004. https://doi.org/10.1016/j.ejor.2016.08.029  공개 구버전 https://www.andrew.cmu.edu/user/vanhoeve/papers/bike_sharing.pdf',
        '[5] Ban, S., & Hyun, K. H. (2019). Designing a User Participation-Based Bike Rebalancing Service. Sustainability, 11(8), 2396. https://doi.org/10.3390/su11082396',
        '[6] Andrist, R. S., et al. (2023). Hardness of the maximum-independent-set problem on unit-disk graphs and prospects for quantum speedups. Physical Review Research, 5, 043277. https://doi.org/10.1103/PhysRevResearch.5.043277  원문 https://arxiv.org/html/2307.09442v3',
        '[7] Lamm, S., Sanders, P., Schulz, C., Strash, D., & Werneck, R. F. (2017). Finding near-optimal independent sets at scale. Journal of Heuristics, 23, 207–229. https://doi.org/10.1007/s10732-017-9337-x  저자 원고 https://arxiv.org/abs/1509.00764',
        '[8] Rønnow, T. F., et al. (2014). Defining and detecting quantum speedup. Science, 345, 420–424. https://doi.org/10.1126/science.1252319  저자 원고 https://arxiv.org/abs/1401.2910',
        '[9] Cawley, G. C., & Talbot, N. L. C. (2010). On Over-fitting in Model Selection and Subsequent Selection Bias in Performance Evaluation. Journal of Machine Learning Research, 11(70), 2079–2107. https://jmlr.org/papers/v11/cawley10a.html',
        '[10] 서울 열린데이터광장. 공공자전거 대여이력, 대여소 정보, 시간별 대여가능 수량. https://data.seoul.go.kr/dataList/OA-15182/F/1/datasetView.do ; https://data.seoul.go.kr/dataList/OA-13252/F/1/datasetView.do ; https://data.seoul.go.kr/dataList/OA-22382/F/1/datasetView.do',
        '[11] HiGHS. HiGHS optimization software. https://highs.dev/',
        '[12] Google OR-Tools. CP-SAT Solver. https://developers.google.com/optimization/cp/cp_solver',
    ]
    for ref in references:
        pr = p(ref)
        add_reference_links(pr,ref)
        pr.paragraph_format.space_after = Pt(6)
        pr.paragraph_format.line_spacing = Pt(11.5)
        for run in pr.runs:
            run.font.size = Pt(8.5)
    p('문헌의 원문·확인 범위는 프로젝트의 참고문헌 대장과 함께 관리한다. 공개 저자 원고와 최종 발행판을 구별했으며, 소프트웨어·자료 링크는 논문과 별도 출처로 표시했다. 보고서 작성 기준일 2026년 11월 9일.').runs[0].font.size = Pt(8.5)

    report.page('부록 재현 및 산출물 안내')
    p('본 보고서는 저장된 완료 결과를 읽어 생성했다. 아래 경로는 프로젝트 루트 기준이며, 원자료는 수정하지 않는다. 반복 수, 시드, 시간 한도, 모형과 코드 해시는 manifest에 기록된다. 분석 코드와 문서 작성 코드는 분리되어 있으며 문서 재생성은 실험을 재실행하지 않는다.')
    table('재현에 필요한 산출물', ['경로', '내용'], [
        ('origin_data/', '원본 여행·이용량·재고·대여소 파일'),
        ('outputs/rd1_data_cache/manifest.json', '원본 해시와 자료 감사 요약'),
        ('outputs/rd1_data_cache/window_registry.csv', '연간 전체 창과 적격·분할 상태'),
        ('outputs/rd1_data_cache/date_split.json', '날짜 경계와 문맥 제외'),
        ('outputs/rd1_main/manifest.json', '주 실행 설정과 코드 해시'),
        ('outputs/rd1_main/execution_cohort.csv', '결과 확인 전 고정한 실행 목록'),
        ('outputs/rd1_main/summary.json', '주 조건과 필터 해제 사례별 결과'),
        ('outputs/rd1_main/cases/', '입력·그래프·인코딩·풀이·검증 로그'),
        ('outputs/rd1_sensitivity/', '10개 조건 변경의 독립 입력과 결과'),
        ('outputs/rd1_postanalysis/', '날짜 통계·무작위·사건 민감도·증거'),
        ('outputs/tools/redumis_availability.json', '전문 MIS 비교군 미실행 환경 근거'),
        ('build_rd1_report.py', '완료 산출물만 읽는 문서 생성기')], [8.0,8.6])
    h('실행 계약', 2)
    p(f'실측 장비는 {environment["cpu_name"]}, RAM {environment["installed_ram_gib"]:.3f}GiB이다. 실행 환경은 {manifest["platform"]}, Python {manifest["python"].split()[0]}, HiGHS {", ".join(sorted(solver_versions["highs"])) or "기록 없음"}, OR Tools {", ".join(sorted(solver_versions["cpsat"])) or "기록 없음"}이다. 풀이기 스레드 수는 1로 고정했으며 자료준비 내부 병렬성까지 제한한 것은 아니다. 공유 환경의 운영체제·동기화 등 외부 부하는 격리하지 않았으므로 벽시계는 이 환경의 실측 비용이며 하드웨어 간 우위의 근거가 아니다.')
    p('동일 설정을 재개할 때에는 완료 사례의 결과를 재사용하고 설정·코드 해시가 다르면 새 결과 경로를 사용한다. 후보·제약 또는 정확 그래프를 수정하면 새 사례로 검증해야 한다. 결과 누락을 0으로 바꾸지 않으며, 성공률의 분모에서 실패·한도 초과 사례를 삭제하지 않는다. 이번 결과의 재현 범위에는 인증, 노트북 및 장치 실행이 포함되지 않는다.')
    p(f'주 실행 경과시간은 {cm["elapsed_seconds"]/3600:.2f}시간, 추가 이동 민감도 경과시간은 {cs["elapsed_seconds"]/3600:.2f}시간이었다. 실제 캠페인 비용은 이 전체 경과시간으로 확인한다. 공유 문맥 생성 시간은 각 사례에 보수적으로 부과되므로 사례별 생성 시간을 합산하면 실제 경과시간이 되지 않는다. 사례 총 시간은 공유 문맥을 제외한다. 순수 풀이 시간과 전체 경과시간도 구별한다.')
    p(f'주 실행 설정 SHA256  {manifest["config_hash"]}')
    p(f'최종 로컬 검증에서 테스트 {verification["tests"]["passed"]}개가 통과했다. 실행 시작 시 기록한 핵심 코드 {len(verification["core_code_hash_checks"])}개 해시가 일치했고 주·민감도 실행의 코드 동일성도 확인했다. 검증 기록은 outputs/rd1_verification.json, 테스트 로그는 {verification["tests"]["log"]}에 있다. 이는 저장된 모형·구현의 검증이며 실제 서비스의 인과효과 인증을 뜻하지 않는다.')
    report.save()
    print(json.dumps({'docx':str(OUT),'paragraphs':len(report.doc.paragraphs),'tables':len(report.doc.tables),'main_cases':len(rows),'sensitivity_cases':len(sensitivity)},ensure_ascii=False))


if __name__ == '__main__':
    build()
