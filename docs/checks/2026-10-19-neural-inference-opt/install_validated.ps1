param(
    [Parameter(Mandatory = $true)]
    [ValidatePattern('^[0-9a-fA-F]{40}$')]
    [string]$ValidatedCommit,
    [Parameter(Mandatory = $true)]
    [ValidatePattern('^[0-9a-fA-F]{64}$')]
    [string]$ValidatedBinarySha256,
    [string]$PrimaryRoot = '<installed>',
    [string]$ResearchRoot = '<research>'
)

# Preparation only: run this helper AFTER all timed measurements have stopped.
# It does not delete state, move old branches, reset a checkout, or promote models.
$ErrorActionPreference = 'Stop'
$baselineCommit = '00dec89b08de4e5f573b18b440a93cd82f4b3311'
$baselineBinarySha256 = '605681320aff062ba2878eece74f453bf6ee451f1faa223e74a18caece328e5a'
$remoteBranch = 'perf/neural-evaluation-cost'
$desktopBranch = 'desktop/neural-inference-opt'
$ValidatedCommit = $ValidatedCommit.ToLowerInvariant()
$ValidatedBinarySha256 = $ValidatedBinarySha256.ToLowerInvariant()
$PrimaryRoot = (Resolve-Path -LiteralPath $PrimaryRoot).Path
$ResearchRoot = (Resolve-Path -LiteralPath $ResearchRoot).Path
$installedBinary = Join-Path $PrimaryRoot 'build\regicide.exe'
$candidateBinary = Join-Path $ResearchRoot 'build-neural-v4\regicide.exe'
$backupDirectory = [IO.Path]::GetFullPath((Join-Path $PrimaryRoot 'build\v063-before-neural-inference-opt'))
$backupBinary = Join-Path $backupDirectory 'regicide.exe'
$fixtureRelative = 'tests\data\neural_diagnostic_positions_v1.json'
$checker = Join-Path $ResearchRoot 'build\neural-inference-opt\check_protection.py'
$python = Join-Path $PrimaryRoot '.venv-tests\Scripts\python.exe'
$auditDirectory = Join-Path $ResearchRoot 'build\neural-inference-opt'
$protectionAfter = Join-Path $auditDirectory 'protection-after-install.json'
$resultPath = Join-Path $auditDirectory 'installation.json'

function Invoke-CheckedGit {
    param([string[]]$GitArguments)
    $gitOutput = & git -C $PrimaryRoot @GitArguments
    if ($LASTEXITCODE -ne 0) {
        throw ('Git failed: ' + ($GitArguments -join ' '))
    }
    return $gitOutput
}

function Get-LiteralSha256 {
    param([string]$Path)
    return (Get-FileHash -LiteralPath $Path -Algorithm SHA256).Hash.ToLowerInvariant()
}

$buildDirectory = (Resolve-Path -LiteralPath (Join-Path $PrimaryRoot 'build')).Path
$buildPrefix = $buildDirectory.TrimEnd([char[]]'\/') + [IO.Path]::DirectorySeparatorChar
if (-not $backupDirectory.StartsWith($buildPrefix, [StringComparison]::OrdinalIgnoreCase)) {
    throw 'Backup target escaped the intended primary build directory.'
}
if (@(Get-Process -Name 'regicide' -ErrorAction SilentlyContinue).Count -ne 0) {
    throw 'Regicide is still running. Finish measurements and close that process before installing.'
}
if (-not (Test-Path -LiteralPath $checker -PathType Leaf)) {
    throw 'The preservation checker is missing.'
}
if (-not (Test-Path -LiteralPath $python -PathType Leaf)) {
    throw 'The validated Python environment is missing.'
}
if (Test-Path -LiteralPath $backupBinary) {
    throw 'The planned backup already exists; inspect the earlier attempt before resuming manually.'
}
if (Test-Path -LiteralPath $resultPath) {
    throw 'An installation result already exists; do not overwrite prior installation evidence.'
}

$beforeHead = (Invoke-CheckedGit -GitArguments @('rev-parse', 'HEAD')).Trim()
$beforeBranch = (Invoke-CheckedGit -GitArguments @('branch', '--show-current')).Trim()
$status = @(Invoke-CheckedGit -GitArguments @('status', '--porcelain', '--untracked-files=normal'))
if ($beforeHead -ne $baselineCommit -or $status.Count -ne 0) {
    throw 'The installed checkout is not the clean expected v0.6.3 baseline.'
}
if ((Get-LiteralSha256 -Path $installedBinary) -ne $baselineBinarySha256) {
    throw 'The installed executable no longer matches the recorded v0.6.3 baseline.'
}
if ((Get-LiteralSha256 -Path $candidateBinary) -ne $ValidatedBinarySha256) {
    throw 'The candidate executable does not match the independently validated SHA256.'
}
if ((Get-LiteralSha256 -Path (Join-Path $ResearchRoot $fixtureRelative)) -ne
    '271b5e9288c07fc63d8d59e283bd2d83cc33ad62571d2a823d636e03909672c5') {
    throw 'The source fixture differs from its baseline bytes.'
}

