param(
    [int]$GamesPerOpening = 2,
    [int]$Nodes = 1000,
    [int]$TeacherNodes = 10000,
    [int]$Epochs = 100,
    [int]$Seed = 20261018
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
    $runName = 'run-' + [DateTime]::UtcNow.ToString('yyyyMMdd-HHmmss') + '-' + [Guid]::NewGuid().ToString('N').Substring(0,8)
    $runDirectory = Join-Path $repoRoot ('build/learning/' + $runName)
    New-Item -ItemType Directory -Path $runDirectory -Force | Out-Null
    $dataDirectory = Join-Path $runDirectory 'data'
    $modelDirectory = Join-Path $runDirectory 'model'
    Write-Host '1/2 자체 대국을 두고 학습 자료를 모읍니다.'
    & $pythonExecutable scripts/collect_learning_data.py --binary $engineExecutable --output $dataDirectory --seed $Seed --games-per-opening $GamesPerOpening --nodes $Nodes --teacher-nodes $TeacherNodes --max-plies 256 --sample-every 4
    if ($LASTEXITCODE -ne 0) { throw ('자료 수집이 완료되지 않았습니다. 기록: ' + $dataDirectory) }
    Write-Host '2/2 작은 신경망을 학습하고 보류한 자료로 평가합니다.'
    & $pythonExecutable scripts/train_neural.py --dataset (Join-Path $dataDirectory 'manifest.json') --output $modelDirectory --epochs $Epochs --batch-size 64 --seed $Seed
    if ($LASTEXITCODE -ne 0) { throw ('학습이 완료되지 않았습니다. 기록: ' + $modelDirectory) }
    Write-Host ('완료. 학습 모델과 검증 결과: ' + $modelDirectory)
    Write-Host '새 모델의 대국 기력은 별도로 확인해야 합니다. 기본 대국 설정은 유지됩니다.'
} finally { Pop-Location }
