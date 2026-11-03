"""Render the completed experiment report from preserved audit inputs."""
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
FOLDER = Path(__file__).resolve().parent
def read(name):
    return json.loads((FOLDER / name).read_text(encoding='utf-8-sig'))
audit = read('independent-audit.json')
cost = read('cost-independent-review.json')
validation = read('validation-summary.json')
assert audit['status'] == 'PASS' and cost['passed'] and validation['all_passed']
draft = (FOLDER / 'report-draft.md').read_text(encoding='utf-8')
head = draft.split('## 비계측 고정 깊이 비교')[0]
paragraph = head.split('\n\n')[1]
head = head.replace(paragraph, '**SEE 함수 비용은 줄였지만, 경기력 향상은 두 시간 조건에서 일관되게 확인되지 않았다.** 수정 전 SEE v0.5.2 대비 새 8위치의 100ms는 **5승 3무 8패(40.625%)**, 1초는 **5승 7무 4패(53.125%)**였다. 같은 깊이의 전체 탐색 시간은 작고 혼재된 차이를 보였다. 따라서 v0.5.3을 검증된 경기력 강화나 안정판으로 승격하지 않는다.', 1)
head = head.replace('**TBD**', '`25f73cacafe4a831f0b3b258fbf5b19632c369b2`')
head = head.replace('전체 탐색 비용과 대국 결과는 별도 비교한다.', '전체 탐색 비용과 대국 결과는 별도로 비교했다.')
lines = [head.rstrip(), '', '## 비계측 고정 깊이 비교', '',
         '기존 전체 10위치는 깊이 3·4 각각 5회, SEE 끔 대조는 깊이 4 1회, 손실 개발 5위치는 깊이 4 3회 측정했다. seed 20261020으로 순서를 섞고 역할별 선실행을 교대했다. 각 묶음의 엔진별 워밍업 1회는 기록하고 요약에서 제외했다. 매 요청 전체 실제 이력과 새 대국 신호를 전달했다.', '',
         '**125쌍 모두 선택 수·점수·노드·완료 깊이·선택 깊이·PV·전이표 계측이 같았다.** 모든 요청 깊이를 완료했고 점수/탐색 차이는 0이었다. [독립 비용 감사](../matches/2026-10-18-see-cost/validation/cost-independent-review.md)는 원자료를 다시 계산해 8,919검사를 통과했다.', '',
         '| 자료 | 쌍 수 | 수정/기존 벽시계 시간비 중앙값 | 전체 원시간 합의 비율 | 원자료 |',
         '|---|---:|---:|---:|---|']
labels = {'depth3': '기존 10위치·깊이 3', 'depth4': '기존 10위치·깊이 4',
          'see-off-depth4': 'SEE 끔·깊이 4', 'loss-scenes-depth4': '손실 5위치·깊이 4'}
for row in cost['datasets']:
    timing = row['timing']
    lines.append(f"| {labels[row['name']]} | {row['pairs']} | {timing['candidate_over_baseline_wall']['median']:.6f} | {timing['ratio_of_total_wall_candidate_over_baseline']:.6f} | [JSON](../matches/2026-10-18-see-cost/validation/{row['name']}.json) |")
