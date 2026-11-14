#!/usr/bin/env python3
"""Cosmos: create the database + containers, and check this VM can use them.

    pip install azure-cosmos azure-identity            (+ azure-mgmt-cosmosdb for create_arm)
    python3 cosmos_setup.py
"""
# ============================ PARAMETERS ============================
ACTION = "check"            # "check" | "create" (with KEY) | "create_arm" (any Entra ID sign-in)
ENDPOINT = "https://<account>.documents.azure.com:443/"
DATABASE = "imaging"
THROUGHPUT = 1000           # shared RU/s, used only when the database is created

AUTH = "managed_identity"   # "managed_identity" | "service_principal" | "entra" | "key"
CLIENT_ID = ""              # managed_identity: user-assigned id ("" = system-assigned); service_principal: app id
TENANT_ID = ""              # service_principal only: the Entra ID tenant
CLIENT_SECRET = ""          # service_principal only: the secret's VALUE (not its ID). Never commit it.
KEY = ""                    # read-write primary key, only when AUTH = "key"

READ_ONLY = False           # check: True = read only; False = also write + delete a probe doc

# create_arm only (the identity needs "DocumentDB Account Contributor" on the account)
SUBSCRIPTION_ID = ""
RESOURCE_GROUP = ""
ACCOUNT_NAME = ""           # the Cosmos account name, e.g. the <account> in ENDPOINT
# ====================================================================

import sys
import uuid

CONTAINERS = {
    "charts": "/chart_name",
    "pages": "/chart_name",
    "runs": "/chart_name",
    "ground_truth": "/chart_name",
    "manifest": "/chart_name",
    "users": "/id",
    "metrics": "/period",
}
PAGES_INDEXING = {
    "indexingMode": "consistent",
    "includedPaths": [{"path": "/*"}],
    "excludedPaths": [{"path": "/ocr/*"}, {"path": "/kv/*"}, {"path": "/text/*"}, {"path": '/"_etag"/?'}],
}


def short(exc):
    return f"{type(exc).__name__}: {str(exc).splitlines()[0][:200]}"


def identity():
    """The Entra ID credential for AUTH: managed identity, app registration, or DefaultAzureCredential."""
    if AUTH == "service_principal":
        if not (TENANT_ID and CLIENT_ID and CLIENT_SECRET):
            sys.exit("AUTH 'service_principal' needs TENANT_ID, CLIENT_ID and CLIENT_SECRET")
        from azure.identity import ClientSecretCredential

        return ClientSecretCredential(tenant_id=TENANT_ID, client_id=CLIENT_ID, client_secret=CLIENT_SECRET)
    if AUTH == "entra":
        from azure.identity import DefaultAzureCredential

        kwargs = {"exclude_interactive_browser_credential": True}
        if CLIENT_ID:
            kwargs["managed_identity_client_id"] = CLIENT_ID
        return DefaultAzureCredential(**kwargs)
    from azure.identity import ManagedIdentityCredential

    return ManagedIdentityCredential(client_id=CLIENT_ID) if CLIENT_ID else ManagedIdentityCredential()


def who():
    if AUTH == "service_principal":
        return "Entra ID app registration (service principal)"
    if AUTH == "entra":
        return "Entra ID (DefaultAzureCredential: environment, managed identity, az login)"
    return f"managed identity ({'user-assigned' if CLIENT_ID else 'system-assigned'})"


def credential():
    if AUTH == "key":
        if not KEY:
            sys.exit("AUTH is 'key' but KEY is empty")
        print("sign-in: account key")
        return KEY
    print(f"sign-in: {who()}")
    cred = identity()
    try:
        cred.get_token("https://cosmos.azure.com/.default")
        print("  token: OK")
    except Exception as exc:  # noqa: BLE001
        sys.exit(f"  token: FAILED -- {short(exc)}\n"
                 "  (managed identity: run on the Azure VM / check CLIENT_ID; service principal: check the "
                 "tenant, app id and secret value; entra: az login or a managed identity)")
    return cred


def database_client():
    from azure.cosmos import CosmosClient

    return CosmosClient(ENDPOINT, credential=credential()).get_database_client(DATABASE)


