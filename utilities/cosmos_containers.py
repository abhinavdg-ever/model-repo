#!/usr/bin/env python3
"""Create the Cosmos database and containers, and check that this host can use them
(2026-11-11, user: the client signs in with a managed identity).

    python3 utilities/cosmos_containers.py check                    # Mac / VM: every container, read + write + delete a probe
    python3 utilities/cosmos_containers.py check --read-only        # read only
    python3 utilities/cosmos_containers.py create                   # with COSMOS_KEY (data plane)
    python3 utilities/cosmos_containers.py create --arm --subscription <id> --resource-group <rg> --account <name>
    python utilities\\cosmos_containers.py check                    # Windows (Command Prompt)

Settings come from core-pipeline/.env (``--env`` for another file; the environment wins):
COSMOS_ENDPOINT, COSMOS_DATABASE, COSMOS_AUTH (key | managed_identity), COSMOS_KEY,
COSMOS_CLIENT_ID (else AZURE_CLIENT_ID). No value is ever printed.

``create`` makes database ``imaging`` (1,000 RU/s shared, or ``--throughput``) and the seven
containers of infra/main.bicep; it changes nothing that already exists. With a key it uses the
data plane. A managed identity's data-plane role (Cosmos DB Built-in Data Contributor) cannot
create databases or containers: use ``--arm`` (management plane; ``pip install
azure-mgmt-cosmosdb``; the identity, or your ``az login``, needs "DocumentDB Account
Contributor"), or deploy infra/main.bicep.

``check`` signs in exactly as the services do and, per container, reads it, then upserts, reads
and deletes one probe document (``--read-only``: read only). Exit code 0 when every container
passed.
"""
from __future__ import annotations

import argparse
import os
import sys
import uuid
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]

# As infra/main.bicep: name -> partition key path.
CONTAINERS = {
    "charts": "/chart_name",
    "pages": "/chart_name",
    "runs": "/chart_name",
    "ground_truth": "/chart_name",
    "manifest": "/chart_name",
    "users": "/id",
    "metrics": "/period",
}
# Large text is not indexed on pages: each write costs fewer RUs.
PAGES_INDEXING = {
    "indexingMode": "consistent",
    "includedPaths": [{"path": "/*"}],
    "excludedPaths": [{"path": "/ocr/*"}, {"path": "/kv/*"}, {"path": "/text/*"}, {"path": '/"_etag"/?'}],
}
MANAGED_IDENTITY = frozenset({"managed_identity", "managed-identity", "managed_entity", "mi", "msi"})


def load_env(path: Path) -> None:
    """KEY=VALUE lines into os.environ, never over a value already set."""
    if not path.is_file():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def setting(name: str, default: str = "") -> str:
    return (os.environ.get(name) or default).strip()


def managed_identity() -> bool:
    return setting("COSMOS_AUTH", "key").lower() in MANAGED_IDENTITY


def credential() -> Any:
    """What the services sign in with (core-pipeline common/db/cosmos_store.cosmos_credential)."""
    if managed_identity():
        from azure.identity import ManagedIdentityCredential

        client_id = setting("COSMOS_CLIENT_ID") or setting("AZURE_CLIENT_ID")
        print(f"sign-in: managed identity ({'user-assigned' if client_id else 'system-assigned'})")
        return ManagedIdentityCredential(client_id=client_id) if client_id else ManagedIdentityCredential()
    key = setting("COSMOS_KEY")
    if not key:
        sys.exit("COSMOS_KEY is empty and COSMOS_AUTH is not managed_identity")
    print("sign-in: account key")
    return key


def client() -> Any:
    from azure.cosmos import CosmosClient

    endpoint = setting("COSMOS_ENDPOINT")
    if not endpoint:
        sys.exit("COSMOS_ENDPOINT is not set")
    return CosmosClient(endpoint, credential=credential())


