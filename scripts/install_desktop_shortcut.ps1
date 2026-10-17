param(
    [string]$RepositoryPath = (Split-Path -Parent $PSScriptRoot)
)

$ErrorActionPreference = 'Stop'
$repo = (Resolve-Path -LiteralPath $RepositoryPath).Path
$launcherPath = Join-Path $repo 'scripts/play_godot.ps1'
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
$linkPath = Join-Path $desktopPath 'Regicide.lnk'
$powershellPath = Join-Path $env:SystemRoot 'System32/WindowsPowerShell/v1.0/powershell.exe'
$arguments = '-NoLogo -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File "' + $launcherPath + '"'
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
