# M2 현황 파악용 96판 계획

2026-10-21 KST. 사용자가 본 대국을 96판으로 줄이기로 명시 선택했다. 목표는 세 평가 경로의 현재 성적·CPU 시간·응답 지연을 작은 비용으로 함께 살피는 것이다. 정식 768판 완료나 Elo·세계 순위·모델 승격을 주장하지 않는다. 기존 동결 소스와 진행 중인 attempt003 사전 대국은 수정하지 않는다.

## 고정할 96판

원래 32개시 main 은행의 파일·순서·SHA를 유지하고, 결과와 무관하게 처음 네 family만 선택한다. 세 후보 × 두 상대 × 두 시간(100/1000ms) × 네 family × 양색 = 96판, 48쌍이다. 각 후보·상대·시간 조건은 8판이며 각 lane에서 한 개시 쌍씩을 맡는다. 후보·상대·모델·옵션·한 검색 스레드·Hash16·500ply·무승부 claim·네 lane·선후공·deadline·부하 계측과 원래 시간 상한은 그대로 둔다.

선택 family 순서는 다음과 같다.

1. `m2-formal-bishop-c6-center-20261021`
2. `m2-formal-center-queen-retreat-20261021`
3. `m2-formal-semi-slav-e3-first-20261021`
4. `m2-formal-sicilian-c3-queen-center-20261021`

이 네 family는 세 개의 넓은 상관 cluster에 속한다. 전체 개시를 대표하는 표본으로 보거나 작은 성적 차이를 확정적 우열로 해석하지 않는다. 이미 노출된 고정 은행이며 새 holdout이 아니다. 이전 main40·main194·각 사전 대국을 새 96판 성적에 합산하지 않는다. 후보별·상대별·시간별 승무패와 CPU/응답 지표를 나란히 보고, bootstrap 구간을 산출한다면 선택한 좁은 개시 집합의 기술적 불확실성으로만 표시한다.

## 기존 실행부를 쓰는 최소 adapter

새 계획의 `openings`는 원래 32개 은행이다. `main_selection_policy`가 0-based index 0~3 및 해당 48개 cell ID를 별도로 고정한다. 원래 전체 384쌍은 참조 범위이며 이번 완료 목표는 48쌍/96판이다. freeze 시 원본 load_plan으로 32개 은행을 검증한 뒤 copied main bank에 처음 네 행만 넣는 wrapper를 사용한다. 복사한 네 개시로 main-0001~0048 schedule을 새로 만들고 기존 worker와 verify_frozen이 그 48쌍을 그대로 검증한다. 원래 384쌍의 index/ID를 부분 투영하지 않는다. 원본 v6 실행부·worker·guard·endpoint·CPU 추적 소스 33개와 작은 adapter 한 개를 합쳐 34개를 SHA로 동결한다. adapter는 이 freeze 선택과 아래 사전 검증 인계만 연결한다. 기존 source/frozen files를 재작성하거나 768판 계획의 완료 표시를 바꾸지 않는다.

`scripts/m2_baseline96.py` adapter의 최종 구현 및 동결 manifest는 별도 준비 검증에서 확정한다. attempt003 사전 24판은 이후 실제 완료했고 제품·기존 독립 v6 원시 전체 감사가 모두 PASS했다. 독립 검증은 합법 mainline 2501ply·PV 16171ply·검색 2213회와 CPU 1089.171875초, 감시 scope 2340개, 최종 Job 42개 active 0을 확인했다. 이 사전 성적은 새 main 96판에 합산하지 않는다. 두 감사의 실제 SHA는 새 계획의 인계 ref에 고정했다.

## 진행 중인 사전 24판의 조건부 인계

attempt003은 원래 계획 SHA `3864c05d7ee18c1426279cccb328acf9e07509310a6c4428dd52329959a46781`, frozen-plan SHA `f5f9c005454a224cc7b3e48d2b334cd48eeea4081712fd778819c1b32f00f76c`로 시작했다. 사전 개시는 main 네 개시와 별도의 고정 한 family이며 세 후보·두 상대·두 시간·양색의 24판이다. 인계하려면 다음을 모두 실제 저장 자료로 확인해야 한다.

- 원본 사전 24판 전부 COMPLETE, 오류·미완료 0, 제품 및 기존 독립 v6 전체 감사 PASS.
- 원본 계획과 새 계획의 20개 경기·시간·장비·감시·runtime 조건이 deep equality이고 사전 cell 목록·개시·profile/model/binary SHA도 같음.
- frozen-plan/seal/원래 계획 및 사전 report·raw·제품/독립 감사의 SHA를 인계 receipt에 연결하고 원본은 그대로 보존.
- 실행부 33개 파일의 실제 SHA가 사전 동결 값·새 실행 동결 값과 전부 동일하며 새 adapter는 선택/인계에만 사용됨.
- 인계 receipt는 새 96 계획 SHA와 원래 사전 계획/frozen SHA를 함께 기록. 사전 결과를 새 장소에서 다시 실행한 결과처럼 복사하지 않음.

조건이 하나라도 다르거나 사전 오류가 나면 인계를 허용하지 않는다. PASS한 기존 24판을 재사용하는 근거는 실제 검증 대상 엔진·worker·계측·사전 조건이 동일하기 때문이다. 새 main 개시 수 감소는 사전 기능 검증을 바꾸지 않는다. 별도의 본 대국 검증이나 96판 완료를 사전 PASS로 대신할 수는 없다.

## 완료와 보고

사전 인계 PASS 후 선택된 48쌍/96판을 모두 완료하고 기보/PV/조건/실제 endpoint별 CPU와 응답/Job scope/외부 엔진 CPU 0/정리를 제품·독립 감사로 확인해야 한다. 잔여 부하 20%는 경고만 기록한다. 원래 callable/envelope `요청시간×2+0.25초` deadline 및 실제 identity/CPU/기보 오류는 첫 발생에 중단한다. CPU 사용률·NPS 하한이나 CPU/wall 상한은 만들지 않는다. 실패 자료는 남기며 성적을 보고할 수 있는 부분만 골라 재실행하거나 이전 부분 경기를 합산하지 않는다.