def create_data_plane(database: str, throughput: int) -> int:
    from azure.cosmos import PartitionKey

    if managed_identity():
        print("A managed identity cannot create databases or containers through the data plane.\n"
              "Use --arm (management plane), deploy infra/main.bicep, or run this once with COSMOS_AUTH=key.")
        return 2
    db = client().create_database_if_not_exists(id=database, offer_throughput=throughput)
    print(f"database {database}: ready")
    for name, pk in CONTAINERS.items():
        extra = {"indexing_policy": PAGES_INDEXING} if name == "pages" else {}
        db.create_container_if_not_exists(id=name, partition_key=PartitionKey(path=pk), **extra)
        print(f"  container {name:<13} pk {pk}: ready")
    return 0


def create_arm(database: str, throughput: int, subscription: str, group: str, account: str) -> int:
    try:
        from azure.mgmt.cosmosdb import CosmosDBManagementClient
    except ImportError:
        print("pip install azure-mgmt-cosmosdb (management plane), or deploy infra/main.bicep")
        return 2
    from azure.identity import DefaultAzureCredential, ManagedIdentityCredential

    if managed_identity():
        client_id = setting("COSMOS_CLIENT_ID") or setting("AZURE_CLIENT_ID")
        cred: Any = ManagedIdentityCredential(client_id=client_id) if client_id else ManagedIdentityCredential()
    else:
        cred = DefaultAzureCredential(exclude_interactive_browser_credential=True)
    mgmt = CosmosDBManagementClient(cred, subscription)
    mgmt.sql_resources.begin_create_update_sql_database(
        group, account, database,
        {"resource": {"id": database}, "options": {"throughput": throughput}},
    ).result()
    print(f"database {database}: ready")
    for name, pk in CONTAINERS.items():
        resource: dict[str, Any] = {"id": name, "partitionKey": {"paths": [pk], "kind": "Hash"}}
        if name == "pages":
            resource["indexingPolicy"] = PAGES_INDEXING
        mgmt.sql_resources.begin_create_update_sql_container(
            group, account, database, name, {"resource": resource, "options": {}},
        ).result()
        print(f"  container {name:<13} pk {pk}: ready")
    return 0


def check(database: str, read_only: bool) -> int:
    db = client().get_database_client(database)
    failed = 0
    for name, pk in CONTAINERS.items():
        container = db.get_container_client(name)
        try:
            container.read()
            if not read_only:
                probe_id = f"_probe-{uuid.uuid4().hex[:12]}"
                field = pk.lstrip("/")
                value = probe_id if field == "id" else "_probe"
                doc = {"id": probe_id, field: value, "type": "probe"}
                container.upsert_item(doc)
                container.read_item(probe_id, partition_key=value)
                container.delete_item(probe_id, partition_key=value)
            print(f"  {name:<13} OK ({'read' if read_only else 'read, write, delete'})")
        except Exception as exc:  # noqa: BLE001 - report every container
            failed += 1
            print(f"  {name:<13} FAILED: {type(exc).__name__}: {str(exc).splitlines()[0][:160]}")
    print("all containers OK" if not failed else f"{failed} container(s) failed")
    return 0 if not failed else 1


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("action", choices=("check", "create"))
    ap.add_argument("--env", default=str(ROOT / "core-pipeline" / ".env"), help="settings file (default core-pipeline/.env)")
    ap.add_argument("--database", help="database (default COSMOS_DATABASE, else imaging)")
    ap.add_argument("--throughput", type=int, default=1000, help="shared RU/s when the database is created")
    ap.add_argument("--read-only", action="store_true", help="check: read only, write nothing")
    ap.add_argument("--arm", action="store_true", help="create through the management plane")
    ap.add_argument("--subscription", help="--arm: subscription id")
    ap.add_argument("--resource-group", help="--arm: resource group")
    ap.add_argument("--account", help="--arm: Cosmos account name")
    args = ap.parse_args(argv)
    load_env(Path(args.env))
    database = args.database or setting("COSMOS_DATABASE", "imaging")
    print(f"database: {database}")
    if args.action == "check":
        return check(database, args.read_only)
    if args.arm:
        if not (args.subscription and args.resource_group and args.account):
            ap.error("--arm needs --subscription, --resource-group and --account")
        return create_arm(database, args.throughput, args.subscription, args.resource_group, args.account)
    return create_data_plane(database, args.throughput)


if __name__ == "__main__":
    raise SystemExit(main())