# The explicit refspec verifies the published branch without merging or resetting.
Invoke-CheckedGit -GitArguments @(
    'fetch', '--quiet', 'origin',
    ('refs/heads/' + $remoteBranch + ':refs/remotes/origin/' + $remoteBranch)
) | Out-Null
$remoteHead = (Invoke-CheckedGit -GitArguments @('rev-parse', ('refs/remotes/origin/' + $remoteBranch))).Trim()
if ($remoteHead -ne $ValidatedCommit) {
    throw 'The published commit differs from the validated installation target.'
}
$existingBranch = @(Invoke-CheckedGit -GitArguments @('branch', '--list', $desktopBranch))
if ($existingBranch.Count -ne 0) {
    throw 'The planned desktop branch already exists; preserve it and inspect manually.'
}

New-Item -ItemType Directory -Path $backupDirectory -Force | Out-Null
Copy-Item -LiteralPath $installedBinary -Destination $backupBinary -ErrorAction Stop
if ((Get-LiteralSha256 -Path $backupBinary) -ne $baselineBinarySha256) {
    throw 'The rollback executable did not verify.'
}

Invoke-CheckedGit -GitArguments @('switch', '--create', $desktopBranch, '--track', ('origin/' + $remoteBranch)) | Out-Null
$installedHead = (Invoke-CheckedGit -GitArguments @('rev-parse', 'HEAD')).Trim()
if ($installedHead -ne $ValidatedCommit) {
    throw 'The desktop branch did not reach the validated published commit.'
}

# Native PowerShell copy is intentional: Python os.replace previously returned
# WinError 5 for this executable despite normal ownership and no running engine.
Copy-Item -LiteralPath $candidateBinary -Destination $installedBinary -Force -ErrorAction Stop
if ((Get-LiteralSha256 -Path $installedBinary) -ne $ValidatedBinarySha256) {
    throw 'The installed executable did not verify.'
}

# Repair the one preexisting CRLF fixture drift identified in protection-before.
# Its JSON content is unchanged; this restores the exact baseline LF bytes.
Copy-Item -LiteralPath (Join-Path $ResearchRoot $fixtureRelative) `
    -Destination (Join-Path $PrimaryRoot $fixtureRelative) -Force -ErrorAction Stop

& $python $checker --installed-sha256 $ValidatedBinarySha256 --primary-head $ValidatedCommit --output $protectionAfter
if ($LASTEXITCODE -ne 0) {
    throw 'Post-install source/model/state preservation checks failed. Keep the backup and inspect the report.'
}
$statusAfter = @(Invoke-CheckedGit -GitArguments @('status', '--porcelain', '--untracked-files=normal'))
if ($statusAfter.Count -ne 0) {
    throw 'The installed checkout is not clean after updating and restoring fixture bytes.'
}
$upstream = (Invoke-CheckedGit -GitArguments @('rev-parse', '--abbrev-ref', '--symbolic-full-name', '@{upstream}')).Trim()
if ($upstream -ne ('origin/' + $remoteBranch)) {
    throw 'The new desktop branch does not track the published optimization branch.'
}

$report = [ordered]@{
    checked_utc = [DateTime]::UtcNow.ToString('o')
    previous_commit = $beforeHead
    previous_branch = $beforeBranch
    installed_commit = $installedHead
    remote_commit = $remoteHead
    installed_branch = $desktopBranch
    upstream = $upstream
    baseline_binary_sha256 = $baselineBinarySha256
    installed_binary_sha256 = $ValidatedBinarySha256
    rollback_binary = 'build/v063-before-neural-inference-opt/regicide.exe'
    learning_state_copied_or_modified = $false
    learning_attempt_advanced = $false
    model_promoted = $false
    protection_report = 'protection-after-install.json'
    repaired_preexisting_fixture_line_endings = 'tests/data/neural_diagnostic_positions_v1.json'
    working_tree_clean = $true
    manual_gui_game_claimed = $false
}
$report | ConvertTo-Json -Depth 6 | Set-Content -LiteralPath $resultPath -Encoding UTF8
$report | ConvertTo-Json -Depth 6
