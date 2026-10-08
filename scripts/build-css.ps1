<#
Build the site stylesheet (Tailwind CSS 4 + daisyUI 5) without Node.js.

    powershell -ExecutionPolicy Bypass -File .\scripts\build-css.ps1          # build once
    powershell -ExecutionPolicy Bypass -File .\scripts\build-css.ps1 -Watch   # rebuild on every change

Needs the Tailwind standalone executable at tools\tailwindcss.exe (git-ignored, ~107 MB):
https://github.com/tailwindlabs/tailwindcss/releases (tailwindcss-windows-x64.exe, renamed).
The daisyUI plugin files are pinned in src\forecast_mcp\styles\.
#>
param([switch]$Watch)
$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
$Tailwind = Join-Path $Root "tools\tailwindcss.exe"
if (-not (Test-Path $Tailwind)) { throw "Missing $Tailwind. Download tailwindcss-windows-x64.exe from the Tailwind releases and save it there." }
$In = Join-Path $Root "src\forecast_mcp\styles\app.css"
$Out = Join-Path $Root "src\forecast_mcp\static\app.css"
Push-Location (Join-Path $Root "src\forecast_mcp\styles")
try {
    if ($Watch) { & $Tailwind -i $In -o $Out --watch } else { & $Tailwind -i $In -o $Out --minify }
    if ($LASTEXITCODE -ne 0) { throw "Tailwind build failed" }
} finally { Pop-Location }
