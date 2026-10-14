<#
Deploy the Cloudflare Pages proxy that serves the app at https://<project>.pages.dev (free plan).

One-time setup: a Cloudflare account, then log in on this computer (opens the browser once):
    npx --yes wrangler@3.114.17 login
Wrangler 3 is used because it runs on Node 18; Wrangler 4 needs Node 22.

After the first deploy, put the address in .env as DEPLOY_PUBLIC_URL and run deploy\azure.ps1 so the app uses it.
#>
param([string]$Project = "event-staffing-planner")

$ErrorActionPreference = "Stop"
$wrangler = @("--yes", "wrangler@3.114.17")

# wrangler.toml in deploy\cloudflare sets the output folder and the compatibility date.
Push-Location (Join-Path $PSScriptRoot "cloudflare")
try {
    $projects = (npx @wrangler pages project list 2>$null) -join "`n"
    if ($projects -notmatch "\b$([regex]::Escape($Project))\b") {
        Write-Host "==> Creating Pages project $Project"
        npx @wrangler pages project create $Project --production-branch main
    }

    Write-Host "==> Deploying the proxy"
    npx @wrangler pages deploy --project-name $Project --branch main --commit-dirty=true
}
finally {
    Pop-Location
}
