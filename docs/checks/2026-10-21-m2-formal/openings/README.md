# M2 정식 개시 은행 — 2026-10-21

정식 32개 부모 가지와 별도 사전 점검 2개를 자체 작성했다. 엔진 평가·탐색·대국 결과·다운로드 기보·오프닝 안내 데이터는 후보 선정에 사용하지 않았다. 각 최종 위치를 엔진 선후공을 바꿔 두 번 사용한다. 정식 은행은 백 차례 16개/흑 차례 16개, 12~16ply다. 사전 점검은 두 개 모두 12ply이며 **첫 번째 한 개만** 24판 점검에 사용한다. 두 번째를 실패 후 대체 표본으로 사용하지 않는다.

## 고정 파일

| 파일 | SHA256 |
|---|---|
| `tests/data/m2_formal_openings_2026-10-21.json` | `1994d67709942de642b0e577ded78e382195df260b806117edc8aa774e454edd` |
| `tests/data/m2_preflight_openings_2026-10-21.json` | `1e6b08b689701e95c34adc8809cf1a104d12413c8ad02c02461de1ef2a6817b8` |
| `exposure-index.json.gz` | `2d7d669485b26d69ce51ddc2060efc8e160c9d2b22ef530a18f6f5ff97fb44cb` |

기존 `compare_engines.load_openings`와 호환되는 JSON이다. `family_id`는 8ply 부모 가지, `cluster_id`는 더 큰 구조 묶음이다. `moves`가 실제 시작 수순이며 SAN·최종 FEN·부모 FEN·plies·입력 SHA를 함께 보존한다. 생성 순서의 고정 seed는 `2026102102`다. 모든 수순은 선언한 뒤 합법성과 노출 배제로만 검수했다. 샘플 수를 채우려고 필터를 완화하거나 관측 성적을 보고 고르지 않았다.

## 배제 기준과 검사 범위

- 8ply 이후의 기존 선언 수순 및 부모 위치와 새 수순의 전치를 배제한다. 규칙 상태(차례·캐슬링·합법 앙파상)를 포함하고 수 카운터를 제외하며, 색 교환+랭크 반전도 같은 것으로 본다.
- 새 최종 위치는 차례·캐슬링·앙파상·카운터를 모두 제외한 기물 입력으로도 과거 노출과 비교한다. 명시적으로 보존된 기존 `feature_hash`/`parent_feature_hash`도 대조한다.
- 새 정식 32개와 사전 점검 2개 사이에서도 8ply 이후 경로의 전치 및 최종 기물 입력 중복을 배제한다.
- 8ply 미만의 흔한 공통 수순은 허용한다. 과거의 1~3수짜리 이름을 전체 오프닝 부류의 영구 배제로 해석하지 않는다. 그 상관은 큰 `cluster_id`로 드러낸다.

인벤토리 **1,006개 파일**의 경로·바이트 SHA·추출 수를 `exclusion-audit.json`에 보존했다. 비교·heldout·LMR·PVS·SEE 2개·neural strength/long-time·M2 pilot의 기존 9개 은행, 학습용 2개 은행, A/B attempt-001/002의 부모·sources·train/validation/test/query 행, 자체 탐색 seed, 과거 연구·대국 JSON/JSONL을 포함한다. 학습 arena의 고정 seed 64개도 기존 규칙대로 12ply로 펼쳐 포함했다. 검사 결과 과거 기물 입력 315,805개, 규칙 상태 318,838개, 8ply 이후 선언 부모 상태 29,501개, 명시 feature SHA 33,188개에 대해 새 최종/부모 충돌은 0개다.

과거 자료의 모든 PV 노출을 완전히 복원했다는 뜻은 아니다. 추가 재생 기록에는 **527개 한계**가 남아 있다: 로컬/상위 root FEN을 찾지 못한 PV 204개, 선언 형식과 추출기 관례가 맞지 않거나 재생이 막힌 기록 322개, 실제로는 일반 텍스트인 `.json` 테스트 영수증 1개다. 이를 원본 자료의 오류 527개로 해석하지 않는다. 해당 FEN과 합법적으로 확인한 prefix는 보수적으로 배제 집합에 남기며, 세부 위치와 이유를 audit에 공개한다. 필수 9개 은행·학습 은행·A/B 부모는 캐시를 사용하지 않는 별도 직접 재생 검사도 통과했다. PGN만 있는 궤적, 저장하지 않은 UCI 중간 info PV, 누락된 원본 데이터, 저장하지 않은 사용자 대국과 외부 체스 이론은 범위 밖이다.

처음의 French advance 후보는 과거 SEE replication 수순과 10ply에서 전치돼 제외했다. 단순 이름·수순 순서 변경 대신 중앙 폰과 나이트가 교환된 French Rubinstein 구조로 교체했다. 제외한 원래 수순과 이유는 audit에 남겼다. 추출기 준비 중 발견한 중첩 `result.pv`의 상위 root FEN 호환도 동결 전에 보강했다. 이때 기존 배제 집합을 유지하고 실제 PV 입력을 더했으므로 배제가 약해지지 않았다. 중간 실행 로그도 그대로 보존한다.

## 표본 해석

정식 32가족은 서로 완전히 독립인 무작위 표본이 아니다. 큰 구조는 7개이며 크기가 불균형하다: open-e4-e5 11, Sicilian 4, French 3, Caro-Kann 3, other-e4-defense 4, queen-pawn-d5 6, Indian-e6 1. 이 큰 묶음으로 선후공·가족을 함께 유지한 cluster bootstrap을 보고하더라도, 7개 자체 선정 묶음에 대한 서술적 불확실성이다. 보편적 Elo나 전체 체스 모집단의 신뢰구간으로 해석하지 않는다. 이름은 구조 설명이며 최선수·인기도·균형 평가를 보증하지 않는다.

## 실제 검증

```powershell
.\.venv-tests\Scripts\python.exe scripts/prepare_m2_formal_openings.py
.\.venv-tests\Scripts\python.exe tests/m2_formal_openings_tests.py --report docs/checks/2026-10-21-m2-formal/openings/test-report-final.json
```

최종 `tests-final.txt`와 `test-report-final.json`: **15개 검사 PASS**, 실패/오류 0, 2.310초. 전체 합법 재생·SAN/FEN·비종료/비체크·최종 입력/부모 전치·색/랭크 반전·원본 SHA·기존 은행/A·B 부모 직접 재생·arena seed·추출 캐시의 긴/짧은 공통 prefix 순서 불변·기존 대국 loader·고정 순서 재생성을 검사했다. 발견한 호환 문제에 대한 중첩 PV root 상속과 FEN 없이 feature SHA만 남은 입력의 배제 검사도 포함한다. 처음의 13개 PASS는 `tests.txt`와 `test-report.json`에 별도로 보존했다. 실제 엔진 질의와 대국은 **0회**다. 생성기는 이미 존재하는 은행/audit를 덮어쓰지 않는다. 재현은 새 출력 경로를 지정하고 보존된 `--exposure-index`를 읽으면 된다. 캐시는 매 원본 파일의 SHA가 일치해야 읽힌다.

최종 생성은 `preparation-final.txt`, 준비 단계의 형식 진단/시간이 오래 걸려 중단한 추출 로그/프랑스 전치 거절은 `preparation-first.txt`, `preparation-second.txt`, `preparation-third.txt`다. 중단한 작업은 이 은행 추출용 Python helper뿐이며, 사용자 앱·공유 엔진·원본/설치 checkout을 변경하거나 종료하지 않았다.