lines += ['', '1보다 작으면 수정 후 시간이 짧다. 쌍별 중앙값은 깊이 3에서 **1.94% 증가**, 깊이 4에서 **2.34% 감소**, 손실 장면에서 **1.63% 감소**했다. 손실 장면의 전체 원시간 합 감소는 **0.52%**이며 1.63%와 다른 통계다. 깊이 4에서도 Sicilian/French/QGD/pawn endgame 4위치의 중앙값은 느렸다. SEE 끔 대조도 0.84% 느렸고 짧은 요청에는 변동이 커, 이 결과로 일관된 전체 엔진 가속을 확정하지 않는다.', '',
          '비용 묶음의 감시 230행에서 외부 엔진 5개의 CPU 증가는 0이었다. 다만 Git 업로드·자료 읽기 등 비엔진 작업과 일반 호스트 부하까지 격리한 측정은 아니다. 프로파일의 함수 비용 절감과 비계측 전체 엔진의 작은 시간 차이를 구분한다. 결과를 본 뒤 후보를 다시 조정하거나 비용 측정을 재선택하지 않았다.', '',
          '[개발 부분집합](../matches/2026-10-18-see-cost/analysis/development-cost-subset.json)은 16개 분석 위치 중 저완료깊이 상위 3개와 현재 자기 평가가 -200cp 이상인 평가 급락 상위 3개를 합쳐 중복 제거한 5개다. [실행 입력](../matches/2026-10-18-see-cost/validation/development-cost-benchmark.json)은 이 다섯 위치의 전체 실제 이력으로 FEN을 재현한다. 대국 중 서로 다른 위치의 평균 노드/깊이는 이 비교를 대신하지 않는다.', '',
          '## 기존 규칙과 제품 회귀', '',
          '후보 C++ **96그룹·50,531검사**: 규칙 20/8,325, 탐색 43/13,616, 평가 6/400, 보드 10/18,974, 전이표 8/97, SEE 9/9,119를 통과했다. SEE 독립 합법 수 재귀 대조는 125위치·3,967수다. 합법 수 차등 2,997사례와 UCI 19그룹, 대국/비교/벤치마크, 사용자 대국·무르기·핸디캡·기보 회귀도 통과했다. 기준 v0.5.2의 규칙 20그룹·8,325검사 및 차등 2,997사례도 다시 통과했다.', '',
          'Godot 4.7.2 headless의 시점/입력 637·실제 엔진 연결 75·백엔드 종료 3사례·핸디캡 53·기보 78검사를 통과했다. [검증 요약](../matches/2026-10-18-see-cost/validation/validation-summary.json)의 **21개 gate 모두 통과**이며, 기존 Windows 심볼릭 링크 권한 1건만 예상대로 건너뛰었다. 직접 GUI 수동 확인은 수행하지 않았다.', '',
          '외부 CPU 가드 18검사 및 새 감사 자체검사의 감시/원자료 변조·잘못된 버전/옵션·미동결 입력 거부를 통과했다. [동결 실행기 Windows·Ubuntu CI](https://github.com/bigpine-choi/Regicide/actions/runs/39061994294)도 Godot까지 모두 성공했다.', '',
          '## 같은 시간·자원과 새 위치', '',
          'AMD Ryzen AI 7 350(8코어·16논리 CPU), RAM 33,598,976,000바이트, Windows 11 Home 10.0.26200, MSVC 19.51.36260.0 C++20 `/O2 /DNDEBUG`, Python 3.13.16·chess 1.11.2다. 원본 v0.5.2 바이너리를 재사용하고 후보는 소스 42444ed에서 기존 빌드 스크립트로 재빌드했다. 양측 단일 탐색 스레드·Hash 16MiB·Move Overhead 20ms·PVS 켬·**SEE 켬**이며 내부 할당은 80/980ms다.', '',
          '새 시작 위치 8개는 엔진 평가 없이 손으로 작성해 계획 7c2ac87에서 고정했다. 백 차례 4개·흑 차례 4개이며, 이전 JSON 105개·PGN 290판의 348개 고유 시작/기준 위치와 최종 배치가 다름을 확인했다. 새 위치를 후보 선택·튜닝·사전 검색에 사용하지 않았다. 워밍업 없이 첫 전이표 할당을 포함하고, 각 시간 묶음 동안 프로세스를 유지하며 매 수 전체 이력을 전달했다.', '',
          '시작 위치별 선후공을 바꿔 시간당 16판, 총 32판이다. 현재/예정 수의 반복·50수 무승부는 claim, 최대 500반수다. 별도 강제 몰수 시계는 추가하지 않았고 요청/내부 시간 초과를 보존했다. Python은 통신·검증·판정을, 자체 C++ 엔진은 수 선택을 담당했다.', '',
          '## 100ms·1초 대국 결과', '',
          '| 수당 요청 시간 | 수정 후 승·무·패 | 득점률 | 시작 쌍 참고 95% 구간 | 원자료 |',
          '|---|---:|---:|---:|---|']
