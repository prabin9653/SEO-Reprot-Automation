<#
.SYNOPSIS
  One-time install of the SEO report runner on a team member's computer.

.DESCRIPTION
  Run from a normal PowerShell window, from the project folder:

      .\scripts\install_device.ps1

  It creates a private Python environment in the project's .venv folder, installs the exact tested package versions from requirements-lock.txt, and downloads
  the Chromium browser the report is rendered with. It is safe to run again: an existing environment is reused and already-installed packages are left alone.

  It needs: Windows, Python 3.14 (https://www.python.org/downloads/, tick "Add python.exe to PATH" / install the py launcher) and an internet connection.
  It does NOT sign in to Google, copy credentials or create any setting. After it finishes, follow the printed next steps (`seo setup device ...`).
#>
$ErrorActionPreference = "Stop"
$project = Split-Path -Parent $PSScriptRoot
Set-Location $project

function Fail($message) { Write-Host "`nERROR: $message" -ForegroundColor Red; exit 1 }
function Step($message) { Write-Host "`n== $message" -ForegroundColor Cyan }

if (-not (Test-Path "requirements-lock.txt")) { Fail "requirements-lock.txt was not found. Run this from a complete copy of the project." }
if ($project -match "OneDrive") {
    Write-Warning "The project is inside a OneDrive folder. It works, but OneDrive can slow down or interfere with the Python environment; a folder such as C:\JasperSEO is safer."
}

Step "1/4  Checking Python 3.14"
$launcher = Get-Command py -ErrorAction SilentlyContinue
if (-not $launcher) { Fail "The Python launcher 'py' was not found. Install Python 3.14 from python.org (include the py launcher), then run this again." }
$version = & py -3.14 --version 2>&1
if ($LASTEXITCODE -ne 0) { Fail "Python 3.14 was not found ($version). Install Python 3.14 from python.org, then run this again." }
Write-Host $version

Step "2/4  Creating the private environment (.venv)"
if (Test-Path ".venv\Scripts\python.exe") { Write-Host "Already exists; reusing it." }
else { & py -3.14 -m venv .venv; if ($LASTEXITCODE -ne 0) { Fail "Could not create the environment." } }
$python = Join-Path $project ".venv\Scripts\python.exe"

Step "3/4  Installing the tested package versions (this can take a few minutes)"
& $python -m pip install --disable-pip-version-check -r requirements-lock.txt
if ($LASTEXITCODE -ne 0) { Fail "Installing the packages failed. Check the internet connection and run this again." }

Step "4/4  Downloading the Chromium browser used for the PDF (about 150 MB, once)"
& $python -m playwright install chromium
if ($LASTEXITCODE -ne 0) { Fail "Downloading Chromium failed. Check the internet connection and run this again." }

Write-Host "`nInstalled. Next steps (in this folder):" -ForegroundColor Green
Write-Host '  1. .\seo.cmd setup device --spreadsheet-id "<the reporting workbook address or ID>"'
Write-Host '  2. Put the team''s OAuth client file in the credentials folder it names, then:  .\seo.cmd setup auth'
Write-Host '  3. .\seo.cmd setup device      (run again: it shows what is still missing)'
Write-Host '  4. .\seo.cmd doctor            (full check)   then   .\seo.cmd web'
