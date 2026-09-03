# One-shot quality gate: lint (autofix), format, re-lint, test.
$ErrorActionPreference = "Stop"
Set-Location (Split-Path -Parent $PSScriptRoot)

ruff check --fix fungi tests
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
ruff format fungi tests
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
ruff check fungi tests
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
python -m pytest -q
exit $LASTEXITCODE
