# 매 수 평가 갱신과 Windows 영어 음성 복구

## 변경과 원인

일반 대국의 형세 막대는 엔진 착수 후 직전 검색 점수를 유지했다. 이제 사람 차례에는 현재 국면을 독립적으로 평가하고, 엔진 차례에는 실제 수 선택 검색의 점수를 재사용한다. 추천 수를 꺼도 동작한다. 대국·revision·FEN이 바뀐 늦은 결과는 버리고, 무르기·새 대국·핸디캡·종료를 함께 처리한다. 선택한 생각 시간을 사용하며 무한 분석하지 않는다. 막대의 변환식과 C++ 엔진은 변경하지 않았다.

영어 음성이 없던 원인은 Windows의 두 음성 목록 차이였다. 실제 Godot 4.7.2 Windows 실행은 OneCore 한국어 Heami만 반환했지만, Desktop SAPI에는 영어 Microsoft Zira Desktop가 있었다. Godot가 영어를 제공하지 못하면 로컬 Desktop 음성을 조회해 WAV로 합성하고 게임의 AudioStreamPlayer로 재생한다. 텍스트는 JSON 파일로 전달하며 네트워크 전송·레지스트리 변경·음성 설치는 없다. 취소된 요청의 WAV, 다른 국면의 늦은 결과, 숨긴 정답은 재생하지 않는다. 준비·실패·재시도 상태도 표시한다.

## 검증

- 현재 국면 평가 상태: 21개 통과. 백·흑 부호, 경계값, 현재 FEN, 추천 OFF, 늦은 응답, 무르기, 시간 설정, 종료 경쟁을 포함한다.
- 실제 Regicide 임시 대국: 초기 배치, 두 번의 사람 착수/엔진 응수, 무르기, 흑 선택 후 첫 응수의 7단계 통과. 사용자 대국 서버에 접속하지 않았다. [기록](live-smoke.json)
- 기존 대국 24개, 분석 21개, 분석 평가 14개, 추천 14개, 시간 설정 스트림 1개 통과. 선택 실행 항목은 각 1/1/2/1개 건너뜀. 신경망 세션 모델 고정·현재 FEN 관련 실제 엔진 검사 3개도 통과했다.
- Godot 코칭 회귀: 412개 통과. 음성 탐색 지연·합성 중 상태·오래된 WAV·실패 재시도·음소거·숨김을 포함한다. 3개 창 크기에서 보드 크기 변화 0.
- 실제 Windows Desktop 어댑터: 12개 통과. JSON 요청→SAPI 합성→Godot WAV 로딩·취소·정리·준비 실패 후 재시도를 확인했다.
- Windows 합성 검사: 3개 통과. 16비트 PCM 최대 진폭 21046으로 무음이 아님을 확인했다.
- 별도 숨김 Windows 실행에서 3.94초 영어 문장을 실제 AudioStreamPlayer로 재생했다. 0.5초 뒤 재생 위치 증가와 정상 완료 신호를 확인했다. 사용자가 스피커로 들었는지까지 확인한 것은 아니다.
- 브라우저 코칭 회귀 62개, 비교 스크립트 무결성 검사 5개 통과.
- 사용자 게임을 종료하거나 재시작하지 않았으며 기존 개인 기보를 수정하지 않았다.

집계와 재생 기록은 [evidence.json](evidence.json)에 있다. 음성 검사에서는 개인 기보가 아닌 고정된 영어 문장을 사용했다.

## 실제 기보와 Stockfish 비교

최근 완료된 실제 대국 두 판의 67개 착수 직전 국면을 합법 재생했다. 각 엔진에 같은 1초, Hash 16MB, 한 스레드, MultiPV 1을 주고 매 검색 해시를 초기화했다. 국면마다 실행 순서를 교대하고 두 엔진을 순차 실행했다. CPU는 AMD Ryzen AI 7 350(8코어/16논리 프로세서)이며 외부 부하는 통제하지 않았다.

- 자체 엔진: Regicide 0.7.0-dev, 기본 공식 평가. 바이너리와 설정은 기존 classical 프로필 해시로 확인했다.
- 외부 기준: 이미 보관된 Stockfish 18. 소스·GPL-3.0-or-later 이용 조건과 바이너리 해시는 기존 고정 프로필을 사용했다. 최신 버전 또는 Chess.com의 현재 설정과 동일하다고 주장하지 않는다.
- 첫 추천 수가 같은 국면은 31/67개였다. 이는 정확도 비율이 아니다. 한 엔진만 메이트를 표시한 국면은 없었다.
- 추천이 갈린 3개 국면을 3초로 재검사하고, 초기 후보가 이미 크게 기운 국면에 치우쳐 기준 평가 절댓값 3.00 이하의 3개 국면을 추가했다. 각 국면에서 기준 엔진의 자유 선택과 Regicide 수로 제한한 선택을 같은 3초로 비교하고 Regicide도 3초로 다시 계산했다.
- 한 국면에서는 Regicide의 1초 추천 Qg4가 3초에서 d5로 바뀌었고 기준 엔진은 Bb7을 선택했다. 또 다른 국면에서는 Regicide가 Qxf2, 기준 엔진은 d6를 선호했다. 후속 전술·왕 안전·전개 진단 후보이며 특정 평가 항목의 결함을 증명하지 않는다.

두 엔진의 CP는 공통 척도로 보정되지 않았다. 원점수 차이는 정확도·수의 손실·Elo로 해석하지 않는다. 추가 제한 탐색 역시 서로 다른 유한 검색이므로 확정적인 최선수 증명이 아니다. 게이지가 약하게 움직인다는 체감만으로 형세 판단이 항상 약하다고 결론 내릴 수 없으며, 이번 표본에서 Regicide가 항상 작은 점수를 내는 패턴도 확인하지 못했다.

개인 기보·FEN·전체 수순·원본 경로가 있는 상세 기록은 Git에서 제외된 `build/evaluation-diagnosis/comparison-001/`에만 둔다. 원본 파일은 전후 해시가 같았다. 공개 집계에는 경로·전체 기보를 포함하지 않는다. 자체 수 선택 엔진을 Stockfish로 교체하지 않았다.

재현 도구:

```text
python scripts/evaluation_comparison.py --game SAVED_GAME.json --reference-profile docs/checks/2026-10-21-m2-foundation/opponents/stockfish18-profile.json --output build/evaluation-comparison-new --time-ms 1000 --confirm-ms 3000
python tests/evaluation_comparison_tests.py
python tests/evaluation_state_tests.py
python tests/windows_speech_tests.py
GODOT --headless --path COPIED_GODOT_PROJECT --script res://tests/windows_speech_tests.gd -- --require-english
```

출처: [Godot Windows TTS 구현](https://github.com/godotengine/godot/blob/4.7.2-stable/platform/windows/tts_windows.cpp), [Stockfish 평가 점수 해석](https://official-stockfish.github.io/docs/stockfish-wiki/Stockfish-FAQ.html), [고정된 외부 엔진 프로필](../2026-10-21-m2-foundation/opponents/stockfish18-profile.json).
