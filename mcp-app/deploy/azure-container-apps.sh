#!/usr/bin/env bash
# Deploys the dashboard MCP server to Azure Container Apps with HTTPS ingress.
# Prerequisites: Azure CLI (az login done), permission to create resources in the subscription.
# Usage: ./deploy/azure-container-apps.sh <resource-group> <region> <app-name> <api-key>
set -euo pipefail
RG=${1:?resource group}; LOC=${2:?region e.g. westus2}; APP=${3:?app name e.g. copilot-dashboard-mcp}; KEY=${4:?api key}
cd "$(dirname "$0")/.."
az group create -n "$RG" -l "$LOC" -o none
az containerapp up \
  --name "$APP" --resource-group "$RG" --location "$LOC" \
  --source . --ingress external --target-port 8000 \
  --env-vars MCP_API_KEY="$KEY"
FQDN=$(az containerapp show -n "$APP" -g "$RG" --query properties.configuration.ingress.fqdn -o tsv)
echo
echo "Server URL for Copilot Studio:  https://$FQDN/mcp"
echo "Health check:                   https://$FQDN/healthz"
