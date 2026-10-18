param(
    [switch]$NoOpen,
    [string]$GodotPath,
    [string]$NeuralModel
)

$ErrorActionPreference = 'Stop'
$repoRoot = Split-Path -Parent $PSScriptRoot
$pythonExecutable = Join-Path $repoRoot '.venv-tests/Scripts/python.exe'
$backendScript = Join-Path $PSScriptRoot 'play_human.py'
$engineExecutable = Join-Path $repoRoot 'build/regicide.exe'
$projectDirectory = Join-Path $repoRoot 'godot'
$logDirectory = Join-Path $repoRoot 'build/godot-play'
$gameDirectory = Join-Path ([Environment]::GetFolderPath('MyDocuments')) 'Regicide/Games'
$serverProcess = $null
$godotProcess = $null
$launchComplete = $false
$startupFile = $null

function Quote-ProcessPath([string]$Value) {
    # Start-Process joins ArgumentList as text. Windows paths cannot contain a
    # quote; these file/directory paths do not end in a directory separator.
    return '"' + $Value + '"'
}

function Stop-OwnedProcessTree($Process) {
    if ($null -eq $Process) { return }
    try {
        $Process.Refresh()
        if (!$Process.HasExited) {
            & taskkill.exe /PID $Process.Id /T /F 2>&1 | Out-Null
        }
    } catch { }
}