for match in audit['matches']:
    ms = match['movetime_ms']
    low, high = match['paired_opening_bootstrap95']['interval']
    lines.append(f"| {ms}ms | {match['candidate_wins']}승 {match['candidate_draws']}무 {match['candidate_losses']}패 | {match['candidate_score']:.3%} | {low:.3%} ~ {high:.3%} | [PGN](../matches/2026-10-18-see-cost/attempt-001/time{ms}.pgn) / [JSON](../matches/2026-10-18-see-cost/attempt-001/time{ms}.json) |")
lines += ['', '**100ms는 열세, 1초는 소폭 우세**여서 두 시간 조건의 일관된 경기력 향상은 확인하지 못했다. 두 참고 구간 모두 50%를 포함하거나 닿는다. seed 20261020·20,000회로 선후공 두 판을 묶어 재표집한 8개 고정 시작 쌍의 기술적 구간이며 일반 강도/Elo 구간이 아니다. 이전 PVS 상대 득점과 이번 v0.5.2 상대 득점은 직접 전후 증감으로 비교하거나 합산하지 않는다.', '',
          '32판 모두 정상 종료했고 불법 수·크래시·미완료·미대국은 0이었다. 100ms는 메이트 13/반복 3, 1초는 메이트 9/반복 6/기물 부족 1이었다. `attempt-001`의 최초 정상 두 묶음을 그대로 채택했고 실패 시도·재시도·유리한 결과 선택은 없었다. 임시 절전 방지의 이전 상태 복구도 성공했다.', '',
          '## 외부 부하와 시간 계측', '',
          '모든 빌드·테스트·프로파일과 다른 엔진 계산을 마친 뒤 100ms → 1초 순차 실행했다. 실제 두 UCI transport의 PID·생성시각·부모 계층만 제외하고, 시작 전 10초 유휴 및 약 0.5초 간격 감시를 적용했다. 외부 CPU 1tick 증가·프로세스 신원 변화·2초 초과 조회/간격은 전체 시간 묶음을 무효로 만드는 조건이었다.', '',
          '| 조건 | 감시 행 | 외부 엔진 | 외부 CPU 증가 | 최대 간격/조회 초 |', '|---|---:|---:|---:|---:|']
for match in audit['matches']:
    m = match['monitor']
    lines.append(f"| {match['movetime_ms']}ms | {m['snapshots']:,} | {m['external_processes']} | {m['external_cpu_increase_ticks']} ticks | {m['max_snapshot_gap_seconds']:.6f} / {m['max_snapshot_duration_seconds']:.6f} |")
lines += ['', '합계 **3,133개 감시 행에서 외부 엔진 5개의 CPU 증가는 0**이었다. 이는 알려진 이름의 엔진을 표본으로 관측한 범위다. 미관측 순간 프로세스·다른 앱·열·전력·스케줄러 상태까지 배제하는 전체 PC 독점 증명은 아니다.', '',
          '| 조건 | 역할 | 평균 완료 깊이 | 평균/최대 벽시계 ms | 내부 할당 초과 / 요청 벽시계 초과 |', '|---|---|---:|---:|---:|']
for match in audit['matches']:
    for role in ('baseline', 'candidate'):
        r = match['resources'][role]
        label = 'v0.5.2' if role == 'baseline' else 'v0.5.3'
        lines.append(f"| {match['movetime_ms']}ms | {label} | {r['mean_completed_depth']:.4f} | {r['mean_wall_ms']:.3f} / {r['max_wall_ms']:.3f} | {r['internal_over_allocated_count']} / {r['wall_over_requested_count']} |")
