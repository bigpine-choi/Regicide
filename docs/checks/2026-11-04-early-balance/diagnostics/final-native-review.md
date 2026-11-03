# 최종 소스의 실제 추가 2판 독립 교차검토

[검사 결과](final-native-cross-check.json), [재현 도구](audit_final_native.py), [최종 동결 소스 해시](final-source-snapshot/manifest.json). 추가 엔진을 실행하지 않고 게임 원본과 native 원시 탐색 패킷을 대조했다.

| 대국 | 완료 ply | 적용 대체 | 문제 |
| --- | ---: | ---: | ---: |
| pair3-1700-black | 100 | 9 | 0 |
| pair3-1700-white | 79 | 8 | 0 |

두 판 모두 정상 완료했고 총 179ply·17건의 실제 대체를 확인했다. 모든 대체 수는 같은 원본 국면·전체 기보에서 Regicide가 반환한 완료 패킷과 일치했다. 발견용 후보와 singleton 재확인 탐색을 따로 대조하고, 저장된 자체 점수가 외부 평가값으로 바뀌지 않았는지 확인했다. 실제 엔진 파일의 해시를 다시 읽어 metadata 및 각 외부 평가에 기록한 해시와 대조했다. native Hash16·UsePieceActivity=true도 확인했다. Python 소스·실행 파일 불변 metadata와 최종 5개 핵심 소스 snapshot 해시가 일치했다.

## 접전 조절·동가 교환 근거

- 흑 13...Bg4: native depth6, 외부 +24cp, `balanced`.
- 백 12.Qd2: native depth5, 외부 +17cp, `balanced`.
- 백 17.Ba4: native depth6, 외부 +289cp, `balance_improved`. 바로 전 자체 Nxb7이 같은 값의 비숍을 잡았고 상대가 O-O로 재포획을 한 번 미뤘다. b7의 같은 나이트·같은 칸을 유지하며 원래부터 합법이던 동일 재포획만 완료하는 `pending_equal_exchange` 증거를 전체 기보로 다시 구성했다.
- 백 19.f4: native depth5, 외부 +98cp, `balance_improved`. 실제 ±100cp 범위지만 내부 ±80cp 버퍼 미달을 `balanced`로 올려 세지 않았다.

두 `balanced`와 두 `balance_improved` 모두 새 기물 헌납 예외를 쓰지 않았고, 일반 strict 검사 또는 좁은 보류 교환 증거가 원본과 일치했다. 기록된 SEE 교환은 모든 합법 같은 칸 포획을 대상으로 별도의 전체 재료 점수 minimax를 계산해 순이득을 대조했다. promotion 가치 변화도 전체 재료 점수에 포함한다. 원본 SEE가 미완료면 이득 0·소진 상태여야 하며, pending 교환 크레딧에는 완료된 SEE만 사용했음을 확인했다.

이 두 실제 대국의 `reference_confirmed_native_candidate` 적용은 **0건**이다. 그 경로는 고정 Qd5 재현과 별도 자동 검사 증거를 사용해야 하며, 이번 실제 대국에서 적용됐다고 표현하지 않는다.

## 보수적 기각과 사후 해석

백 15.Bxb5는 짧은 외부 평가가 +137→+143cp여도 국소 헌납 신호 때문에 최종 게이트에서 기각됐다. 다른 수를 못 찾아 원래 수를 `unverified`로 유지한 기록은 보존한다. [법적 강제 회수 검토](bxb5-material-review.md)에서 `...Qxa5 Bxd7+` 뒤의 모든 흑 왕 응수와 `Bxc8` 회수를 확인했다. 이 신호를 보상 없는 나이트 헌납으로 단정하지 않는다.

앞선 4판 280ply는 이전 소스로 실행한 별도 기록이며 원본과 snapshot을 보존했다. 위 179ply만 최종 SEE·보류 교환 소스의 실제 대국 증거다. 이 교차검토는 전체 기력·Elo·20수 연속 접전 보장이 아니며, 접전 비율과 깊은 형세 재평가는 별도 Stockfish 감사에서 판단한다.