function Test-OwnBackendProcess([int]$BackendId, [int]$LauncherId) {
    # A venv python.exe can redirect to a child interpreter. Require this
    # launch's process or a descendant, rather than demanding identical PIDs.
    $currentId = $BackendId
    for ($generation = 0; $generation -lt 12 -and $currentId -gt 0; $generation++) {
        if ($currentId -eq $LauncherId) { return $true }
        $processInfo = Get-CimInstance Win32_Process -Filter ('ProcessId=' + $currentId) `
            -OperationTimeoutSec 3 -ErrorAction Stop
        if ($null -eq $processInfo -or [int]$processInfo.ParentProcessId -eq $currentId) { return $false }
        $currentId = [int]$processInfo.ParentProcessId
    }
    return $false
}

function Find-GodotExecutable {
    if ($GodotPath) {
        if (!(Test-Path -LiteralPath $GodotPath -PathType Leaf)) {
            throw "지정한 Godot 실행 파일을 찾을 수 없습니다: $GodotPath"
        }
        return (Resolve-Path -LiteralPath $GodotPath).Path
    }
    if ($env:REGICIDE_GODOT_PATH) {
        if (!(Test-Path -LiteralPath $env:REGICIDE_GODOT_PATH -PathType Leaf)) {
            throw "REGICIDE_GODOT_PATH의 실행 파일을 찾을 수 없습니다: $env:REGICIDE_GODOT_PATH"
        }
        return (Resolve-Path -LiteralPath $env:REGICIDE_GODOT_PATH).Path
    }
    foreach ($commandName in @('godot', 'godot4')) {
        $command = Get-Command $commandName -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 1
        if ($null -ne $command) { return $command.Source }
    }
    $installedPath = Join-Path $env:USERPROFILE 'Tools/Godot/Godot_v4.7.2-stable_win64.exe'
    if (Test-Path -LiteralPath $installedPath -PathType Leaf) { return $installedPath }
    throw 'Godot 4.4 이상이 필요합니다. -GodotPath로 실행 파일을 지정하거나 REGICIDE_GODOT_PATH 환경 변수를 설정해 주세요.'
}

try {
    if (!(Test-Path -LiteralPath $backendScript -PathType Leaf)) {
        throw 'scripts/play_human.py를 찾을 수 없습니다. 저장소 파일을 확인해 주세요.'
    }
    if (!(Test-Path -LiteralPath (Join-Path $projectDirectory 'project.godot') -PathType Leaf)) {
        throw 'Godot 대국 프로젝트를 찾을 수 없습니다. godot/project.godot 파일을 확인해 주세요.'
    }
    if (!(Test-Path -LiteralPath $pythonExecutable -PathType Leaf)) {
        throw 'Python 환경이 없습니다. 저장소에서 python -m venv .venv-tests 실행 후 .\.venv-tests\Scripts\python.exe -m pip install -r requirements-test.txt를 실행해 주세요.'
    }
    $godotExecutable = Find-GodotExecutable
    New-Item -ItemType Directory -Path $logDirectory -Force | Out-Null
    New-Item -ItemType Directory -Path $gameDirectory -Force | Out-Null
    $runName = 'session-' + [DateTime]::UtcNow.ToString('yyyyMMdd-HHmmss') + '-' + [Guid]::NewGuid().ToString('N')
    $startupFile = Join-Path $logDirectory ($runName + '.json')
    $stdoutFile = Join-Path $logDirectory ($runName + '.backend.stdout.log')
    $stderrFile = Join-Path $logDirectory ($runName + '.backend.stderr.log')
    $godotStdout = Join-Path $logDirectory ($runName + '.godot.stdout.log')
    $godotStderr = Join-Path $logDirectory ($runName + '.godot.stderr.log')

    # Version checking is a hidden helper; the actual game below is visible.
    $versionStdout = Join-Path $logDirectory ($runName + '.godot-version.stdout.log')
    $versionStderr = Join-Path $logDirectory ($runName + '.godot-version.stderr.log')
    $versionProcess = Start-Process -FilePath $godotExecutable -ArgumentList '--version' `
        -WorkingDirectory $repoRoot -WindowStyle Hidden -PassThru `
        -RedirectStandardOutput $versionStdout -RedirectStandardError $versionStderr
    # Windows PowerShell 5.1 needs a retained process handle to read ExitCode.
    $versionProcess.Handle | Out-Null
    if (!$versionProcess.WaitForExit(10000)) {
        Stop-OwnedProcessTree $versionProcess
        throw "Godot 버전을 확인하지 못했습니다. 실행 기록: $versionStderr"
    }
    $versionProcess.Refresh()
    $godotVersion = (Get-Content -LiteralPath $versionStdout -Raw -Encoding UTF8).Trim()
    if ($versionProcess.ExitCode -ne 0 -or $godotVersion -notmatch '^4\.(\d+)\.' -or [int]$Matches[1] -lt 4) {
        throw "Godot 4.4 이상이 필요합니다. 실행 파일과 버전을 확인해 주세요: $godotExecutable"
    }

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
        '--no-browser', '--desktop-client'
    )
    if ($NeuralModel) {
        $modelPath = (Resolve-Path -LiteralPath $NeuralModel -ErrorAction Stop).Path
        $serverArguments += @('--neural-model', (Quote-ProcessPath $modelPath))
    }
    $serverProcess = Start-Process -FilePath $pythonExecutable -ArgumentList $serverArguments `
        -WorkingDirectory $repoRoot -WindowStyle Hidden -PassThru `
        -RedirectStandardOutput $stdoutFile -RedirectStandardError $stderrFile
    $serverProcess.Handle | Out-Null

    $startup = $null
    $deadline = [DateTime]::UtcNow.AddSeconds(15)
    while ([DateTime]::UtcNow -lt $deadline) {
        if (Test-Path -LiteralPath $startupFile -PathType Leaf) {
            try {
                $candidate = Get-Content -LiteralPath $startupFile -Raw -Encoding UTF8 | ConvertFrom-Json
                if ($candidate.url -and $candidate.pid -and $candidate.token -and $candidate.engine_name -and
                    $candidate.binary_sha256 -match '^[0-9a-f]{64}$') {
                    $startup = $candidate
                    break
                }
            } catch {
                # The backend atomically replaces this launch's unique file.
            }
        }
        $serverProcess.Refresh()
        if ($serverProcess.HasExited) {
            throw "대국 관리기를 시작하지 못했습니다. 실행 기록: $stderrFile"
        }
        Start-Sleep -Milliseconds 100
    }
    if ($null -eq $startup) {
        throw "대국 관리기 준비에 15초 이상 걸렸습니다. 실행 기록: $stderrFile"
    }

    $startupUrl = [Uri]$startup.url
    if (!$startupUrl.IsAbsoluteUri -or $startupUrl.Scheme -ne 'http' -or
        $startupUrl.Host -ne '127.0.0.1' -or $startupUrl.Port -le 0 -or
        $startupUrl.UserInfo -or $startupUrl.Query -or $startupUrl.Fragment -or
        $startupUrl.AbsolutePath -ne '/' -or [int]$startup.pid -le 0) {
        throw "대국 관리기의 로컬 접속 정보를 확인할 수 없습니다. 실행 기록: $stderrFile"
    }
    if (!(Test-OwnBackendProcess ([int]$startup.pid) $serverProcess.Id)) {
        throw "이번에 시작한 대국 관리기의 프로세스를 확인할 수 없습니다. 실행 기록: $stderrFile"
    }
    try {
        $state = Invoke-RestMethod -Uri ($startupUrl.AbsoluteUri + 'api/state') `
            -Headers @{ 'X-Regicide-Token' = $startup.token } -Method Get -TimeoutSec 3
        if ($null -eq $state.revision -or !$state.board -or $state.engine.name -ne $startup.engine_name) {
            throw '대국 상태를 확인하지 못했습니다.'
        }
    } catch {
        throw "대국 관리기에 연결하지 못했습니다. 실행 기록: $stderrFile"
    }

    if ($NoOpen) {
        # Deliberately omit the token, including from redirected stdout/logs.
        [pscustomobject]@{
            url = $startup.url
            pid = $startup.pid
            backend_launcher_pid = $serverProcess.Id
            engine_name = $startup.engine_name
            binary_sha256 = $startup.binary_sha256
            startup_file = $startupFile
            godot_executable = $godotExecutable
            godot_version = $godotVersion
            project_directory = $projectDirectory
            stdout_log = $stdoutFile
            stderr_log = $stderrFile
        } | ConvertTo-Json -Compress
    } else {
        $godotArguments = @(
            '--path', (Quote-ProcessPath $projectDirectory),
            '--', '--backend-file', (Quote-ProcessPath $startupFile), '--own-backend'
        )
        # The user explicitly requested an interactive native game window.
        $godotProcess = Start-Process -FilePath $godotExecutable -ArgumentList $godotArguments `
            -WorkingDirectory $repoRoot -PassThru `
            -RedirectStandardOutput $godotStdout -RedirectStandardError $godotStderr
        $godotProcess.Handle | Out-Null
        Start-Sleep -Milliseconds 800
        $godotProcess.Refresh()
        if ($godotProcess.HasExited -and $godotProcess.ExitCode -ne 0) {
            throw "Godot 대국 화면을 열지 못했습니다. 실행 기록: $godotStderr"
        }
        Write-Host 'Regicide Godot 대국 화면을 열었습니다.'
        Write-Host "저장된 기보 폴더: $gameDirectory"
    }
    $launchComplete = $true
} catch {
    $failureMessage = $_.Exception.Message
    if (!$launchComplete) {
        Stop-OwnedProcessTree $godotProcess
        Stop-OwnedProcessTree $serverProcess
        if ($startupFile -and (Test-Path -LiteralPath $startupFile -PathType Leaf)) {
            Remove-Item -LiteralPath $startupFile -Force -ErrorAction SilentlyContinue
        }
    }
    [Console]::Error.WriteLine('Regicide Godot 실행 오류: ' + $failureMessage)
    exit 1
}
