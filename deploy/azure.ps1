<#
Deploy forecast-mcp to Azure Container Apps (works with an Azure for Students subscription).

Student subscriptions don't allow Azure to build images (ACR Tasks), so GitHub Actions builds the image after
the tests pass and pushes it to your Azure registry. This script sets that up and deploys a pushed image.

    az login                                       # once, in the browser
    .\deploy\azure.ps1 -ConnectGitHub              # once: registry + GitHub secrets, then GitHub builds the image
    .\deploy\azure.ps1                             # deploy the image of your latest pushed commit (and every update)
    .\deploy\azure.ps1 -EnableSignIn               # once WorkOS values are in .env

What it creates (in one resource group):
  - an Azure Container Registry, Basic tier (about USD 5 a month from the student credit)
  - a Container Apps environment without paid log storage
  - the container app: 0.5 vCPU / 1 GiB, scales to zero when idle (inside the monthly free allowance)

Secrets (DATABASE_URL, OIDC_CLIENT_SECRET, SESSION_SECRET, the registry password) go straight from .env or Azure
into Azure / GitHub secrets. They are never printed and never committed.
#>
param(
    [string]$ResourceGroup = "forecast-mcp-rg",
    # Closest region to the Neon database (Singapore) that Azure for Students subscriptions allow.
    [string]$Location = "malaysiawest",
    [string]$AppName = "forecast-mcp",
    [string]$EnvironmentName = "forecast-mcp-env",
    [string]$ImageTag = "",          # default: the commit you have checked out (it must be pushed and built)
    [switch]$ConnectGitHub,
    [switch]$EnableSignIn,
    [switch]$PruneImages             # keep only the 5 newest images in the registry
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
Set-Location $Root

# Call the Azure CLI's Python directly: az.cmd goes through cmd.exe, which breaks values containing & (like Neon URLs).
$AzPython = Join-Path ${env:ProgramFiles} "Microsoft SDKs\Azure\CLI2\python.exe"
if (-not (Test-Path $AzPython)) { throw "Azure CLI not found at $AzPython. Install it: winget install -e --id Microsoft.AzureCLI" }
# Windows PowerShell 5.1 turns a native command's stderr into terminating errors under "Stop", so both helpers
# run the CLI with "Continue" and judge success by its exit code only.
function az {
    $ErrorActionPreference = "Continue"
    & $AzPython -IBm azure.cli @args
    if ($LASTEXITCODE -ne 0) { throw "az $($args[0..2] -join ' ') failed (exit $LASTEXITCODE)" }
}
function az-quiet {
    # True if the command succeeds; output and errors are swallowed (used for "does this exist?" checks).
    $ErrorActionPreference = "Continue"
    & $AzPython -IBm azure.cli @args 2>&1 | Out-Null
    return ($LASTEXITCODE -eq 0)
}

function az-try {
    # The command's output if it succeeds, otherwise $null (used for lookups that may legitimately fail).
    $ErrorActionPreference = "Continue"
    $out = & $AzPython -IBm azure.cli @args 2>$null
    if ($LASTEXITCODE -eq 0) { return $out } else { return $null }
}

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

Step "Checking that $Location is allowed"
$allowed = az policy assignment list --query "[].parameters.listOfAllowedLocations.value[]" --output json | ConvertFrom-Json
if ($allowed -and ($allowed -notcontains $Location)) {
    throw "Your subscription only allows these regions: $($allowed -join ', '). Run again with -Location <one of them>."
}

Step "Preparing the subscription (one-time, can take a minute)"
az extension add --name containerapp --upgrade --only-show-errors
foreach ($ns in "Microsoft.App", "Microsoft.ContainerRegistry") { az provider register --namespace $ns --wait --output none }

Step "Resource group $ResourceGroup"
# A resource group's own location is only metadata; an existing group is kept wherever it is.
if ((az group exists --name $ResourceGroup).Trim() -ne "true") {
    az group create --name $ResourceGroup --location $Location --output none
}

# Reuse an existing registry (preferably next to the app); create one only if the group has none.
$Registry = "$(az acr list --resource-group $ResourceGroup --query "[?location=='$Location'].name | [0]" --output tsv)".Trim()
if (-not $Registry) { $Registry = "$(az acr list --resource-group $ResourceGroup --query "[0].name" --output tsv)".Trim() }
if (-not $Registry) {
    $Registry = ("fmcp" + ($sub.id -replace '-', '').Substring(0, 12) + ($Location -replace '[^a-z]', '').Substring(0, 2)).ToLower()
}
Step "Container registry $Registry"
if (-not (az-quiet acr show --name $Registry --resource-group $ResourceGroup --output none)) {
    az acr create --name $Registry --resource-group $ResourceGroup --location $Location --sku Basic --admin-enabled true --output none
}
$others = az acr list --resource-group $ResourceGroup --query "[?name!='$Registry'].name" --output tsv
if ($others) {
    Write-Warning "Unused registries still cost about USD 5 a month each: $($others -join ', '). Delete one with: az acr delete --name <name> --resource-group $ResourceGroup --yes"
}
$LoginServer = "$Registry.azurecr.io"

if ($ConnectGitHub) {
    Step "Letting GitHub Actions push images to $LoginServer"
    if (-not (Get-Command gh -ErrorAction SilentlyContinue)) { throw "GitHub CLI (gh) not found." }
    $creds = az acr credential show --name $Registry --query "{u:username, p:passwords[0].value}" --output json | ConvertFrom-Json
    gh variable set ACR_LOGIN_SERVER --body $LoginServer
    if ($LASTEXITCODE -ne 0) { throw "Could not store the GitHub variable (is gh logged in?)" }
    gh secret set ACR_USERNAME --body $creds.u
    if ($LASTEXITCODE -ne 0) { throw "Could not store ACR_USERNAME in GitHub" }
    gh secret set ACR_PASSWORD --body $creds.p
    if ($LASTEXITCODE -ne 0) { throw "Could not store ACR_PASSWORD in GitHub" }
    Write-Host "Stored ACR_LOGIN_SERVER (variable) and ACR_USERNAME / ACR_PASSWORD (secrets) in the GitHub repository."
    gh workflow run tests --ref main
    if ($LASTEXITCODE -ne 0) { throw "Could not start the GitHub workflow; push a commit to main instead." }
    Write-Host "Started the GitHub workflow. When it finishes (about 5 minutes; see: gh run list), run .\deploy\azure.ps1"
    exit 0
}

if (-not $ImageTag) { $ImageTag = (git rev-parse HEAD).Trim() }
$image = "$LoginServer/forecast-mcp:$ImageTag"
Step "Looking for the image of commit $($ImageTag.Substring(0, 7)) in the registry"
$tagsJson = az-try acr repository show-tags --name $Registry --repository forecast-mcp --output json
$tags = if ($tagsJson) { ($tagsJson -join "`n") | ConvertFrom-Json } else { @() }
if ($tags -notcontains $ImageTag) {
    throw ("The image for this commit is not in the registry yet. Push your commits (git push) and wait for the GitHub " +
           "'tests' workflow to pass (gh run list); it builds and uploads the image. First time? Run with -ConnectGitHub.")
}

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
Step "Container app $AppName (image $($ImageTag.Substring(0, 7)))"
if (-not (az-quiet containerapp show --name $AppName --resource-group $ResourceGroup --output none)) {
    az containerapp create --name $AppName --resource-group $ResourceGroup --environment $EnvironmentName `
        --image $image --registry-server $LoginServer --registry-username $creds.u --registry-password $creds.p `
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

if ($PruneImages) {
    Step "Removing old images (keeping the 5 newest)"
    $old = az acr repository show-tags --name $Registry --repository forecast-mcp --orderby time_desc --output json | ConvertFrom-Json | Select-Object -Skip 5
    foreach ($t in $old) { if ($t -ne $ImageTag) { az acr repository delete --name $Registry --image "forecast-mcp:$t" --yes --output none } }
}

Step "Done"
Write-Host "Dashboard:    $url/"
Write-Host "MCP endpoint: $url/mcp"
Write-Host "Sign-in:      $(if ($EnableSignIn) { 'on (WorkOS)' } else { 'OFF - anyone with the address can use it; run again with -EnableSignIn after WorkOS is set up' })"
Write-Host "The first request after an idle period wakes the app; give it up to a minute."
