# 로컬 Stockfish 심판을 사용하는 스파링

스파링의 수와 양보 후보는 기존 Regicide가 만듭니다. 별도 Stockfish는 현재 형세를 평가하고, Regicide가 제안한 양보를 허용할지 확인합니다. C++ 자체 엔진·평가 함수·학습 모델은 바꾸지 않았습니다. 이 모드는 **Stockfish 보조 스파링**이며, 독립 Regicide의 경기력 측정과 구분합니다.

## 사용하기

현재 개발 PC에서는 기존 공식 Stockfish 18 패키지를 자동으로 찾습니다. 실행 중인 대국은 그대로 두고, 다음에 Regicide를 다시 열어 **새 대국 설정 → 스파링 · Stockfish 심판**을 선택합니다. 이번 심판 기능에는 인터넷 연결·리체스 로그인·토큰이 필요하지 않습니다.

형세 막대의 **Stockfish 심판** 표시는 현재 위치에 대한 심판 분석이 완료됐다는 뜻입니다. `+`는 백, `−`는 흑 기준이고, 자세한 설명에 심판의 깊이와 **Regicide 자체 점수**를 함께 표시합니다. 준비 중·실패·다른 위치의 응답이면 자체 평가와 이유를 표시합니다. 형세 값은 사람의 실제 승률이나 승패 보장이 아닙니다. 분석·코칭·추천 수는 기존 Regicide를 사용합니다.

설치 파일을 찾지 못하면 **스파링 · 자체 평가**로 표시하고 기존 자체 스파링을 사용합니다. 이미 연결된 심판의 후보 검사가 실패하면 해당 수는 Regicide의 원래 기준수로 복귀합니다. 심판이 직접 찾은 다른 수를 대신 두지 않습니다.

## 후보를 확인하는 방법

예를 들어 Regicide가 원래 수 A와 양보 후보 B를 만들면, Stockfish는 **A와 B만** 허용한 같은 탐색에서 같은 완료 깊이의 두 평가를 비교합니다. B가 기준 범위에 들면 B를 둔 뒤의 위치에서 상대의 제한 없는 반격도 다시 확인합니다. 두 검사를 통과해야 B를 허용합니다. Stockfish의 자유 탐색 결과를 실제 착수로 사용하지 않습니다.

평가의 단위와 기준이 엔진마다 다르므로 Regicide 점수에서 Stockfish 점수를 빼지 않습니다. 심판 내부의 판단은 모두 Stockfish 점수끼리 비교합니다. 초반 심판 목표는 자기 쪽 기준 +20cp, 허용 범위는 ±80cp입니다. 21~49수에는 기준수의 Stockfish 평가를 향해 목표를 옮기고, 각 진영의 **50번째 착수부터는 Regicide 기준수**를 보존합니다. 기존 자체 정책의 20수 균형 회복·이후 전환도 유지합니다.

심판은 1스레드·Hash 32MiB·Skill Level 20·기력 제한 해제로 실행합니다. 동시에 실행하는 심판 프로세스는 최대 하나입니다.

| 용도 | 기본 계산 시간 | 처리 |
| --- | --- | --- |
| 현재 위치의 형세 | 500ms | 백그라운드에서 별도 채널로 갱신 |
| 두 자체 후보와 착수 후 반격 확인 | 총 1,500ms 이내 | 후보 공통 깊이 6 이상, 착수 후 깊이 4 이상 요구 |
| 남은 시간 3분 미만 | 최대 800ms | 두 후보 확인 예산 축소 |
| 남은 시간 1분 미만 | 최대 350ms | 검증이 완성되지 않으면 기준수 |
| 남은 시간 10초 미만 | 추가 후보 검사 생략 | 기준수 보존 |