lines += ['', '1초 후보의 내부 보고 최대는 982ms로 할당 980ms를 1회 넘었고, 요청 벽시계 1,000ms 초과는 양쪽 모두 0이었다. 서로 다른 실제 진행 위치에서 얻은 평균 깊이·노드를 같은 위치의 가속 또는 강도 증거로 해석하지 않는다.', '',
          '## 독립 감사·보관·최종 판단', '',
          '[주 감사](../matches/2026-10-18-see-cost/validation/independent-audit.md)는 실제 **3,312반수**, PV **16,971반수**를 재생하고 PGN·최종 FEN·종료 판정·색 배분·득점·버전·옵션·해시·규칙 gate·모든 감시 행·첫 정상 묶음 채택을 대조해 PASS였다. 별도 공통 재생 코드를 사용하지 않은 [두 번째 검토](../matches/2026-10-18-see-cost/validation/second-review.json)도 대조했다. 주 감사 콘솔 로그의 한글은 셸 인코딩 때문에 손상됐으므로 UTF-8 JSON/Markdown 원본을 읽는다. 계산 원자료는 그대로 보존했다.', '',
          '전체 증거는 [보관 폴더](../matches/2026-10-18-see-cost/)와 파일별 `sha256.json`, 원본-보관 경로 `source-map.json`에 있다. 함수 프로파일의 초기 비통제 자료도 별도로 남겼다. 컴파일 중간물·실행 파일은 해시를 기록하고 로컬 빌드 폴더에 보관하며 Git에는 코드·설정·원자료를 저장한다. 개인 사용자 기보는 포함하지 않았다.', '',
          '**판정은 계산 비용의 제한적 개선과 경기력 개선 미확정이다.** SEE의 국소 값과 고정 깊이 탐색 결과는 유지하면서 함수 비용을 줄였지만, 전체 엔진 시간은 혼재됐고 100ms 경기력은 나빴다. 이번 결과를 보고 계수·후보·위치·판수를 바꾸거나 재대국하지 않았다. v0.5.3은 개발 후보로 보존하고 안정판 설치를 승격하지 않는다.', '',
          '다음 우선 분석 대상은 안정화 탐색의 체크 회피 수 정렬과 조용한 위협이 실제 패배에 미치는 영향이다. 92.19%라는 노드 비중만으로 해당 노드를 없애도 된다고 결론 내리지는 않는다. 이 표본은 사용 이후 개발 자료이며 다음 개선의 독립 확인 자료로 재사용하지 않는다.', '',
          '재현에는 원래 바이너리·환경 및 동결 명세를 확인하고 원자료 경로를 복원한다. 다른 PC에서 다시 빌드하면 실행 파일 해시가 달라질 수 있으므로 새 바이너리 해시와 환경을 별도 계획으로 고정한다. 기존 증거를 덮어쓰지 않는다.', '',
          '```powershell',
          './scripts/run_see_cost_comparison.ps1 -CandidateCommit 42444edc3ff1de358b97ac4d250b43bf342d1df0 -HarnessCommit 25f73cacafe4a831f0b3b258fbf5b19632c369b2 -ValidationEvidence build/see-cost-evidence/validation-summary.json -Attempt attempt-001',
          './.venv-tests/Scripts/python.exe tests/audit_see_cost_results.py build/see-cost-comparison/attempt-001 --harness-commit 25f73cacafe4a831f0b3b258fbf5b19632c369b2 --validation-summary build/see-cost-evidence/validation-summary.json --output build/see-cost-evidence/new-independent-audit.json',
          '```', '']
report = '\n'.join(lines)
assert 'TBD' not in report and '초안 상태' not in report
path = ROOT / 'docs/experiments/2026-10-18-see-cost.md'
assert not path.exists(), 'Preserve an already-written report'
path.write_text(report, encoding='utf-8')
print(f'Wrote {path.relative_to(ROOT)} ({len(report.splitlines())} lines)')
