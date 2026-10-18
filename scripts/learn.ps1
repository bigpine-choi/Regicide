param(
    [int]$Rounds = 3,
    [int]$GamesPerOpening = 2,
    [int]$Nodes = 1000,
    [int]$TeacherNodes = 10000,
    [int]$Epochs = 100,
    [int]$Seed = 20261018,
    [int]$AnalysisNodes = 50000,
    [int]$ArenaPairs = 32,
    [int]$MoveTimeMS = 100
)
$ErrorActionPreference = 'Stop'
$repoRoot = Split-Path -Parent $PSScriptRoot
$pythonExecutable = Join-Path $repoRoot '.venv-tests/Scripts/python.exe'
$engineExecutable = Join-Path $repoRoot 'build/regicide.exe'
if (!(Test-Path -LiteralPath $pythonExecutable)) { throw '먼저 README의 Python 실행 환경 준비를 진행해 주세요.' }
if (!(Test-Path -LiteralPath $engineExecutable)) { throw '먼저 scripts/build.ps1로 엔진을 빌드해 주세요.' }
Push-Location $repoRoot
try {
    & $pythonExecutable -c 'import chess, numpy'
    if ($LASTEXITCODE -ne 0) {
        & $pythonExecutable -m pip install -r requirements-learning.txt
        if ($LASTEXITCODE -ne 0) { throw '학습 도구 설치에 실패했습니다.' }
    }
    Write-Host '자가대국 → 패배 분석 → 자료 누적 → 재학습 → 이전 모델 대국 검증을 자동으로 반복합니다.'
    Write-Host '기준을 통과한 모델만 채택합니다. 측정 중에는 대국과 브라우저 사용을 쉬어 주세요.'
    Write-Host 'Ctrl+C로 중단할 수 있으며, 완료된 자료와 기존 채택 모델은 보존합니다.'
    & $pythonExecutable scripts/learning_cycle.py --binary $engineExecutable --rounds $Rounds --seed $Seed --games-per-opening $GamesPerOpening --nodes $Nodes --teacher-nodes $TeacherNodes --analysis-nodes $AnalysisNodes --epochs $Epochs --arena-pairs $ArenaPairs --movetime-ms $MoveTimeMS
    if ($LASTEXITCODE -ne 0) { throw ('학습 순환이 중단됐습니다. 기록과 현재 채택 모델을 확인해 주세요: ' + (Join-Path $repoRoot 'build/learning-cycle')) }
    Write-Host ('완료. 누적 자료와 판정 기록: ' + (Join-Path $repoRoot 'build/learning-cycle'))
    Write-Host '새 채택 모델이 있으면 다음 Regicide 대국을 시작할 때 자동으로 사용합니다.'
} finally { Pop-Location }
