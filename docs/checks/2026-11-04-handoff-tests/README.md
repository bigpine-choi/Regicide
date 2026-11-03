# 2026-11-04 메인 반영 전 통합 검사

오늘 변경된 화면·호스트·자료 연결을 최종 소스에서 점검했다. 결과와 소스 SHA256은 [results.json](results.json)에 보존했다. 이 기록은 원격 GitHub Actions 완료나 경기력 측정의 증거가 아니다.

| 범위 | 수행 결과 |
| --- | --- |
| 브라우저 동작 | 13개 묶음 전부 통과 |
| 웹 JavaScript 구문 | 7개 파일 전부 통과 |
| Python 분석·코칭·오프닝·기보·호스트 | 고유 이름 검사 320개 중 309개 통과, 선택적 검사 11개 제외, 최종 실패 0 |
| 추가 안전·품질·시간·반복 CI 경계 | 12개 묶음, 210개 통과·제외 0 |
| 새 CI 갬빗 묶음 명령 | 99개 통과; 부모 작업의 191개 검사와 중복되므로 별도 고유 표본으로 더하지 않음 |
| 실제 엔진 데스크톱 시작 | 3개 통과; 비공개 시작 파일 인증과 표준 출력의 인증 값 비노출 확인 |
| 실제 엔진 코칭 HTTP | 인증된 로컬 요청·기보 분석·중단·재시도 및 원 대국/파일 불변 확인 |
| CI 구성 | YAML 파싱, Linux/Windows 행렬과 push/PR 트리거, 203개 run 단계의 파일 경로 확인 |

Python의 제외 11개는 이 단위 실행에서 실제 엔진 인자를 받지 않은 선택적 검사 및 심볼릭 링크 권한 조건이다. 데스크톱 시작은 이후 실제 실행 파일로 별도 수행해 3개 모두 통과했다. 분석 화면의 실제 Stockfish/Native 격리와 Godot 836개 화면 검사는 각각 기존 [공통 게이지](../2026-11-04-stockfish-gauge/README.md), [공통 UI](../2026-11-04-shared-game-ui/README.md) 기록에 있으며 이 표에 다시 더하지 않는다. 최종 운영 정책의 실제 분기 검증은 [희생 후 운영](../2026-11-04-gambit-operations/README.md)에 별도로 보존한다.

처음 실행한 PGN 미리보기 검사 하나는 이전 표시명 `Ruy Lopez`를 기대했다. 현재 `opening_names.py`의 명시적인 표시명 변환과 오프닝 안내·진영별 표시 검사는 `Ruy Lopez Opening`을 요구하고 있었다. 제품 동작을 바꾸지 않고 이 낡은 기대값을 맞춘 후 해당 16개를 다시 실행해 전부 통과했다. 처음 실패와 수정 후 원본을 [python-output.txt](python-output.txt)에 함께 남겼다.

원본 기록: [브라우저](browser-output.txt), [Python](python-output.txt), [추가 CI 경계](added-ci-output.txt), [실제 시작](native-bootstrap-output.txt), [실제 코칭](native-coaching-evidence.json). 실제 엔진은 SHA256 `5011da4c5e6ef01c452bd7125e4e22b372d2c9cfb858ed8759057325acb0e6ba`이며 임시 기보와 해당 검증이 생성한 프로세스만 사용했다.

CI에는 새 갬빗 지식·운영·기억, 공통 게이지와 코칭 참고, 반복 판정, 공통 시간 및 스파링 재료·안전·품질 검사를 추가했다. 기존의 Release 빌드·ctest·Linux/Windows 규칙 교차 비교·네이티브 프로토콜·NNUE와 Windows Godot 검사는 유지한다. 원격 실행의 실제 완료 상태는 GitHub에서 별도로 확인해야 한다. 이 로컬 검사에서는 C++가 바뀌지 않아 새 컴파일을 반복하지 않았다.
