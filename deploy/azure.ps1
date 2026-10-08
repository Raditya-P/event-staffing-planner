<#
Deploy forecast-mcp to Azure Container Apps (works with an Azure for Students subscription).

    az login                                   # once, in the browser
    .\deploy\azure.ps1                          # first deploy, and every update afterwards
    .\deploy\azure.ps1 -EnableSignIn            # once WorkOS values are in .env

What it creates (in one resource group):
  - an Azure Container Registry, Basic tier (about USD 5 a month): stores the image; Azure builds it, no local Docker
  - a Container Apps environment without paid log storage
  - the container app: 0.5 vCPU / 1 GiB, scales to zero when idle (inside the monthly free allowance)

Secrets (DATABASE_URL, OIDC_CLIENT_SECRET, SESSION_SECRET) are read from .env and stored as Container Apps
secrets. They are never printed and never committed.
#>
param(
    [string]$ResourceGroup = "forecast-mcp-rg",
    [string]$Location = "southeastasia",   # Singapore, next to the Neon database
    [string]$AppName = "forecast-mcp",
    [string]$EnvironmentName = "forecast-mcp-env",
    [switch]$EnableSignIn
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
Set-Location $Root

# Call the Azure CLI's Python directly: az.cmd goes through cmd.exe, which breaks values containing & (like Neon URLs).
$AzPython = Join-Path ${env:ProgramFiles} "Microsoft SDKs\Azure\CLI2\python.exe"
if (-not (Test-Path $AzPython)) { throw "Azure CLI not found at $AzPython. Install it: winget install -e --id Microsoft.AzureCLI" }
function az {
    & $AzPython -IBm azure.cli @args
    if ($LASTEXITCODE -ne 0) { throw "az $($args[0..2] -join ' ') failed (exit $LASTEXITCODE)" }
}
function az-quiet { & $AzPython -IBm azure.cli @args 2>$null; return ($LASTEXITCODE -eq 0) }

function Read-DotEnv {
    $values = @{}
    if (-not (Test-Path ".env")) { throw "No .env file. Copy .env.example to .env and fill in DATABASE_URL." }
    foreach ($line in Get-Content ".env") {
        if ($line -match '^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=(.*)$') { $values[$Matches[1]] = $Matches[2].Trim() }
    }
    return $values
}

function Step($text) { Write-Host "`n==> $text" -ForegroundColor Cyan }

Step "Checking the Azure login"
if (-not (az-quiet account show --output none)) { throw "Not logged in. Run: az login" }
$sub = az account show --query "{name:name, id:id}" --output json | ConvertFrom-Json
Write-Host "Subscription: $($sub.name)"
$Registry = ("fmcp" + ($sub.id -replace '-', '').Substring(0, 12)).ToLower()   # globally unique, stable per subscription

$dotenv = Read-DotEnv
if (-not $dotenv["DATABASE_URL"]) { throw "DATABASE_URL is empty in .env" }
if (-not $dotenv["SESSION_SECRET"]) {
    # Generated once and kept in your local .env so sign-in sessions survive redeploys.
    $bytes = New-Object byte[] 32; [System.Security.Cryptography.RandomNumberGenerator]::Create().GetBytes($bytes)
    $dotenv["SESSION_SECRET"] = [Convert]::ToBase64String($bytes)
    Add-Content ".env" "`nSESSION_SECRET=$($dotenv['SESSION_SECRET'])"
    Write-Host "Generated SESSION_SECRET and saved it in .env"
}
if ($EnableSignIn) {
    foreach ($k in "OIDC_ISSUER", "OIDC_CLIENT_ID", "OIDC_CLIENT_SECRET") {
        if (-not $dotenv[$k]) { throw "-EnableSignIn needs $k in .env" }
    }
}

Step "Preparing the subscription (one-time, can take a minute)"
az extension add --name containerapp --upgrade --only-show-errors
foreach ($ns in "Microsoft.App", "Microsoft.ContainerRegistry") { az provider register --namespace $ns --wait --output none }

Step "Resource group $ResourceGroup in $Location"
az group create --name $ResourceGroup --location $Location --output none

Step "Container registry $Registry"
if (-not (az-quiet acr show --name $Registry --resource-group $ResourceGroup --output none)) {
    az acr create --name $Registry --resource-group $ResourceGroup --location $Location --sku Basic --admin-enabled true --output none
}

$tag = (git rev-parse --short HEAD).Trim()
if (git status --porcelain) { $tag = "$tag-dirty-$(Get-Date -Format yyyyMMddHHmm)" }
$image = "$Registry.azurecr.io/forecast-mcp:$tag"
Step "Building $image in Azure (takes a few minutes the first time)"
az acr build --registry $Registry --image "forecast-mcp:$tag" --file Dockerfile . --output none

Step "Container Apps environment $EnvironmentName"
if (-not (az-quiet containerapp env show --name $EnvironmentName --resource-group $ResourceGroup --output none)) {
    az containerapp env create --name $EnvironmentName --resource-group $ResourceGroup --location $Location --logs-destination none --output none
}

$secrets = @("db-url=$($dotenv['DATABASE_URL'])", "session-secret=$($dotenv['SESSION_SECRET'])")
if ($EnableSignIn) { $secrets += "oidc-secret=$($dotenv['OIDC_CLIENT_SECRET'])" }

$envVars = @(
    "ENV=production", "PORT=8000", "ENGINE_PROFILE=light", "SINGLE_INSTANCE=1", "TRUST_PROXY=1",
    "DATABASE_URL=secretref:db-url", "SESSION_SECRET=secretref:session-secret"
)
if ($dotenv["CONTACT_EMAIL"]) { $envVars += "CONTACT_EMAIL=$($dotenv['CONTACT_EMAIL'])" }
if ($EnableSignIn) {
    $envVars += @("AUTH_MODE=oidc", "OIDC_ISSUER=$($dotenv['OIDC_ISSUER'])", "OIDC_CLIENT_ID=$($dotenv['OIDC_CLIENT_ID'])",
                  "OIDC_CLIENT_SECRET=secretref:oidc-secret", "OIDC_PKCE=$(if ($dotenv['OIDC_PKCE']) { $dotenv['OIDC_PKCE'] } else { '0' })",
                  "MCP_AUDIENCE=$($dotenv['MCP_AUDIENCE'])")
} else {
    $envVars += "AUTH_MODE=none"
}

$creds = az acr credential show --name $Registry --query "{u:username, p:passwords[0].value}" --output json | ConvertFrom-Json
Step "Container app $AppName"
if (-not (az-quiet containerapp show --name $AppName --resource-group $ResourceGroup --output none)) {
    az containerapp create --name $AppName --resource-group $ResourceGroup --environment $EnvironmentName `
        --image $image --registry-server "$Registry.azurecr.io" --registry-username $creds.u --registry-password $creds.p `
        --target-port 8000 --ingress external --min-replicas 0 --max-replicas 1 --cpu 0.5 --memory 1.0Gi `
        --secrets @secrets --env-vars @envVars --output none
} else {
    az containerapp secret set --name $AppName --resource-group $ResourceGroup --secrets @secrets --output none
    az containerapp update --name $AppName --resource-group $ResourceGroup --image $image --set-env-vars @envVars --output none
}

# The app needs to know its own public address (sign-in redirects, the MCP address shown to users).
$fqdn = (az containerapp show --name $AppName --resource-group $ResourceGroup --query "properties.configuration.ingress.fqdn" --output tsv).Trim()
$url = "https://$fqdn"
az containerapp update --name $AppName --resource-group $ResourceGroup --set-env-vars "PUBLIC_BASE_URL=$url" --output none

Step "Done"
Write-Host "Dashboard:    $url/"
Write-Host "MCP endpoint: $url/mcp"
Write-Host "Sign-in:      $(if ($EnableSignIn) { 'on (WorkOS)' } else { 'OFF - anyone with the address can use it; run again with -EnableSignIn after WorkOS is set up' })"
Write-Host "The first request after an idle period wakes the app; give it up to a minute."
