param(
    [Parameter(Mandatory = $true)]
    [ValidatePattern('^[0-9a-fA-F]{40}$')]
    [string]$ValidatedCommit,
    [Parameter(Mandatory = $true)]
    [ValidatePattern('^[0-9a-fA-F]{40}$')]
    [string]$ValidatedStudySourceCommit,
    [string]$PrimaryRoot = '<installed>',
    [string]$ResearchRoot = '<research>'
)

# PREPARED ONLY. Run only after attempt 6 and authenticated remote validation are complete.
# No reset, deletion, ACL changes, old-branch movement, or model promotion occurs.
$ErrorActionPreference = 'Stop'
$baselineCommit = '527289e74dbdfb28a98625ef961c7470e9c78ff9'
$studySourceCommit = $ValidatedStudySourceCommit.ToLowerInvariant()
$baselineBinarySha256 = 'ff63e5c1bb1aaa863f6bdaa19bfaf61c660350b88a702916b40372249f22f4b1'
$candidateBinarySha256 = 'e2b218e7b22f3f4f166d999f011ed08cb66120cc6a6f3265448537d72b8254e3'
$baselineBranch = 'desktop/neural-inference-opt'
$desktopBranch = 'desktop/endgame-learning-scale'
$remoteBranch = 'feat/endgame-learning-scale'
$ValidatedCommit = $ValidatedCommit.ToLowerInvariant()
$PrimaryRoot = (Resolve-Path -LiteralPath $PrimaryRoot).Path
$ResearchRoot = (Resolve-Path -LiteralPath $ResearchRoot).Path
$buildDirectory = (Resolve-Path -LiteralPath (Join-Path $PrimaryRoot 'build')).Path
$researchBuild = (Resolve-Path -LiteralPath (Join-Path $ResearchRoot 'build')).Path
$installedBinary = Join-Path $buildDirectory 'regicide.exe'
$candidateBinary = Join-Path $ResearchRoot 'build-neural-v5\regicide.exe'
$installedState = Join-Path $buildDirectory 'learning-cycle'
$researchState = Join-Path $researchBuild 'learning-cycle'
$stagedState = [IO.Path]::GetFullPath((Join-Path $buildDirectory 'learning-cycle-endgame-staged'))
$backupDirectory = [IO.Path]::GetFullPath((Join-Path $buildDirectory 'v064-before-endgame-learning-scale'))
$backupBinary = Join-Path $backupDirectory 'regicide.exe'
$backupState = Join-Path $backupDirectory 'learning-cycle'
$auditDirectory = Join-Path $researchBuild 'endgame-learning-scale'
$protectionPath = Join-Path $auditDirectory 'protection-before.json'
$criterionPath = Join-Path $auditDirectory 'criterion-preservation.json'
$resultPath = Join-Path $auditDirectory 'installation.json'
$journalPath = Join-Path $auditDirectory 'installation-running.json'
$mutableOldFiles = @('catalog.json', 'latest.json', 'cycle.lock')

function Get-LiteralSha256 {
    param([string]$Path)
    return (Get-FileHash -LiteralPath $Path -Algorithm SHA256).Hash.ToLowerInvariant()
}

function Invoke-CheckedGit {
    param([string]$Root, [string[]]$GitArguments)
    $result = & git -C $Root @GitArguments
    if ($LASTEXITCODE -ne 0) { throw ('Git failed: ' + ($GitArguments -join ' ')) }
    return $result
}

function Assert-Contained {
    param([string]$Parent, [string]$Target)
    $parentFull = [IO.Path]::GetFullPath($Parent).TrimEnd([char[]]'\/')
    $targetFull = [IO.Path]::GetFullPath($Target)
    $prefix = $parentFull + [IO.Path]::DirectorySeparatorChar
    if (-not $targetFull.StartsWith($prefix, [StringComparison]::OrdinalIgnoreCase)) {
        throw ('Path escaped the verified parent: ' + $targetFull)
    }
    return $targetFull
}

