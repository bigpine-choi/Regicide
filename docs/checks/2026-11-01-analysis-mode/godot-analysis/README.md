# Godot 분석 모드 자동 검증

2026-11-01에 Godot 4.7.2와 `build/regicide.exe`의 Regicide 0.7.0-dev를 사용했다. 엔진 SHA-256은 `edd950fb673cfca4134968b64cc5ef0f3d77eda698e9f40d2965170f143d6e6a`이다.

```powershell
.\.venv-tests\Scripts\python.exe tests/analysis_godot_tests.py `
  --godot C:/Users/hanso/Tools/Godot/Godot_v4.7.2-stable_win64_console.exe `
  --binary build/regicide.exe --output build/analysis-godot
```

새 프로젝트 복사본의 캐시 없는 실행, 합성 입력 검사 22개, 실제 Regicide와 임시 로컬 관리기를 연결한 검사 30개를 통과했다. 두 검사 모두 headless 모드에서 실행했으며 사람이 창을 직접 클릭한 검증이나 화면 배치의 시각 검증을 의미하지 않는다.

합성 검사는 기물 배치·지우개·차례·FEN 왕복·잘못된 FEN의 원상 보존·화살표 좌표와 시점 전환·오래된 추천 무효화·연결 단절 후 복귀를 확인한다. 실제 연동 검사는 왕 없는 배치 거절, 사용자 시작 배치의 추천 수, 백·흑의 수동 착수, 한 수 무르기, 분석 중지 후 재편집, 나이트 승격, 재진입을 확인한다.

화면 직접 조작에서 상태 조회 중 클릭이 누락되는 문제를 발견해, 상태 조회와 착수 요청의 HTTP 연결을 분리하고 오래된 조회 응답을 무효화했다. 회귀 검사는 실제 GET 응답을 350ms 지연시켜 조회 도중 출발 칸·승격 도착 칸 클릭, 조회 응답 후 선택 보존, 조회 중 나이트 승격 요청을 확인한다. 서버 시각 기록으로 오래된 GET이 반환되기 전에 착수 요청이 완료된 사실을 별도로 검사하고, 늦은 응답이 승격된 기물이나 승격 선택 창을 되돌리지 않는지 확인했다.

분석 조작 중 기존 대국 상태와 대국 저장 파일의 바이트가 동일했고, 기록된 POST 요청은 모두 `/api/analysis`였다. 요청 경로·엔진 정보·검증 방식은 `analysis-integration.json`에 있다. 인증 토큰과 임시 연결 설정은 보관하지 않았다.

원본 `build/analysis-godot/`의 결과 내용을 그대로 복사했다. 버전 관리에서 제외되는 `.log` 확장자만 `.txt`로 바꿨으며, 원본과 사본의 SHA-256 일치를 확인했다. 파일 대응과 해시는 `sha256.json`에 기록했다.
