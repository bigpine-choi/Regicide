param([Parameter(Mandatory = $true)][string]$RequestPath)

# Desktop SAPI is independent of the OneCore voices exposed by recent Godot.
# This helper only writes a WAV; it never plays sound or changes installed voices.
$ErrorActionPreference = 'Stop'
$requestFile = [IO.Path]::GetFullPath($RequestPath)
$folder = [IO.Path]::GetDirectoryName($requestFile)
$responseFile = [IO.Path]::Combine($folder, 'response.json')
$waveFile = [IO.Path]::Combine($folder, 'speech.wav')
$cancelFile = [IO.Path]::Combine($folder, 'cancel')
$synth = $null
$requestId = ''
$result = $null
$utf8 = New-Object Text.UTF8Encoding($false)

try {
    $request = [IO.File]::ReadAllText($requestFile, [Text.Encoding]::UTF8) | ConvertFrom-Json
    $requestId = [string]$request.request_id
    if ($requestId -notmatch '^[A-Za-z0-9_-]{1,100}$') { throw 'Invalid voice request identifier.' }
    if (Test-Path -LiteralPath $cancelFile) { throw 'cancelled' }
    Add-Type -AssemblyName System.Speech
    $synth = New-Object System.Speech.Synthesis.SpeechSynthesizer
    $voices = @($synth.GetInstalledVoices() | Where-Object {
        $_.Enabled -and $_.VoiceInfo.Culture.TwoLetterISOLanguageName -eq 'en'
    } | ForEach-Object {
        @{ id = $_.VoiceInfo.Name; name = $_.VoiceInfo.Name; language = $_.VoiceInfo.Culture.Name }
    })
    if ($request.action -eq 'voices') {
        $result = @{ ok = $true; request_id = $requestId; voices = $voices }
    } elseif ($request.action -eq 'synthesize') {
        $text = [string]$request.text
        $voiceId = [string]$request.voice_id
        if ([string]::IsNullOrWhiteSpace($text) -or $text.Length -gt 4000) { throw 'Speech text must contain 1 to 4000 characters.' }
        if (-not ($voices | Where-Object { $_.id -eq $voiceId })) { throw 'The selected English Desktop voice is not installed.' }
        $synth.SelectVoice($voiceId)
        $synth.Rate = 0
        $synth.Volume = 100
        $synth.SetOutputToWaveFile($waveFile)
        $prompt = $synth.SpeakAsync($text)
        $deadline = [DateTime]::UtcNow.AddSeconds(15)
        while (-not $prompt.IsCompleted) {
            if ((Test-Path -LiteralPath $cancelFile) -or [DateTime]::UtcNow -gt $deadline) {
                $synth.SpeakAsyncCancelAll()
                throw 'cancelled or speech synthesis timed out'
            }
            Start-Sleep -Milliseconds 20
        }
        $synth.SetOutputToNull()
        if (Test-Path -LiteralPath $cancelFile) { throw 'cancelled' }
        $result = @{ ok = $true; request_id = $requestId; voice_id = $voiceId; output = 'speech.wav' }
    } else { throw 'Unknown voice action.' }
} catch {
    $result = @{ ok = $false; request_id = $requestId; error = $_.Exception.Message }
} finally {
    if ($null -ne $synth) { $synth.Dispose() }
}

# Atomic publication prevents the client from parsing an incomplete response.
$temporaryResponse = [IO.Path]::Combine($folder, 'response.tmp')
[IO.File]::WriteAllText($temporaryResponse, ($result | ConvertTo-Json -Depth 5 -Compress), $utf8)
[IO.File]::Move($temporaryResponse, $responseFile)
