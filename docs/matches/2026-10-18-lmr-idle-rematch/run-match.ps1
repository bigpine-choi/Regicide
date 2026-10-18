$ErrorActionPreference='Stop'
$repoRoot=Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
Set-Location -LiteralPath $repoRoot
$pythonPath='$BASELINE_REPO\.venv-tests\Scripts\python.exe'
& $pythonPath scripts/lmr_experiment.py match --telemetry build/lmr-idle-rematch-20261018-attempt2/match-200ms-lmr.json -- build/lmr-validation/regicide-v057.exe build/regicide.exe --pgn build/lmr-idle-rematch-20261018-attempt2/match-200ms.pgn --movetime 200 --max-plies 300 --draw-policy claim --openings tests/data/lmr_openings_2026-10-18.json --baseline-commit 5b5b5deedbef96133836609ec00229c7a548a15a --candidate-commit 157305d804bc1828641a7e8d9b9b793b7746bcc3 --baseline-options '{"Hash":16,"Move Overhead":20}' --candidate-options '{"Hash":16,"Move Overhead":20,"UseLMR":true}'
exit $LASTEXITCODE

