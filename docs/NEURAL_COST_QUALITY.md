# 신경망 비용·판단 분리와 정답 품질

v0.6.3은 기존 평가·출력0 V2·학습V2 진단과 자체 교사의 정답 안정성 확인을 추가한다. 기존 체스 규칙·탐색·평가 수식·100cp보정한도·모델 채택 기준은 유지한다. [사전 계획](experiments/2026-10-19-neural-cost-quality-plan.md)을 참고한다.

## 세 가지 평가를 비교하는 이유

- 기존 평가: 현재 수식 점수만 계산한다.
- 출력0 V2: 학습망의 숨은 가중치는 그대로 읽고 마지막 출력층만0으로 만든다. 신경망 연산 비용은 발생하지만 점수는 기존 평가와 같다.
- 학습V2: 같은 구조의 실제 학습 가중치를 사용한다.

같은 위치·깊이/노드에서 기존평가와0모델의 수·점수·PV·노드·TT작업이 같아야 계산 비용을 분리할 수 있다. 학습V2의 같은 노드 비교는 보정이 탐색 판단을 바꾸는 효과를 보여 준다. 같은 시간 비교에는 비용과 탐색 경로 변화가 함께 반영된다. 수가 바뀌었다는 사실만으로 악수라고 하지 않는다.

자체 교사를100,000/200,000노드로 검색한 뒤 선택된 수들의 합집합을 공통 완료깊이에서 강제 탐색한다. 같은 완료깊이의 정확 유한 점수를 얻지 못하면 수 순위·후회값은 unavailable이다. 이 수치는 자체 유한 교사의 개발용 판단이며 참정답·Elo·모델 채택 근거가 아니다.

```powershell
$sourceCommit = git rev-parse HEAD
.\.venv-tests\Scripts\python.exe scripts/diagnose_neural_cost.py build/regicide.exe --model PATH_TO_V2.nn --source-commit $sourceCommit --output build/neural-cost-diagnostic --depth 3 --nodes 10000 --movetime-ms 100 --repeats 6 --exclude-manifest PATH_TO_CORPUS_MANIFEST.json
```

출력은 새 폴더여야 한다. `--exclude-manifest`는 반복해 원본 수집/누적/보류 자료를 보호할 수 있다. 기본 개발 위치12개는 학습 오프닝과 예약 시작 위치의 같은 보드·신경망 입력을 거부한다. 실제 제외 파일·해시를 기록하며 임의의 아직 알려지지 않은 후속 위치 전체와 중복이 없다는 증명은 아니다.

시간 비교는 알려진 브라우저·외부 엔진 CPU, 측정 프로세스와 감시 공백을 검사한다. 부하가 감지되면 유효한 시간 비교로 제시하지 않는다. 임의의 다른 부하·온도·짧은 프로세스까지 완전히 통제하지는 못한다. 실패한 원자료도 보존한다.

## 정답 안정성 확인

자동 순환은 누적train의 각 위치를 자체 수식 엔진으로10,000→50,000→200,000노드 재탐색한다. 단계마다 전체 이력을 보내고 새 논리 대국·빈 TT를 사용한다. 시간 제한은 없다.

마지막 두 검색 모두 정확한 유한CP·완료깊이3이상·같은 최선수·점수차30cp이하이면 마지막 라벨을 사용한다. 메이트·bound·누락을 임의CP로 바꾸지 않는다. 모든 단계의 검색·원래 라벨·선택/제외 이유를 보존한다. 더 오래 계산한 결과의 일치가 참정답을 뜻하지는 않는다.

```powershell
.\.venv-tests\Scripts\python.exe scripts/teacher_quality.py --dataset PATH_TO_CORPUS_MANIFEST.json --binary build/regicide.exe --output build/teacher-quality
```

원본 self-play·패배분석·corpus는 보존하고 새 `quality/`dataset을 만든다. validation/test 전체 JSONL 행과 게임/PGN 바이트는 그대로 복사한다. 기존 검증 라벨은 얕은 교사의 점수이므로 그 오차만으로 정답 품질 향상을 증명하지 않는다. train이 전부 탈락하면 훈련·채택을 중단한다.

자동 순환은 품질단계를 필수로 거쳐 `quality/manifest.json`을 학습한다. 기본 계산량은 train위치당260,000노드이며 완료된 누적train을 매번 재점검하므로 기존보다 오래 걸릴 수 있다. 캐시·신경망 증분 계산·정수화는 이번 범위에 포함하지 않는다. `--quality-nodes`, `--quality-max-cp-delta`, `--quality-min-depth`로 사전 설정을 기록할 수 있다. 결과를 본 뒤 같은 시도의 설정을 다시 고르지 않는다.

## 그대로 유지하는 검증

예약 아레나 자료는 학습에 넣지 않는다. 기존 영속시도번호·bootstrap 검증/시험·최소32쌍·수당100ms·선후공 교대·자원감시·시도별alpha/점수하한 기준을 유지한다. 파생 학습자료도 동결해 변조되면 채택을 차단한다. [자동 학습 설명](CONTINUAL_LEARNING.md)을 따른다. 규칙 정확성·비용·교사 일치·대국 기력은 구분해 보고한다.
