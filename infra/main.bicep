// Azure resources the platform plan relies on (docs/specials/PLATFORM_PLAN.md §4, §12 item 9).
// Every resource refers to an EXISTING account / namespace (created by the client's admins);
// this file only adds what lives inside them, so it is safe to run more than once.
//
//   az deployment group create -g <resource-group> -f infra/main.bicep -p infra/main.parameters.json
//
// What it creates (all idempotent):
//   Cosmos (NoSQL):  database `imaging` (manual throughput, shared) with its seven containers,
//                    and optionally the review-ui-client replica database.
//   Service Bus:     queue `chart-runs` with sessions, 5-minute lock, 5 deliveries -> dead-letter.
//   Storage:         blob container `imaging-models` (models + canon, phase 8).

@description('Existing Cosmos DB for NoSQL account name (e.g. medicalimagingtest).')
param cosmosAccountName string

@description('Database name.')
param cosmosDatabaseName string = 'imaging'

@description('Shared throughput of the database, RU/s (free tier: 1000).')
param cosmosThroughput int = 1000

@description('Also create the review-ui-client replica database (billed outside the free tier).')
param createClientReplica bool = false

@description('Replica database name for review-ui-client.')
param cosmosClientDatabaseName string = 'imaging_client'

@description('Existing Service Bus namespace (Standard or Premium: sessions need it).')
param serviceBusNamespaceName string

@description('Queue name.')
param queueName string = 'chart-runs'

@description('Existing storage account holding imaging-pipeline.')
param storageAccountName string

var containers = [
  { name: 'charts', pk: '/chart_name' }
  { name: 'pages', pk: '/chart_name' }
  { name: 'runs', pk: '/chart_name' }
  { name: 'ground_truth', pk: '/chart_name' }
  { name: 'manifest', pk: '/chart_name' }
  { name: 'users', pk: '/id' }
  { name: 'metrics', pk: '/period' }
]

// Large text is not indexed: each write of a page doc costs fewer RUs.
var pagesIndexing = {
  indexingMode: 'consistent'
  includedPaths: [ { path: '/*' } ]
  excludedPaths: [ { path: '/ocr/*' }, { path: '/kv/*' }, { path: '/text/*' }, { path: '/"_etag"/?' } ]
}

resource cosmos 'Microsoft.DocumentDB/databaseAccounts@2024-05-15' existing = {
  name: cosmosAccountName
}

resource database 'Microsoft.DocumentDB/databaseAccounts/sqlDatabases@2024-05-15' = {
  parent: cosmos
  name: cosmosDatabaseName
  properties: {
    resource: { id: cosmosDatabaseName }
    options: { throughput: cosmosThroughput }
  }
}

resource databaseContainers 'Microsoft.DocumentDB/databaseAccounts/sqlDatabases/containers@2024-05-15' = [for c in containers: {
  parent: database
  name: c.name
  properties: {
    resource: {
      id: c.name
      partitionKey: { paths: [ c.pk ], kind: 'Hash' }
      indexingPolicy: c.name == 'pages' ? pagesIndexing : null
    }
  }
}]

resource clientDatabase 'Microsoft.DocumentDB/databaseAccounts/sqlDatabases@2024-05-15' = if (createClientReplica) {
  parent: cosmos
  name: cosmosClientDatabaseName
  properties: {
    resource: { id: cosmosClientDatabaseName }
    options: { throughput: 400 }
  }
}

resource clientContainers 'Microsoft.DocumentDB/databaseAccounts/sqlDatabases/containers@2024-05-15' = [for c in containers: if (createClientReplica) {
  parent: clientDatabase
  name: c.name
  properties: {
    resource: {
      id: c.name
      partitionKey: { paths: [ c.pk ], kind: 'Hash' }
      indexingPolicy: c.name == 'pages' ? pagesIndexing : null
    }
  }
}]

resource serviceBus 'Microsoft.ServiceBus/namespaces@2022-10-01-preview' existing = {
  name: serviceBusNamespaceName
}

resource queue 'Microsoft.ServiceBus/namespaces/queues@2022-10-01-preview' = {
  parent: serviceBus
  name: queueName
  properties: {
    requiresSession: true
    lockDuration: 'PT5M'
    maxDeliveryCount: 5
    deadLetteringOnMessageExpiration: true
    defaultMessageTimeToLive: 'P14D'
  }
}

resource storage 'Microsoft.Storage/storageAccounts@2023-05-01' existing = {
  name: storageAccountName
}

resource blobService 'Microsoft.Storage/storageAccounts/blobServices@2023-05-01' existing = {
  parent: storage
  name: 'default'
}

resource modelsContainer 'Microsoft.Storage/storageAccounts/blobServices/containers@2023-05-01' = {
  parent: blobService
  name: 'imaging-models'
  properties: { publicAccess: 'None' }
}

output cosmosDatabase string = database.name
output queue string = queue.name
