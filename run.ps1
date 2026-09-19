# Run the Windows Voice Assistant from any directory.
#
#   .\run.ps1              # start the assistant (real models + microphone)
#   .\run.ps1 --check      # load every model, report, and exit
#   .\run.ps1 --stub-audio # run with model-free stubs
#
# This script sets the working directory to the repo root first, because the
# assistant resolves `config/config.yaml` and `models/` relative to the CWD.

[CmdletBinding()]
param(
    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]] $Args
)

$repoRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $repoRoot

$python = (Get-Command python).Source
Write-Host "repo   : $repoRoot"
Write-Host "python : $python"
Write-Host ""

# NOTE: the assistant writes informational warnings to stderr. PowerShell
# would surface those as error records under $ErrorActionPreference='Stop',
# so run the native command directly and propagate only its exit code.
& $python -m winvoice @Args
exit $LASTEXITCODE