def check():
    db = database_client()
    failed = 0
    for name, pk in CONTAINERS.items():
        container = db.get_container_client(name)
        try:
            container.read()
            if not READ_ONLY:
                field = pk.lstrip("/")
                doc_id = f"_probe-{uuid.uuid4().hex[:12]}"
                value = doc_id if field == "id" else "_probe"
                container.upsert_item({"id": doc_id, field: value, "type": "probe"})
                container.read_item(doc_id, partition_key=value)
                container.delete_item(doc_id, partition_key=value)
            print(f"  {name:<13} OK ({'read' if READ_ONLY else 'read, write, delete'})")
        except Exception as exc:  # noqa: BLE001
            failed += 1
            print(f"  {name:<13} FAILED -- {short(exc)}")
    print("ALL CONTAINERS OK" if not failed else f"{failed} CONTAINER(S) FAILED")
    if failed:
        print("  Forbidden/403: give the identity 'Cosmos DB Built-in Data Contributor' (wait ~5 min)\n"
              "  NotFound: the database / container does not exist yet (ACTION = create / create_arm)")
    return 1 if failed else 0


def create():
    if AUTH != "key":
        sys.exit("ACTION 'create' needs AUTH = 'key' (read-write key). With an Entra ID sign-in use 'create_arm'.")
    from azure.cosmos import CosmosClient, PartitionKey

    try:
        db = CosmosClient(ENDPOINT, credential=credential()).create_database_if_not_exists(
            id=DATABASE, offer_throughput=THROUGHPUT)
        print(f"database {DATABASE}: ready")
        for name, pk in CONTAINERS.items():
            extra = {"indexing_policy": PAGES_INDEXING} if name == "pages" else {}
            db.create_container_if_not_exists(id=name, partition_key=PartitionKey(path=pk), **extra)
            print(f"  container {name:<13} pk {pk}: ready")
    except Exception as exc:  # noqa: BLE001
        print(f"FAILED -- {short(exc)}")
        print("  Unauthorized: use the READ-WRITE primary key (Keys -> Read-write Keys), not a read-only one.\n"
              "  Forbidden: key-based creation is disabled on this account -- use create_arm or the portal.")
        return 1
    return 0


def create_arm():
    if not (SUBSCRIPTION_ID and RESOURCE_GROUP and ACCOUNT_NAME):
        sys.exit("create_arm needs SUBSCRIPTION_ID, RESOURCE_GROUP and ACCOUNT_NAME")
    try:
        from azure.mgmt.cosmosdb import CosmosDBManagementClient
    except ImportError:
        sys.exit("pip install azure-mgmt-cosmosdb")
    print(f"sign-in: {who()}, management plane")
    try:
        mgmt = CosmosDBManagementClient(identity(), SUBSCRIPTION_ID)
        mgmt.sql_resources.begin_create_update_sql_database(
            RESOURCE_GROUP, ACCOUNT_NAME, DATABASE,
            {"resource": {"id": DATABASE}, "options": {"throughput": THROUGHPUT}},
        ).result()
        print(f"database {DATABASE}: ready")
        for name, pk in CONTAINERS.items():
            resource = {"id": name, "partitionKey": {"paths": [pk], "kind": "Hash"}}
            if name == "pages":
                resource["indexingPolicy"] = PAGES_INDEXING
            mgmt.sql_resources.begin_create_update_sql_container(
                RESOURCE_GROUP, ACCOUNT_NAME, DATABASE, name, {"resource": resource, "options": {}},
            ).result()
            print(f"  container {name:<13} pk {pk}: ready")
    except Exception as exc:  # noqa: BLE001
        print(f"FAILED -- {short(exc)}")
        print("  AuthorizationFailed: give the identity 'DocumentDB Account Contributor' on the account.")
        return 1
    return 0


if __name__ == "__main__":
    print(f"endpoint: {ENDPOINT}  database: {DATABASE}  action: {ACTION}")
    actions = {"check": check, "create": create, "create_arm": create_arm}
    if ACTION not in actions:
        sys.exit(f"ACTION must be one of {', '.join(actions)}")
    sys.exit(actions[ACTION]())
