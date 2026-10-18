# v0.5.8 보수적인 LMR 공개 검증 자료

실행 코드 고정 커밋은 `157305d804bc1828641a7e8d9b9b793b7746bcc3`이며 [설계와 검증 기록](../../experiments/2026-10-18-lmr.md), [사전 계획](../../experiments/2026-10-18-lmr-plan.md)을 함께 읽는다. 규칙 정확성, LMR OFF의 기존 탐색 동등성, LMR ON의 계산 비용, 같은 시간 소규모 대국은 서로 다른 증거다. 이 폴더만으로 일반적인 기력 상승이나 Elo를 확정하지 않는다.

## 파일 구분

- `off-equivalence-depth3/4/5*`: v0.5.7 대 v0.5.8 LMR OFF, 각30쌍. 수·점수·깊이·노드·seldepth·PV·TT 전체 일치 검증이다.
- `on-cost-depth5/6*`: 같은 v0.5.8 OFF/ON, 각30쌍. 선택적 검색이므로 점수 차이를 허용한 계산 비용 측정이다.
- `*-lmr.json`: 각 실행기의 검색 호출과 1:1 대응하는 보조 통계다. warmup도 별도 표시한다. 없는 구버전 통계는 `null`이다.
- `match-200ms.json/pgn` 및 sidecar: 사전 고정한 새8시작점의 선후공 교대16판, 200ms/수, Hash16MiB, overhead20ms, claim, 300반수 한도다. `*`는 미완료이며 무승부가 아니다.
- `search-load.json`, `match-load.json`: 목표500ms의 프로세스 CPU 집계 표본이다. 개별 프로세스 이름/ID를 공개하지 않는다. 새로 시작·종료·접근 불가 프로세스, 온도와 클럭을 완전히 관찰하지 못한다. Regicide 합계에는 비교 엔진이 포함된다.
- `frozen-regicide_*.log`, `frozen-differential.log`, `frozen-engine-integration.log`: 코드 커밋 후 재링크한 최종 실행 파일의 핵심6종·규칙·UCI 재확인이다.
- `windows-native.log`, `protocol-*`, `*godot_tests.log`, `godot_integration_tests.log`: 동일한 최종 소스의 커밋 전 링크에 대한 넓은 호환성 검사다. `provenance.json`과 `protocol-tools-summary.json`의 서로 다른 바이너리 해시를 구분한다. 이중 실행과 하위에서 호출한 검사를 합계에 중복 산입하지 않는다.
- `linux-*`, `validate-linux.sh`: TT 안전성 수정 후 Linux 엄격 빌드·ASan·UBSan·누수 검사다. 검사 시점은 미커밋 최종 소스이며, 전후 raw 소스 해시가 고정 커밋의 파일 내용과 대응한다.
- `independent-core-review.md`: 첫 구현의 혼합 깊이 TT 저장 위험과 저장 생략 수정의 독립 읽기 검토다. 수정 전 테스트를 최종 검증에 합산하지 않는다.
- `independent-lmr-audit.*`, `audit-lmr.py`: 고정 깊이·LMR 통계·PGN 전체 재생·종료·시간/색/해시·CPU 기록의 독립 감사다. 기력 보장은 아니다.
- `public-replay-audit.*`: 경로를 치환한 공개 사본의 재감사도13,212항목·실패0으로 통과했다. 원본 감사의 실행 파일 바이트 검사10회는 공개 실행 파일이 없어 생략되며 선언된 해시와 메타데이터를 대조한다. 기보·통계의 숫자와 수순은 동일하게 검증한다.
- `installation-verification.json`: 개인 기보의 원본 보존·백업 검증, 설치 실행 파일·실제 창/추천 확인의 공개 요약이다. 개인 기보와 백업 위치 자체는 포함하지 않는다.

## 공개 사본 정책

`build/lmr-validation`의 지정된 최상위 로그·JSON·PGN·재현/감사 스크립트와 `build/lmr-linux`의 지정6파일만 선택했다. 실행 파일·오브젝트·개인 기보·백업·토큰·세션·Godot 테스트 출력 하위 폴더와 예비 smoke 실행은 제외했다. smoke는 형식/수집기 확인용이며 비용/기력 측정에 합산하지 않는다.

Windows/WSL 경로와 호스트명을 `$REPO`, `$BASELINE_REPO`, `$WSL_REPO`, `$USERPROFILE`, `$VS_VCVARS64`, `$HOSTNAME`으로 치환했다. JSON 숫자·점수·노드·수순·옵션·시드·원본 해시는 바꾸지 않았다. 공개 텍스트는 UTF-8/LF이며 `linux-final-source-sha256.txt`는 원본 바이트 그대로 보존한다. `.gitattributes`는 이 폴더의 자동 개행 변환을 막는다.

각 원자료 내부의 `evidence_sha256`은 당시 원본을 가리킨다. **경로를 치환한 공개 파일의 실제 바이트 해시는 [sha256.json](sha256.json)** 을 사용한다. 엔진/소스 SHA는 원본 정체성이므로 그대로 둔다. 공개 자료에는 실행 파일이 없으므로 재감사에서 선언된 엔진 SHA의 일관성을 검사할 수는 있지만 파일 바이트를 새로 확인할 수는 없다.

## 재현

고정 커밋에서 테스트 의존성을 설치하고 Windows는 저장소의 `scripts/build.ps1 -Test`를 사용한다. Linux는 `validate-linux.sh`의 경로를 자신의 checkout으로 바꿔 실행한다. 기준 v0.5.7 실행 파일도 해당 소스로 별도 빌드해 확보한다. 새 빌드의 SHA는 운영체제·도구·링크 메타데이터에 따라 다를 수 있다.

`run-fixed-depth.ps1`, `run-match.ps1`, `run-monitored.ps1`에서 공개 경로 변수를 자기 경로로 바꾸고 새 출력 폴더에서 실행한다. 원래 출력과 감사는 덮어쓰지 않는다. `scripts/lmr_experiment.py`가 수집기를 제공하며, 체스 규칙 검증은 `chess==1.11.2`를 사용한다. 시간 측정 중에는 빌드·다른 검증·대국 계산을 겹치지 않는다.

공개 사본 감사는 저장소 안에서 다음처럼 새 이름으로 실행할 수 있다. 소스 커밋의 Git 객체와 테스트 데이터도 필요하다.

```powershell
python docs/checks/2026-10-18-lmr/audit-lmr.py --evidence docs/checks/2026-10-18-lmr --output-prefix replay-audit
```

이 폴더의 감사·검증은 실제 로컬 실행 결과이며 원격 CI 통과 주장과 구분한다.
