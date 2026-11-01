param([switch]$NoOpen)

$ErrorActionPreference = 'Stop'
$repoRoot = Split-Path -Parent $PSScriptRoot
$pythonExecutable = Join-Path $repoRoot '.venv-tests/Scripts/python.exe'
$backendScript = Join-Path $PSScriptRoot 'play_human.py'
$engineExecutable = Join-Path $repoRoot 'build/regicide.exe'
$logDirectory = Join-Path $repoRoot 'build/human-play'
$gameDirectory = Join-Path ([Environment]::GetFolderPath('MyDocuments')) 'Regicide/Games'
$serverProcess = $null
$serverReady = $false

function Quote-ProcessPath([string]$Value) {
    # Windows paths cannot contain a quote; these paths are files or directory
    # names without a trailing slash. Start-Process joins ArgumentList as text.
    return '"' + $Value + '"'
}

try {
    if (!(Test-Path -LiteralPath $backendScript -PathType Leaf)) {
        throw '대국 실행 파일 scripts/play_human.py를 찾을 수 없습니다. 저장소 파일을 확인해 주세요.'
    }
    if (!(Test-Path -LiteralPath $pythonExecutable -PathType Leaf)) {
        throw 'Python 환경이 없습니다. 저장소에서 python -m venv .venv-tests 실행 후 .\.venv-tests\Scripts\python.exe -m pip install -r requirements-test.txt를 실행해 주세요.'
    }

    New-Item -ItemType Directory -Path $logDirectory -Force | Out-Null
    New-Item -ItemType Directory -Path $gameDirectory -Force | Out-Null
    $runName = 'session-' + [DateTime]::UtcNow.ToString('yyyyMMdd-HHmmss') + '-' + [Guid]::NewGuid().ToString('N')
    $startupFile = Join-Path $logDirectory ($runName + '.json')
    $stdoutFile = Join-Path $logDirectory ($runName + '.stdout.log')
    $stderrFile = Join-Path $logDirectory ($runName + '.stderr.log')

    $dependencyExit = 1
    try {
        $dependencyMessages = & $pythonExecutable -c 'import chess; import chess.engine' 2>&1
        $dependencyExit = $LASTEXITCODE
    } catch {
        $dependencyMessages = $_
    }
    if ($dependencyExit -ne 0) {
        $dependencyFile = Join-Path $logDirectory ($runName + '.dependencies.log')
        $dependencyMessages | Out-File -LiteralPath $dependencyFile -Encoding utf8
        throw "체스 도구가 준비되지 않았습니다. .\.venv-tests\Scripts\python.exe -m pip install -r requirements-test.txt를 실행해 주세요. 자세한 기록: $dependencyFile"
    }

    # Resolve an immutable installed engine for this launch without replacing
    # the executable used by any running game.
    $resolvedEngine = & $pythonExecutable (Join-Path $PSScriptRoot 'runtime_engine.py') --resolve
    if ($LASTEXITCODE -ne 0 -or @($resolvedEngine).Count -ne 1) {
        throw '설치된 체스 엔진을 확인하지 못했습니다. 엔진 선택 기록과 실행 파일을 확인해 주세요.'
    }
    $engineExecutable = [string]$resolvedEngine
    if (!(Test-Path -LiteralPath $engineExecutable -PathType Leaf)) {
        $buildFile = Join-Path $logDirectory ($runName + '.build.log')
        try {
            & (Join-Path $PSScriptRoot 'build.ps1') *> $buildFile
        } catch {
            throw "체스 엔진을 만들지 못했습니다. Visual Studio의 C++ 데스크톱 개발 도구를 확인해 주세요. 자세한 기록: $buildFile"
        }
        if (!(Test-Path -LiteralPath $engineExecutable -PathType Leaf)) {
            throw "체스 엔진 실행 파일이 없습니다. 자세한 빌드 기록: $buildFile"
        }
    }

    $serverArguments = @(
        (Quote-ProcessPath $backendScript),
        '--binary', (Quote-ProcessPath $engineExecutable),
        '--port', '0',
        '--save-dir', (Quote-ProcessPath $gameDirectory),
        '--import-games', (Quote-ProcessPath (Join-Path $repoRoot 'build/human-games')),
        '--startup-file', (Quote-ProcessPath $startupFile),
        '--no-browser'
    )
    $serverProcess = Start-Process -FilePath $pythonExecutable -ArgumentList $serverArguments `
        -WorkingDirectory $repoRoot -WindowStyle Hidden -PassThru `
        -RedirectStandardOutput $stdoutFile -RedirectStandardError $stderrFile

    $startup = $null
    $deadline = [DateTime]::UtcNow.AddSeconds(10)
    while ([DateTime]::UtcNow -lt $deadline) {
        if (Test-Path -LiteralPath $startupFile -PathType Leaf) {
            try {
                $candidate = Get-Content -LiteralPath $startupFile -Raw -Encoding UTF8 | ConvertFrom-Json
                if ($candidate.url -and $candidate.pid -and $candidate.engine_name -and $candidate.binary_sha256) {
                    $startup = $candidate
                    break
                }
            } catch {
                # A concurrently written startup file may be incomplete briefly.
            }
        }
        $serverProcess.Refresh()
        if ($serverProcess.HasExited) {
            throw "대국 화면을 시작하지 못했습니다. 실행 기록: $stderrFile"
        }
        Start-Sleep -Milliseconds 100
    }
    if ($null -eq $startup) {
        throw "대국 화면 준비에 10초 이상 걸렸습니다. 실행 기록: $stderrFile"
    }

    $startupUrl = [Uri]$startup.url
    if (!$startupUrl.IsAbsoluteUri -or $startupUrl.Scheme -ne 'http' -or !$startupUrl.IsLoopback) {
        throw "대국 화면의 로컬 주소를 확인할 수 없습니다. 실행 기록: $stderrFile"
    }
    try {
        $response = Invoke-WebRequest -Uri $startupUrl.AbsoluteUri -UseBasicParsing -TimeoutSec 3
        if ($response.StatusCode -ne 200) {
            throw '대국 화면 응답을 확인하지 못했습니다.'
        }
    } catch {
        throw "대국 화면에 연결하지 못했습니다. 실행 기록: $stderrFile"
    }
    $serverReady = $true

    if ($NoOpen) {
        [pscustomobject]@{
            url = $startup.url
            pid = $startup.pid
            engine_name = $startup.engine_name
            binary_sha256 = $startup.binary_sha256
            startup_file = $startupFile
            stdout_log = $stdoutFile
            stderr_log = $stderrFile
        } | ConvertTo-Json -Compress
    } else {
        try {
            Start-Process -FilePath $startupUrl.AbsoluteUri
        } catch {
            throw "브라우저를 열지 못했습니다. 이 주소를 직접 열어 주세요: $($startup.url)"
        }
        Write-Host "Regicide 대국 화면을 열었습니다: $($startup.url)"
        Write-Host "저장된 기보 폴더: $gameDirectory"
    }
} catch {
    $failureMessage = $_.Exception.Message
    if ($null -ne $serverProcess -and !$serverReady) {
        # Stop only the process tree launched by this attempt. A Python venv
        # executable may be a redirector whose actual server is a child process.
        try { & taskkill.exe /PID $serverProcess.Id /T /F 2>&1 | Out-Null } catch { }
    }
    [Console]::Error.WriteLine('Regicide 실행 오류: ' + $failureMessage)
    exit 1
}
