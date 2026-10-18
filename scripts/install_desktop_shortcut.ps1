param(
    [string]$RepositoryPath = (Split-Path -Parent $PSScriptRoot),
    [ValidateSet('Default', 'Neural', 'Learning')][string]$Mode = 'Default'
)

$ErrorActionPreference = 'Stop'
$repo = (Resolve-Path -LiteralPath $RepositoryPath).Path
$launcherPath = Join-Path $repo 'scripts/play_godot.ps1'
if ($Mode -eq 'Learning') { $launcherPath = Join-Path $repo 'scripts/learn.ps1' }
$iconPath = Join-Path $repo 'assets/regicide.ico'
foreach ($required in @($launcherPath, $iconPath)) {
    if (!(Test-Path -LiteralPath $required -PathType Leaf)) {
        throw "Required Regicide file is missing: $required"
    }
}
$desktopPath = [Environment]::GetFolderPath('DesktopDirectory')
if (!$desktopPath -or !(Test-Path -LiteralPath $desktopPath -PathType Container)) {
    throw 'Could not locate the Windows desktop directory.'
}
$shortcutName = switch ($Mode) {
    'Neural' { 'Regicide - Neural Experiment.lnk' }
    'Learning' { 'Regicide - Collect and Learn.lnk' }
    default { 'Regicide.lnk' }
}
$linkPath = Join-Path $desktopPath $shortcutName
$powershellPath = Join-Path $env:SystemRoot 'System32/WindowsPowerShell/v1.0/powershell.exe'
$arguments = '-NoLogo -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File "' + $launcherPath + '"'
if ($Mode -eq 'Neural') {
    $modelPath = Join-Path $repo 'models/regicide-small-v1.nn'
    if (!(Test-Path -LiteralPath $modelPath -PathType Leaf)) { throw 'The experiment model is missing.' }
    $arguments += ' -NeuralModel "' + $modelPath + '"'
}
if ($Mode -eq 'Learning') {
    $arguments = '-NoLogo -NoProfile -NoExit -ExecutionPolicy Bypass -File "' + $launcherPath + '"'
}
$shell = New-Object -ComObject WScript.Shell
if (Test-Path -LiteralPath $linkPath) {
    $existing = $shell.CreateShortcut($linkPath)
    if ($existing.Arguments -ne $arguments) {
        throw 'A different Regicide shortcut already exists on the desktop. It was preserved.'
    }
}
$link = $shell.CreateShortcut($linkPath)
$link.TargetPath = $powershellPath
$link.Arguments = $arguments
$link.WorkingDirectory = $repo
$link.IconLocation = $iconPath + ',0'
$link.Description = 'Regicide chess: undo, handicap moves, and board perspectives'
$link.WindowStyle = 1
$link.Save()
$verified = $shell.CreateShortcut($linkPath)
if ($verified.TargetPath -ne $powershellPath -or $verified.Arguments -ne $arguments -or
    $verified.WorkingDirectory -ne $repo -or $verified.IconLocation -ne ($iconPath + ',0')) {
    throw 'The saved desktop shortcut did not match the Regicide launcher.'
}
[pscustomobject]@{
    shortcut = $linkPath
    repository = $repo
    launcher = $launcherPath
    icon = $iconPath
} | ConvertTo-Json
