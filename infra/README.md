# infra/

Bicep for what the platform plan needs **inside** the client's existing Azure resources
(the Cosmos account, the Service Bus namespace and the storage account are created by the
client's admins). Running it again changes nothing that already matches.

| Creates | Notes |
|---|---|
| Cosmos database `imaging` + 7 containers | shared manual throughput (1,000 RU/s on the free tier); `pages` does not index OCR text |
| Cosmos `imaging_client` + 7 containers | only with `createClientReplica=true` (billed outside the free tier) |
| Service Bus queue `chart-runs` | sessions on, 5-minute lock, 5 deliveries then dead-letter |
| Blob container `imaging-models` | models and canon (phase 8) |

Set `storageAccountName` in `main.parameters.json` first (the account in `AZURE_STORAGE_ACCOUNT_NAME`).

```bash
# Mac / Ubuntu VM (Azure CLI, signed in with `az login`)
az deployment group what-if -g <resource-group> -f infra/main.bicep -p infra/main.parameters.json
az deployment group create  -g <resource-group> -f infra/main.bicep -p infra/main.parameters.json
```

```powershell
# Windows (PowerShell, Azure CLI)
az deployment group what-if -g <resource-group> -f infra\main.bicep -p infra\main.parameters.json
az deployment group create  -g <resource-group> -f infra\main.bicep -p infra\main.parameters.json
```

Run `what-if` first: it lists the changes without making them. The Cosmos database and
containers already exist (created 2026-11-07 from the Mac), so `what-if` should show them
unchanged. Not run yet: there is no Azure CLI on the Mac.

**Session queue:** a queue's `requiresSession` cannot be changed after it is created. If
`chart-runs` was created without sessions, Azure refuses this deployment for the queue:
delete and recreate it (it holds no messages until phase 4), or deploy with another
`queueName`.
