param(
    [Parameter(Mandatory)]
    [string]$ResourceGroup,

    [Parameter(Mandatory)]
    [string]$Location,

    [Parameter(Mandatory)]
    [string]$AppName,

    [Parameter(Mandatory)]
    [string]$ApiKey
)

$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot

az group create --name $ResourceGroup --location $Location --output none
if ($LASTEXITCODE -ne 0) {
    throw 'Failed to create or update the Azure resource group.'
}

Push-Location $projectRoot
try {
    az containerapp up `
        --name $AppName `
        --resource-group $ResourceGroup `
        --location $Location `
        --source . `
        --ingress external `
        --target-port 8000 `
        --env-vars "MCP_API_KEY=$ApiKey"

    if ($LASTEXITCODE -ne 0) {
        throw 'Azure Container Apps deployment failed.'
    }
}
finally {
    Pop-Location
}

$fqdn = az containerapp show `
    --name $AppName `
    --resource-group $ResourceGroup `
    --query properties.configuration.ingress.fqdn `
    --output tsv

if ($LASTEXITCODE -ne 0 -or [string]::IsNullOrWhiteSpace($fqdn)) {
    throw 'The app deployed, but its public hostname could not be read.'
}

Write-Host ''
Write-Host "MCP server URL: https://$fqdn/mcp"
Write-Host "Health URL:     https://$fqdn/healthz"
