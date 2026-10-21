# M1 통합 검증 영수증

2026-10-21 KST. 통합 버전 `0.7.0-dev`, 브랜치 `integration/m1-unified-engine`.

두 부모의 코드·자료·모델·이력을 보존하면서 설치 경로와 정수 NNUE 연구를 연결했다. [설계·실행 방법](../../M1_INTEGRATION.md)을 따른다.

| 검증 | 최종 결과 | 근거 |
| --- | --- | --- |
| Windows MSVC Release | 9모음·174그룹·617,469검사, 실패0 | `windows-native-build-and-tests.txt`, `windows-canonical-relink.txt` |
| CMake·CTest 배선 | CMake4.4.4·NMake Release 전체 빌드, CTest9/9 통과 | `cmake/execution.json`, `cmake/ctest.txt` |
| Linux GCC 13.3 ASan/UBSan | 같은 9모음 전부 통과 | `linux-sanitized-build-and-tests.txt` |
| 원본 두 경로와 비교 | 8위치·이력×7조건=56쌍, 시간 외 공통 필드 일치 | `engine-parity.json` |
| UCI·모델 전환 | 잘못된 계열·손상·상호 배타성·공백 경로·builtin 복원23사례 통과 | `engine-parity.json` |
| 기존 Python·대국 도구 | 29개 회귀 실행 전부 통과, 차등 규칙2,997사례 포함 | `preserved-regression-matrix.json`, `preserved-*.txt` |
| 기존 보정망 | V1/V2 native·학습·대국 기능 연결 검사 통과 | `integration-neural_integration_tests.py.txt`, `preserved-learning_runtime_tests.txt` |
| NNUE 학습·QAT·정밀도 | 단위·실제 소규모 학습/내보내기·native 전체/증분 일치 통과 | `python-unit-results.json`, `canonical-python/results.json` |
| 두 모델 계열 공존 | 보정망 V1/V2 실제 학습, NNUE3내보내기×126위치 Python=전체=증분, 오용 거절·모드 왕복 통과 | `canonical-python/coexistence.json` 및 같은 폴더 로그 |
| 검색 자료 | 실제 생성·원시 기록·재현 검증 통과 | `search-data-integration.txt`, `search-data-smoke/` |
| 과거/신규 감사 호환 | 7개 감사 회귀 실행 통과 | `python-audit-compat-results.json` |
| 소스 이력·변조 감지 | 중첩 모듈 포함·추가/삭제·변조·원격 경로5검사 및 관련 회귀 통과 | `source-inventory-test-results.json` |
| 두 의존성 환경 | NumPy2.3.4 및2.3.5에서 선택 회귀·공존 검사 통과 | `canonical-python/`, `numpy235/results.json` |
| Godot·브라우저·기보 | 실제 엔진 backend5·Godot6모음 및 offline 검사 통과 | `ui-report.json`, `ui-final-backend-summary.json`, `ui-final-godot-summary.json` |
| 원본 작업 보존 | 연구2파일·설치14파일 수정/미추적 내용과 HEAD·상태 유지 | `source-preservation-before.json`, `source-preservation-after.json` |
| 과거 실험 보존 | 부모별 보관 경로1,405개/1,589개에 수정·삭제0 | `archive-preservation.json` |

원본 부모는 `3c3d4b4526e1fa0c927347d9974534e4385ea601`과 `0f7783bf8212b86be0fbd74e425c55c4ffc61a06`이다. 모델과 참조 소스의 해시는 `engine-parity.json`, 실제 실행 파일과 코어 소스의 해시는 `build-provenance.json`에 있다.

최종 Windows 실행 파일은 `build/regicide.exe`, SHA256은 `edd950fb673cfca4134968b64cc5ef0f3d77eda698e9f40d2965170f143d6e6a`다. 실행 파일은 빌드 산출물로 로컬에 있으며 저장소 소스로 다시 빌드할 수 있다.

## 발견 후 수정한 문제

- 정수 NNUE 파일 경로의 빈 값이 builtin으로 복원되지 않는 UCI 호환 회귀를 수정했다. 기존 학습·QAT·정밀도 통합 모음을 다시 실행해 통과했다.
- 새로운 응답 순서 보호와 충돌하던 두 Godot 테스트의 오래된 게임/revision 가정을 수정했다. 오래된 응답 거절과 실제 추천·평가·재생 동작을 함께 재검증했다.
- 처음 실패한 로그는 진단 이력이다. 최종 엔진 결과는 `canonical-python/`, `ui-final-*`, `ui-report.json`, `engine-parity.json`으로 구분한다.

## 주장 범위

이번 검증은 기능 보존·정확성·학습 도구 연결을 확인한다. 기력 향상·Elo·세계 순위를 판정한 실험은 아니다. UI 결과는 실제 엔진을 연결한 headless 자동 장면·합성 입력이며 사람이 직접 조작한 수동 GUI 검증은 아니다. GitHub 원격 CI 실행 결과와 로컬 Windows/Linux 실행 결과도 구분한다.