function Assert-NoReparsePoints {
    param([string]$Directory)
    $rootItem = Get-Item -LiteralPath $Directory -Force
    $items = @($rootItem) + @(Get-ChildItem -LiteralPath $Directory -Force -Recurse)
    foreach ($item in $items) {
        if (($item.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) {
            throw ('State copy refuses symbolic links or junctions: ' + $item.FullName)
        }
        $null = Assert-Contained -Parent $Directory -Target (Join-Path $item.FullName 'containment-probe')
    }
}

function Get-StateHashes {
    param([string]$Directory)
    $resolved = (Resolve-Path -LiteralPath $Directory).Path.TrimEnd([char[]]'\/')
    $prefix = $resolved + [IO.Path]::DirectorySeparatorChar
    $hashes = [ordered]@{}
    Assert-NoReparsePoints -Directory $resolved
    foreach ($file in @(Get-ChildItem -LiteralPath $resolved -File -Force -Recurse | Sort-Object FullName)) {
        $full = Assert-Contained -Parent $resolved -Target $file.FullName
        $relative = $full.Substring($prefix.Length).Replace('\', '/')
        $hashes[$relative] = Get-LiteralSha256 -Path $full
    }
    return $hashes
}

function Assert-HashMapsEqual {
    param($Expected, $Actual, [string]$Label)
    if ($Expected.Count -ne $Actual.Count) { throw ($Label + ' file count changed.') }
    foreach ($key in $Expected.Keys) {
        if (-not $Actual.Contains($key) -or $Actual[$key] -ne $Expected[$key]) {
            throw ($Label + ' byte mismatch: ' + $key)
        }
    }
}

function Assert-OldFilesPreserved {
    param($ExpectedObject, $ActualHashes, [bool]$AllowMutableTop, [string]$Label)
    foreach ($property in $ExpectedObject.PSObject.Properties) {
        if ($AllowMutableTop -and $mutableOldFiles -contains $property.Name) { continue }
        if (-not $ActualHashes.Contains($property.Name) -or $ActualHashes[$property.Name] -ne $property.Value) {
            throw ($Label + ' historical file changed: ' + $property.Name)
        }
    }
}

function Assert-NoRegicide {
    if (@(Get-Process -Name 'regicide' -ErrorAction SilentlyContinue).Count -ne 0) {
        throw 'Regicide is running; finish measurements and close it before installation.'
    }
}

function Assert-StateNotLocked {
    param([string]$Directory)
    $lockPath = Join-Path $Directory 'cycle.lock'
    $stream = [IO.File]::Open($lockPath, [IO.FileMode]::Open, [IO.FileAccess]::ReadWrite, [IO.FileShare]::None)
    $stream.Dispose()
}

foreach ($target in @($installedState, $stagedState, $backupDirectory, $backupBinary, $backupState)) {
    $null = Assert-Contained -Parent $buildDirectory -Target $target
}
$null = Assert-Contained -Parent $researchBuild -Target $researchState
$null = Assert-Contained -Parent $ResearchRoot -Target $candidateBinary
Assert-NoRegicide
foreach ($target in @($backupDirectory, $stagedState, $resultPath, $journalPath)) {
    if (Test-Path -LiteralPath $target) { throw ('Prior installation evidence/staging already exists: ' + $target) }
}
Assert-StateNotLocked -Directory $installedState
Assert-StateNotLocked -Directory $researchState
$protection = Get-Content -LiteralPath $protectionPath -Raw -Encoding UTF8 | ConvertFrom-Json
if ($protection.baseline_commit -ne $baselineCommit -or $protection.old_binary_sha256 -ne $baselineBinarySha256) {
    throw 'The initial preservation record differs from the expected v0.6.4 baseline.'
}
$beforeHead = (Invoke-CheckedGit -Root $PrimaryRoot -GitArguments @('rev-parse', 'HEAD')).Trim()
$beforeBranch = (Invoke-CheckedGit -Root $PrimaryRoot -GitArguments @('branch', '--show-current')).Trim()
if ($beforeHead -ne $baselineCommit -or $beforeBranch -ne $baselineBranch -or
    @(Invoke-CheckedGit -Root $PrimaryRoot -GitArguments @('status', '--porcelain', '--untracked-files=normal')).Count -ne 0) {
    throw 'The installed checkout is not the expected clean v0.6.4 desktop baseline.'
}
$researchHead = (Invoke-CheckedGit -Root $ResearchRoot -GitArguments @('rev-parse', 'HEAD')).Trim()
if ($researchHead -ne $ValidatedCommit -or
    @(Invoke-CheckedGit -Root $ResearchRoot -GitArguments @('status', '--porcelain', '--untracked-files=normal')).Count -ne 0) {
    throw 'The research checkout is not the exact clean validated publication commit.'
}
Invoke-CheckedGit -Root $ResearchRoot -GitArguments @('merge-base', '--is-ancestor', $studySourceCommit, $ValidatedCommit) | Out-Null
if ((Get-LiteralSha256 -Path $installedBinary) -ne $baselineBinarySha256 -or
    (Get-LiteralSha256 -Path $candidateBinary) -ne $candidateBinarySha256) {
    throw 'Installed or candidate executable SHA256 changed.'
}
$oldStateHashes = Get-StateHashes -Directory $installedState
if ($oldStateHashes.Count -ne 105 -or @($protection.states.installed.PSObject.Properties).Count -ne 105) {
    throw 'The original installed learning state is not the expected105-file snapshot.'
}
Assert-OldFilesPreserved -ExpectedObject $protection.states.installed -ActualHashes $oldStateHashes -AllowMutableTop $false -Label 'Installed baseline'
$newStateHashes = Get-StateHashes -Directory $researchState
Assert-OldFilesPreserved -ExpectedObject $protection.states.research -ActualHashes $newStateHashes -AllowMutableTop $true -Label 'Research state'
$criterion = Get-Content -LiteralPath $criterionPath -Raw -Encoding UTF8 | ConvertFrom-Json
if ($criterion.status -ne 'passed' -or $criterion.baseline_commit -ne $baselineCommit -or
    $criterion.current_source_commit -ne $studySourceCommit -or
    $criterion.learning_arena_whole_sha256 -ne (Get-LiteralSha256 -Path (Join-Path $ResearchRoot 'scripts\learning_arena.py'))) {
    throw 'The criterion preservation audit is missing, failed, or belongs to different study source bytes.'
}
foreach ($functionName in @('promotion_decision', 'verify_match_records')) {
    $entry = $criterion.functions.$functionName
    if ($null -eq $entry -or $entry.identical -isnot [bool] -or -not $entry.identical -or
        $entry.baseline_sha256 -notmatch '^[0-9a-f]{64}$' -or
        $entry.current_sha256 -ne $entry.baseline_sha256) {
        throw ('The unchanged full promotion criterion was not verified: ' + $functionName)
    }
}
$registryExpected = $protection.protected.'scripts/learning_registry.py'
if ($criterion.registry.identical -isnot [bool] -or -not $criterion.registry.identical -or
    $criterion.registry.baseline_sha256 -ne $registryExpected -or
    $criterion.registry.current_sha256 -ne $registryExpected -or
    (Get-LiteralSha256 -Path (Join-Path $ResearchRoot 'scripts\learning_registry.py')) -ne $registryExpected) {
    throw 'The unchanged whole champion registry was not verified by the criterion audit.'
}
if (@($protection.protected.PSObject.Properties).Count -ne 10) {
    throw 'The protected engine/promotion source inventory no longer has its original ten entries.'
}
$protectedSourceAudit = @()
$strictUtf8 = [Text.UTF8Encoding]::new($false, $true)
foreach ($property in $protection.protected.PSObject.Properties) {
    # The requested rejection-only futility check changed this file. Its two
    # full promotion functions are bound above; the other nine files remain exact.
    if ($property.Name -eq 'scripts/learning_arena.py') { continue }
    $researchSource = Join-Path $ResearchRoot $property.Name
    $primarySource = Join-Path $PrimaryRoot $property.Name
    if ((Get-LiteralSha256 -Path $researchSource) -ne $property.Value) {
        throw ('Frozen research source bytes changed: ' + $property.Name)
    }
    $expectedBlob = (Invoke-CheckedGit -Root $PrimaryRoot -GitArguments @('rev-parse', ($baselineCommit + ':' + $property.Name))).Trim()
    $publishedBlob = (Invoke-CheckedGit -Root $ResearchRoot -GitArguments @('rev-parse', ($ValidatedCommit + ':' + $property.Name))).Trim()
    $researchText = $strictUtf8.GetString([IO.File]::ReadAllBytes($researchSource)).Replace("`r`n", "`n")
    $primaryText = $strictUtf8.GetString([IO.File]::ReadAllBytes($primarySource)).Replace("`r`n", "`n")
    if ($expectedBlob -ne $publishedBlob -or $researchText -cne $primaryText) {
        throw ('Protected source differs beyond CRLF/LF line endings: ' + $property.Name)
    }
    $protectedSourceAudit += [ordered]@{ path = $property.Name; git_blob = $expectedBlob;
        research_raw_sha256 = $property.Value; primary_raw_sha256 = (Get-LiteralSha256 -Path $primarySource);
        identical_committed_bytes = $true; working_content_identical_except_crlf_lf = $true }
}
$catalog = Get-Content -LiteralPath (Join-Path $researchState 'catalog.json') -Raw -Encoding UTF8 | ConvertFrom-Json
$latest = Get-Content -LiteralPath (Join-Path $researchState 'latest.json') -Raw -Encoding UTF8 | ConvertFrom-Json
if ($catalog.attempts_started -ne 6 -or @($catalog.runs).Count -ne 6 -or
    $latest.status -ne 'complete' -or $latest.attempt_index -ne 6 -or $latest.run -ne $catalog.runs[-1]) {
    throw 'Installation requires the completed sixth attempt and its unchanged persistent counter.'
}
$run = Assert-Contained -Parent $researchState -Target (Join-Path $researchState $latest.run)
$cycle = Get-Content -LiteralPath (Join-Path $run 'cycle.json') -Raw -Encoding UTF8 | ConvertFrom-Json
$arena = Get-Content -LiteralPath (Join-Path $run 'arena\arena.json') -Raw -Encoding UTF8 | ConvertFrom-Json
if ($cycle.status -ne 'complete' -or $cycle.attempt_index -ne 6 -or
    $cycle.study_schema -ne 'regicide.endgame.study.v1' -or $cycle.checkout.commit -ne $studySourceCommit -or
    $cycle.checkout.status_porcelain -ne '' -or
    $cycle.binary_sha256 -ne $candidateBinarySha256 -or
    @('complete', 'rejected_early_futile') -notcontains $arena.status -or
    $arena.attempt_index -ne 6 -or $arena.binary_sha256 -ne $candidateBinarySha256) {
    throw 'The completed attempt/arena engine identity does not match the validated study.'
}
if ($cycle.validation_profile -ne 'fixed-nodes-remote-time-v1' -or
    $cycle.configuration.validation_location -ne 'github-actions-windows' -or
    $cycle.remote_import.audit -ne 'remote-import.json' -or
    $cycle.remote_import.original_evidence -ne 'remote-evidence') {
    throw 'The sixth study does not identify its authenticated remote validation evidence.'
}
$remoteAuditPath = Join-Path $run 'remote-import.json'
$remoteAudit = Get-Content -LiteralPath $remoteAuditPath -Raw -Encoding UTF8 | ConvertFrom-Json
$originalArenaPath = Join-Path $run 'remote-evidence\arena\arena.json'
if ($remoteAudit.schema -ne 'regicide.learning.remote-import.v1' -or
    $remoteAudit.status -ne 'verified_evidence' -or
    $remoteAudit.request_sha256 -ne $cycle.remote_request.sha256 -or
    $remoteAudit.localized_arena_json_sha256 -ne (Get-LiteralSha256 -Path (Join-Path $run 'arena\arena.json')) -or
    $remoteAudit.original_arena_json_sha256 -ne (Get-LiteralSha256 -Path $originalArenaPath) -or
    [string]$remoteAudit.workflow_receipt.run_id -ne [string]$cycle.remote_import.receipt_run_id -or
    $remoteAudit.workflow_receipt.result_commit -ne $cycle.remote_import.result_commit -or
    $remoteAudit.workflow_receipt.status -ne 'completed' -or $remoteAudit.workflow_receipt.conclusion -ne 'success') {
    throw 'Authenticated remote validation audit differs from its preserved arena bytes or workflow receipt.'
}
$championPath = Join-Path $researchState 'champion.json'
$championSha = $null
if (Test-Path -LiteralPath $championPath) {
    $champion = Get-Content -LiteralPath $championPath -Raw -Encoding UTF8 | ConvertFrom-Json
    $modelPath = Assert-Contained -Parent $researchState -Target (Join-Path $researchState $champion.relative_model_path)
    if ($champion.schema -ne 'regicide.learning.champion.v1' -or $champion.engine_sha256 -ne $candidateBinarySha256 -or
        (Get-LiteralSha256 -Path $modelPath) -ne $champion.model_sha256 -or
        $cycle.selected_model_sha256 -ne $champion.model_sha256) {
        throw 'The existing research champion registry does not match the completed study.'
    }
    $championSha = $champion.model_sha256
} elseif ($null -ne $cycle.selected_model_sha256 -or $cycle.adopted) {
    throw 'Completed study claims a selected model without its existing registry.'
}
if ([bool]$cycle.adopted -ne [bool]$arena.promotion_passed) {
    throw 'Study adoption disagrees with its completed unchanged arena gate.'
}
Invoke-CheckedGit -Root $PrimaryRoot -GitArguments @('fetch', '--quiet', 'origin',
    ('refs/heads/' + $remoteBranch + ':refs/remotes/origin/' + $remoteBranch)) | Out-Null
$remoteHead = (Invoke-CheckedGit -Root $PrimaryRoot -GitArguments @('rev-parse', ('refs/remotes/origin/' + $remoteBranch))).Trim()
if ($remoteHead -ne $ValidatedCommit) { throw 'Published branch is not the validated publication commit.' }
if (@(Invoke-CheckedGit -Root $PrimaryRoot -GitArguments @('branch', '--list', $desktopBranch)).Count -ne 0) {
    throw 'The planned desktop branch already exists; preserve it and inspect manually.'
}

$journal = [ordered]@{ status = 'installing'; started_utc = [DateTime]::UtcNow.ToString('o');
    target_commit = $ValidatedCommit; previous_commit = $beforeHead; previous_branch = $beforeBranch;
    stage = 'preflight-passed'; completed_study_run = $latest.run; error = $null }
function Save-Journal {
    $journal | ConvertTo-Json -Depth 5 | Set-Content -LiteralPath $journalPath -Encoding UTF8
}
Save-Journal
try {
    # Stage a complete separate copy before replacing either live artifact.
    $null = Assert-Contained -Parent $buildDirectory -Target $stagedState
    Copy-Item -LiteralPath $researchState -Destination $stagedState -Recurse -ErrorAction Stop
    $stagedHashes = Get-StateHashes -Directory $stagedState
    Assert-HashMapsEqual -Expected $newStateHashes -Actual $stagedHashes -Label 'Staged learning state'
    Assert-HashMapsEqual -Expected $newStateHashes -Actual (Get-StateHashes -Directory $researchState) -Label 'Research state during copy'
    New-Item -ItemType Directory -Path $backupDirectory -ErrorAction Stop | Out-Null
    Copy-Item -LiteralPath $installedBinary -Destination $backupBinary -ErrorAction Stop
    if ((Get-LiteralSha256 -Path $backupBinary) -ne $baselineBinarySha256) { throw 'Rollback executable mismatch.' }
    $journal.stage = 'state-staged-and-rollback-executable-verified'; Save-Journal
    Assert-NoRegicide
    Assert-StateNotLocked -Directory $installedState
    Assert-StateNotLocked -Directory $researchState
    Assert-HashMapsEqual -Expected $oldStateHashes -Actual (Get-StateHashes -Directory $installedState) -Label 'Installed state before move'
    Invoke-CheckedGit -Root $PrimaryRoot -GitArguments @('switch', '--create', $desktopBranch, '--track', ('origin/' + $remoteBranch)) | Out-Null
    # Both recursive directory moves use native PowerShell with fresh absolute
    # containment checks. Existing state remains recoverable in the backup.
    $null = Assert-Contained -Parent $buildDirectory -Target $installedState
    $null = Assert-Contained -Parent $buildDirectory -Target $backupState
    Move-Item -LiteralPath $installedState -Destination $backupState -ErrorAction Stop
    $journal.stage = 'old-state-moved-to-rollback'; Save-Journal
    $null = Assert-Contained -Parent $buildDirectory -Target $stagedState
    $null = Assert-Contained -Parent $buildDirectory -Target $installedState
    Move-Item -LiteralPath $stagedState -Destination $installedState -ErrorAction Stop
    $journal.stage = 'new-state-installed'; Save-Journal
    # Native copy deliberately avoids the earlier Python os.replace WinError5.
    Assert-NoRegicide
    Copy-Item -LiteralPath $candidateBinary -Destination $installedBinary -Force -ErrorAction Stop
    $journal.stage = 'validated-executable-installed'; Save-Journal
    if ((Get-LiteralSha256 -Path $installedBinary) -ne $candidateBinarySha256) { throw 'Installed executable mismatch.' }
    $installedHashes = Get-StateHashes -Directory $installedState
    Assert-HashMapsEqual -Expected $newStateHashes -Actual $installedHashes -Label 'Installed complete learning state'
    Assert-HashMapsEqual -Expected $oldStateHashes -Actual (Get-StateHashes -Directory $backupState) -Label 'Rollback105-file learning state'
    Assert-HashMapsEqual -Expected $newStateHashes -Actual (Get-StateHashes -Directory $researchState) -Label 'Final research learning state'
    $installedHead = (Invoke-CheckedGit -Root $PrimaryRoot -GitArguments @('rev-parse', 'HEAD')).Trim()
    $upstream = (Invoke-CheckedGit -Root $PrimaryRoot -GitArguments @('rev-parse', '--abbrev-ref', '--symbolic-full-name', '@{upstream}')).Trim()
    $rollbackBranchHead = (Invoke-CheckedGit -Root $PrimaryRoot -GitArguments @('rev-parse', ('refs/heads/' + $baselineBranch))).Trim()
    $remoteLine = (Invoke-CheckedGit -Root $PrimaryRoot -GitArguments @('ls-remote', 'origin', ('refs/heads/' + $remoteBranch))).Trim()
    $remoteVerified = ($remoteLine -split '\s+')[0]
    if ($installedHead -ne $ValidatedCommit -or $remoteVerified -ne $ValidatedCommit -or
        $rollbackBranchHead -ne $baselineCommit -or $upstream -ne ('origin/' + $remoteBranch) -or
        @(Invoke-CheckedGit -Root $PrimaryRoot -GitArguments @('status', '--porcelain', '--untracked-files=normal')).Count -ne 0) {
        throw 'Final source/remote/upstream/rollback-branch/clean-tree verification failed.'
    }
    $result = [ordered]@{
        status = 'complete'; checked_utc = [DateTime]::UtcNow.ToString('o');
        previous_commit = $beforeHead; previous_branch = $beforeBranch; installed_commit = $installedHead;
        research_commit = $researchHead; remote_commit = $remoteVerified; installed_branch = $desktopBranch; upstream = $upstream;
        study_runtime_source_commit = $studySourceCommit; completed_study_run = $latest.run; learning_attempt_index = 6;
        criterion_preservation_sha256 = (Get-LiteralSha256 -Path $criterionPath);
        remote_import_sha256 = (Get-LiteralSha256 -Path $remoteAuditPath);
        arena_status = $arena.status; remote_workflow_run_id = [string]$cycle.remote_import.receipt_run_id;
        baseline_binary_sha256 = $baselineBinarySha256; installed_binary_sha256 = $candidateBinarySha256;
        rollback_binary = 'build/v064-before-endgame-learning-scale/regicide.exe';
        rollback_learning_state = 'build/v064-before-endgame-learning-scale/learning-cycle';
        rollback_state_files = $oldStateHashes.Count; installed_state_files = $installedHashes.Count;
        historical_immutable_files_preserved = (@($protection.states.research.PSObject.Properties).Count - $mutableOldFiles.Count);
        protected_source_verification = $protectedSourceAudit;
        installed_state_all_sha256 = $installedHashes; rollback_state_all_sha256 = $oldStateHashes;
        learning_attempt_advanced_by_installer = $false; model_promoted_by_installer = $false;
        model_adopted_by_completed_study = [bool]$cycle.adopted; selected_model_sha256 = $championSha;
        working_tree_clean = $true; rollback_branch_preserved = $true; manual_gui_game_claimed = $false
    }
    $result | ConvertTo-Json -Depth 8 | Set-Content -LiteralPath $resultPath -Encoding UTF8
    $journal.status = 'complete'; $journal.stage = 'all-hashes-and-source-verified'; Save-Journal
    [pscustomobject]$result | Select-Object status, installed_commit, installed_binary_sha256, installed_state_files, rollback_state_files, selected_model_sha256 | ConvertTo-Json -Depth 3
} catch {
    $journal.status = 'failed'; $journal.error = $_.Exception.Message; Save-Journal
    throw
}
