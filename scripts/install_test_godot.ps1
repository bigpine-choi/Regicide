# CI-only tool bootstrap. The desktop launcher uses an already installed Godot.
$ErrorActionPreference = 'Stop'
$version = '4.7.2-stable'
$directory = Join-Path (Split-Path -Parent $PSScriptRoot) 'build/godot-tools'
if ($IsWindows) {
    $asset = "Godot_v${version}_win64.exe.zip"
    $expected = '731980f9608d61333e5baf54a2ef17210acc7a538446c0cb9969f002aca1e953'
    $executable = "Godot_v${version}_win64_console.exe"
} elseif ($IsLinux) {
    $asset = "Godot_v${version}_linux.x86_64.zip"
    $expected = 'cadd3204e728a35d3f13adb7fd0d7902636b79f6b95c40c265eb73b6c35329e4'
    $executable = "Godot_v${version}_linux.x86_64"
} else {
    throw 'This CI bootstrap supports Windows and Linux x86-64 runners.'
}
New-Item -ItemType Directory -Path $directory -Force | Out-Null
$archive = Join-Path $directory $asset
Invoke-WebRequest -Uri "https://github.com/godotengine/godot-builds/releases/download/$version/$asset" -OutFile $archive
if ((Get-FileHash -LiteralPath $archive -Algorithm SHA256).Hash.ToLowerInvariant() -ne $expected) {
    throw 'Official Godot archive checksum mismatch.'
}
Expand-Archive -LiteralPath $archive -DestinationPath $directory -Force
$godot = Join-Path $directory $executable
if ($IsLinux) {
    & chmod +x $godot
    if ($LASTEXITCODE -ne 0) { throw 'Could not make Godot executable.' }
}
"REGICIDE_TEST_GODOT=$godot" | Out-File -LiteralPath $env:GITHUB_ENV -Encoding utf8 -Append
& $godot --headless --version
if ($LASTEXITCODE -ne 0) { throw 'Godot version probe failed.' }
