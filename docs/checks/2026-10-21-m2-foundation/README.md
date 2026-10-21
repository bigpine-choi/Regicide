# M1 마감과 M2 공통 평가 기반 — 2026-10-21

M1의 로컬 통합을 다시 감사하고, 두 개발 경로의 성과를 같은 조건에서 비교할 수 있는 **세 평가 프로필·입력 동결·외부 대국 파일럿**을 구현했다. 실제 수를 선택하는 C++ Regicide 코어와 기본 수식 평가는 바꾸지 않았다. 이 기록은 경기력 향상이나 신경망 승격을 선언하지 않는다.

## M1에서 남아 있던 것

엔진·모델 통합 기능은 완료돼 있었다. 17개 native 파일과 현재 실행 파일이 M1 최종 영수증과 같고, 별도 세 프로필 검사에서 그 소스가 `de79979`의 Git 내용과 같은 것도 확인했다. 과거 NNUE 미완료 메모를 현재 상태로 재사용하지 않았다.

남은 정리는 ROADMAP의 ‘아직 통합 전’·‘M1 이후는 계획’ 문구와 기준판/원격 반영 대상의 명시였다. 이를 [M1 문서](../../M1_INTEGRATION.md#로컬-통합-완료와-배포-경계)와 [로드맵](../../ROADMAP.md)에 반영했다. `ENGINE.md`의 오래된 `searchmoves` 미지원 설명도 현재 구현과 맞췄다. 원격 main은 읽기 조회에서 여전히 `e3b0902a3e9203f63725f19eda562af6cce2d5fe`이며, 원격 PR·CI·배포는 실행하지 않았다.

## 두 경로를 살리는 공통 기준

| 평가 프로필 | 고정한 모델·상태 |
| --- | --- |
| 수식 평가 | 통합판 기본. 폰 방패·폰 구조·LMR·PVS·SEE 등의 현재 옵션 명시 |
| Residual V2 | M1에서 사용한 quality attempt4 모델, SHA `5b363fa05487d7707d943c26c8a8343c86122315ecd61f88003692ff449291b3`. 기존 경로의 수식 평가 보정. 과거 미채택 이력 유지 |
| NNUE S32 | M1에서 사용한 S32 QAT 선택 모델, SHA `c9ca5f35bb0ef0f61fca9b7eba10cd01d4320cf6c2a1dd4863630b7f87baaa92`. 정수·증분 경로. 과거 강도 미승격 이력 유지 |

세 모드는 같은 `0.7.0-dev` 실행 파일 SHA `edd950fb673cfca4134968b64cc5ef0f3d77eda698e9f40d2965170f143d6e6a`, Hash16·단일 탐색 스레드·같은 통합 탐색 옵션을 사용한다. NNUE의 과거 pawn/LMR 비활성 실험을 그대로 재현하는 프로필은 아니다. 현재 통합 탐색에 연결한 공통 비교 조건이다.

프로필은 `config/engine-profiles/{classical,residual-v2,nnue-s32}.json`이다. 모델은 결과를 보고 새로 고르지 않고 M1에서 실제 비교한 파일을 이어 썼다. 각 모델의 학습 출처·선택 영수증도 아티팩트로 묶었다. 설치 경로의 자동 학습은 보정망을, 정수 연구 경로는 별도 NNUE 도구를 계속 사용한다. 모델을 합산하거나 서로의 형식으로 변환하지 않는다.

새 `engine_profile.py`는 실행 파일·모델·선언한 소스/영수증의 해시와 모델/옵션 결합을 검사하고 새 폴더로 복사한다. 모드 모순·계열 오류·변조·경로 충돌을 거절한다. 독립 리뷰에서 찾은 옵션 대소문자 우회도 수정했다. `Hash`와 `hash` 같은 중복, Regicide의 비정규 표기 별칭을 거절한다.

새 `external_baseline_matrix.py`는 기존 대국 도구를 재사용하며, 결과 전에 프로필·모델·개시군·시간·판수·순서·중단 상한을 동결한다. 셀마다 조건과 파일 해시를 다시 확인한다. PGN·JSON의 모든 수와 최종 배치·종료·점수를 재생하고, 빈 결과나 증거 없는 완료를 거절한다. 시간 상한 때는 자신이 만든 작업 프로세스 트리만 정리하며 기존 게임을 종료하지 않는다.

## 실행 조건과 실제 결과

실행 조건은 첫 결과 전에 [계획 JSON](../../experiments/2026-10-21-m2-pilot-plan.json)으로 고정했다. 설명문과 결과 정리는 이후 작성했다. 계획은 **세 평가 × 두 외부 상대 × 50/200ms × 두 개시 계열 × 양색 = 48판**, 12셀이다. 각 셀은 4판이다. 후보 순서를 블록마다 회전하며 첫 미완료 셀에서 중단한다. 최대360반수는 미완료 `*` 처리이며 무승부가 아니다. 셀 상한600초·전체 상한1800초다.

외부 상대는 공식 고정 [Stockfish18](https://github.com/official-stockfish/Stockfish/releases/tag/sf_18)과 [Sunfish e396948](https://github.com/thomasahle/sunfish/tree/e396948acbd80cdcbad3dccfb07e878fba9df8f4)이다. Stockfish는 generic Windows x86-64·Threads1·Hash16·Skill20·강도 제한 꺼짐·테이블베이스 경로 비움이다. Sunfish는 고정된 원본3파일과 CPython3.13.16에서 Hash16으로 실행한다. 양쪽의 Hash16이 전체 프로세스 메모리가 정확히 같다는 뜻은 아니다. interpreter 및 런타임 설정도 기록하지만 Python 기본 설치·OS 전체를 봉인하지 않는다.

공식 배포와 원저자 저장소에서 각각 [Stockfish GPL3+](https://github.com/official-stockfish/Stockfish/blob/cb3d4ee9b47d0c5aae855b12379378ea1439675c/Copying.txt), [Sunfish GPL3](https://github.com/thomasahle/sunfish/blob/e396948acbd80cdcbad3dccfb07e878fba9df8f4/LICENSE.md)를 확인했다. 이들은 외부 비교 상대이며 자체 엔진의 수 선택 구현에 편입하지 않았다. 내려받은 파일·원본 소스·라이선스는 ignored `build-m2-opponents/`와 동결 실행 폴더에 보존한다. 대용량 실행 파일을 Git에 넣지 않았다. [출처와 원본 해시](opponents/provenance.json), [외부 UCI 스모크](opponents/profile-smoke.json)를 참고한다.

**48/48판 완료**, 12/12셀 정상 종료. 미완료·오류·미실행·시간 상한 중단은 0건이다. 전체 경과 시간은 439.73초였다. 아래는 각 조건별 4판의 Regicide 관점 **승/무/패**다.

| 평가 | Stockfish 50ms | Stockfish 200ms | Sunfish 50ms | Sunfish 200ms |
| --- | --- | --- | --- | --- |
| 수식 평가 | 0/1/3 | 0/1/3 | 3/1/0 | 3/1/0 |
| 보정망 V2 | 0/0/4 | 0/0/4 | 1/3/0 | 3/0/1 |
| 정수 NNUE S32 | 0/0/4 | 0/0/4 | 2/1/1 | 4/0/0 |

작은 고정 표본에서 수식 평가를 교체할 근거는 얻지 못했다. Stockfish에는 세 경로 모두 승리가 없었다. Sunfish 성적도 시간·평가별로 따로 읽어야 하며 두 상대·시간의 점수를 합쳐 일반적인 모델 순위로 만들지 않는다.

원시 [기보·JSON·로그](pilot-001/cells/), [결과 요약](pilot-summary.json), [동결 명세](pilot-001/frozen-plan.json), [실행 상태](pilot-001/report.json)를 보존했다. 조건별 Wilson/색 교환 쌍 bootstrap은 요약 JSON에 **기술적·서술적 범위**로 남긴다. 두 고정 계열의 퇴화된 bootstrap 구간이나 무승부를 반승으로 센 Wilson 구간을 일반 기력의 95% 신뢰구간으로 사용하지 않는다. 독립 감사는 [별도 결과](peer-audit.json)를 따른다.

## 검증 기록

| 검증 | 결과·근거 |
| --- | --- |
| 프로필 단위 | 최종15/15 PASS. [최종 영수증](engine-profile-unit-attempt2.json), 초기13개 통과 기록도 보존 |
| 프로필 실제 연결 | 최종3모드×5위치 실제 검색15건, NNUE 정적 전체/증분5위치·검색5쌍 일치. 계열/동시 활성 거절 뒤 정상 상태 보존. [최종 기록](profile-validation/attempt-002/results.json) |
| 행렬 단위 | 20/20 PASS. 완료 오판·변조·조건 오용·중단/시간 상한·미실행 집계. [기록](matrix-unit/report.json). 실제 엔진 실행과 프로세스 정리는 mock으로 구분 |
| 기존 외부 대국 도구 | 실제 Regicide 실행 파일을 전달한 기존 외부 연결 검사4/4 PASS. [실행 기록](external-baseline-regression.json) |
| 전체 파일럿 재감사 | [제품 감사](product-audit.json)12셀 PASS. 별도 구현 감사48판·본수4,258반수·PV22,134반수·실제 검색3,682반수 PASS. [독립 결과](peer-audit.json), [실행 영수증](peer-audit-execution.json) |

행렬 단위 검사는 기보·결과·설정 감사이며 검색 PV의 독립 합법성 감사까지 포함한다고 주장하지 않는다. 실제 프로필 검색은 별도로 PV/합법 수를 검사한다. M1의 전체 native 회귀는 소스와 영수증 일치를 재확인했으며 이번 Python 도구 변경을 위해 617,469개 검사를 새로 전부 실행한 것으로 표현하지 않는다.

신규 단위 두 모음을 기존 Windows/Ubuntu CI 절차에 연결했다. 로컬 결과와 원격 CI 결과를 혼동하지 않는다. 실제 고정 프로필은 이 빌드/경로에 묶여 있으며 `tests/engine_profile_integration.py --profiles-dir ...`은 해당 프로필을 준비한 환경에서 실행한다.

## 재현과 보존

외부 준비에 실제 사용한 [다운로드·검사 스크립트](opponents/preparation/prepare.py)와 [프로필·스모크 스크립트](opponents/preparation/profiles.py)를 원문 보관했다. 새 환경에서는 이 두 파일을 먼저 저장소의 **`build-m2-opponents/prepare.py`, `build-m2-opponents/profiles.py`**에 복사해 실행한다. 문서 보관 위치에서 직접 실행하면 루트 기준이 달라지므로 사용하지 않는다. 기존 산출물과 섞지 않고 새 체크아웃/출력에서 재현한다. 준비 스크립트는 Windows x86-64와 준비된 `.venv-tests`를 사용하며 pip로 Sunfish 의존성을 설치하지 않는다.

현재 고정 프로필의 Regicide 바이너리 해시와 다르게 새로 빌드했다면 새 빌드 영수증/모델/실행 파일 해시로 별도 프로필과 계획을 만든다. 이전 검사를 우회하지 않는다. 동결 폴더는 이 PC의 경로를 기록한 실행 영수증이며 다른 PC로 옮겨 절대 경로만 조용히 바꾸는 방식은 지원하지 않는다.

```powershell
.venv-tests/Scripts/python.exe tests/engine_profile_tests.py
.venv-tests/Scripts/python.exe tests/external_baseline_matrix_tests.py
.venv-tests/Scripts/python.exe tests/engine_profile_integration.py --output build/m2-profile-check
.venv-tests/Scripts/python.exe scripts/external_baseline_matrix.py freeze --plan docs/experiments/2026-10-21-m2-pilot-plan.json --output build-m2-matrix/new-pilot
.venv-tests/Scripts/python.exe scripts/external_baseline_matrix.py run --directory build-m2-matrix/new-pilot
.venv-tests/Scripts/python.exe scripts/external_baseline_matrix.py audit --directory build-m2-matrix/new-pilot
```

실제 실행 폴더는 `build-m2-matrix/pilot-001/`이다. 실행·모델·선언 아티팩트 복사본과 사전 동결·원시 파일을 이 위치에 유지한다. 문서에는 대용량 바이너리 대신 계획·해시·원시 기보/JSON·관측·감사를 남긴다. 연구·설치 원본의 기존 변경과 개인 기보는 건드리지 않는다. 바탕화면 게임의 실행 파일도 교체하지 않는다.

[최종 파일 해시 목록](artifact-integrity.json)은 이 증거 폴더와 이번 프로필·실행 도구·검사·로드맵의 원문 바이트를 연결한다. 목록 파일 자체는 제외하며, 로컬 Git 커밋이 최종 보관본을 식별한다. 원본 체크아웃은 [전](original-checkouts-before.json)·[후](original-checkouts-after.json), 통합 native/실행 파일은 [보존 검사](native-preservation.json)를 따른다.

## 다음 판단

**M2 전체 완료는 아직 아니다.** 이 파일럿은 두 계열에서 만든 모델을 공통 엔진과 실제 외부 상대에 연결한 첫 측정이다. 공유 PC·짧은 시간·두 가족의 작은 표본으로 모델 순위를 확정하거나 Elo를 추정하지 않는다. 요청한 movetime은 전체 대국 시계 심판이 아니다. 관측기는 알려진 외부 엔진 CPU를 약1초 간격으로 표집하지만 다른 프로그램 부하·전력·열·샘플 사이 프로세스를 통제하지 않는다. Windows 이외의 관측은 지원하지 않는다.

실제로 437개 관측 표본에서 알려진 비소유 엔진17개의 신원과 양의 CPU 증가가 확인됐다. 열린 사용자 게임도 연산했다. 최대 표본 간격은 약1.023초이며 관측 오류는0건이다. 따라서 격리 조건은 충족하지 않았다. 이 사실은 [독립 감사의 CPU 관측](peer-audit.json)에 별도로 남기며, 대국 기록의 일치와 기력 비교의 유효성을 구분한다. 실행 종료 뒤 파일럿 경로를 사용한 프로세스가0개인 것도 [확인](process-cleanup.json)했다.

정식 M2는 별도 개시군·100ms/1초·충분한 색 교환 쌍·통제된 실행 환경·오류/미완료 및 계열 단위 불확실성을 사전에 정해 실행해야 한다. 이번 두 계열은 다음 확인 실험의 새 보류 자료로 사용하지 않는다.

M3에서는 기존 10/21 진단의 **같은 망·같은 자료에서 MSE/Huber 손실만 바꾸는 실험**을 우선 가설로 유지한다. 손번 입력의 표현 제약은 별도 요인이다. 손실·손번·망 크기를 동시에 바꾸어 효과를 혼동하지 않으며, 실수 보정망에 정수망의 가설을 자동 적용하지 않는다. 어느 경로든 지금 만든 동결 프로필과 같은 외부 기준으로 새 후보를 평가한 뒤 기본값 승격을 판단한다.