형세 조회는 기동에 최대 1.5초를 별도로 허용하고, 준비가 끝난 뒤 500ms를 분석합니다. 분석 종료 여유는 기본 350ms로 제한하며 기동·분석 경과 시간을 구분해 기록합니다. 후보 검사에는 기동·정리 비용도 총 시간에 포함됩니다. 기준수와 후보가 같거나 50수 이후면 후보 검사를 생략합니다. 현재 위치·대국·수정 번호·실행 파일 해시가 맞아야 결과를 적용합니다. 새 위치·무르기·종료·‘지금 두기’와 시계 만료는 늦은 응답을 무효화합니다. 충분한 시간이 있어도 짧은 분석만으로 모든 전술적 손해를 배제할 수는 없습니다.

## 기록과 재현

JSON의 `sparring_referee`에는 심판 이름·실행 파일 SHA256·정책·설정 여부와 판정을 따로 기록합니다. `sparring_play.decisions[].regicide_proposal`은 원래 자체 판단, 같은 항목의 `referee`는 심판의 근거입니다. 실제 선택과 일치하는 증거만 보관함의 `referee_decisions`에 노출합니다. 심판 오류로 자체 기준수를 둔 경우에도 해당 대국의 외부 보조 모드 표시는 유지합니다.

PGN은 기존 `EngineSHA256`을 Regicide 신원으로 유지하고 `SparringReferee`, `RefereeBinarySHA256`, `RefereePolicy`, `AssistedSparring`을 추가합니다. 보관함 재생·다시 내보내기에도 보존합니다. 이전 기보에 심판 정보를 만들어 붙이지 않습니다. 외부 평가를 자체 평가 기록이나 신경망 학습 목표에 섞지 않습니다.

## 다른 컴퓨터의 설정

자동 조회 위치는 다음 두 곳이며, Windows 공식 고정 패키지의 바이너리와 원본 라이선스 해시를 확인합니다. 자동 다운로드는 하지 않습니다.

```text
build-m2-opponents/stockfish18/package/stockfish/stockfish-windows-x86-64.exe
build/referee/stockfish18/package/stockfish/stockfish-windows-x86-64.exe
```

다른 로컬 Stockfish는 `REGICIDE_REFEREE_ENGINE` 환경변수 또는 Python 실행기의 `--referee-engine`으로 지정할 수 있습니다. 잘못 지정한 경로를 다른 엔진으로 자동 대체하지 않습니다. Windows에서는 `.exe`, Linux/macOS에서는 해당 OS의 실행 파일이 필요합니다. 시작할 때 Stockfish UCI 이름과 필수 옵션도 확인합니다. `--no-referee`는 외부 심판을 완전히 끕니다. 직접 생성하는 `HumanGame`과 기존 단위 테스트는 심판을 명시적으로 연결하지 않으면 자체 엔진만 사용합니다.

## 외부 도구 출처

현재 사용한 파일은 [공식 Stockfish 18 릴리스](https://github.com/official-stockfish/Stockfish/releases/tag/sf_18)의 Windows x86-64 generic 패키지입니다. 소스 리비전은 `cb3d4ee9b47d0c5aae855b12379378ea1439675c`, 실행 파일 SHA256은 `9bde420202717ce083412027fbfb8c5c935b537591d712be8a8a8bae92f6e8d6`입니다. 원본 바이너리를 수정하거나 저장소에 포함하지 않았습니다. 소스와 [GPL 라이선스 원문](https://github.com/official-stockfish/Stockfish/blob/cb3d4ee9b47d0c5aae855b12379378ea1439675c/Copying.txt)은 공식 저장소를 따릅니다.

점수 해석은 [Stockfish 공식 FAQ](https://official-stockfish.github.io/docs/stockfish-wiki/Stockfish-FAQ.html)를 참고합니다. Stockfish의 정규화 점수를 Regicide 평가나 인간 레이팅과 동일시하지 않습니다. 이번 변경은 제한된 기능 검증이며, Elo·접전 성공률·체감 난이도 개선은 별도 대국 실험이 필요합니다.
