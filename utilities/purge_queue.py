"""Empty the chart-run Service Bus queue(s) and mark their waiting jobs as stopped (2026-11-19, user).

Run from core-pipeline/ with its venv (it reads core-pipeline/.env: SERVICEBUS_CONNECTION_STRING,
SERVICEBUS_QUEUE, SERVICEBUS_QUEUE_LARGE, DATABASE_URL). Stop the workers first, or they keep
taking messages while this empties the queue:

    docker compose stop worker                    (Ubuntu VM, bash)
    python ../utilities/purge_queue.py --dry-run  counts only, nothing changed
    python ../utilities/purge_queue.py            asks, then empties and marks the jobs
    python ../utilities/purge_queue.py --yes      no question

What it does, per queue (SERVICEBUS_QUEUE, and SERVICEBUS_QUEUE_LARGE when set; --queue NAME for
one queue only):

  1. counts the waiting and dead-lettered messages (when the key may read them: Manage rights);
  2. deletes every waiting message: 8 sessions (one per chart) at a time, each received-and-
     deleted (about a second per chart per thread);
  3. deletes the dead-lettered messages too (--keep-dead-letters to leave them);
  4. sets every pipeline_jobs row still queued / running / retrying on those queues to failed,
     error "cancelled: queue purged by operator" (--keep-jobs to leave them).

A chart a worker is running right now is not stopped by this: stop or kill the worker. No message
body, chart name or credential is printed: only counts.
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path
from typing import Any, Optional

REPO = Path(__file__).resolve().parents[1]
CORE = REPO / "core-pipeline"
if str(CORE) not in sys.path:
    sys.path.insert(0, str(CORE))

try:
    from dotenv import load_dotenv

    load_dotenv(CORE / ".env")
except ImportError:
    pass

CANCEL_NOTE = "cancelled: queue purged by operator"
SESSION_WAIT = 5  # seconds to wait for another session before the queue counts as empty
# Inside a session (one chart, usually one message): how long to wait for more. Was 5 s per
# chart, which made a purge of 500 charts take ~45 minutes (2026-11-19).
MESSAGE_WAIT = 1
PARALLEL = 8  # sessions emptied at once
BATCH = 100


def queues(only: Optional[str]) -> list[str]:
    if only:
        return [only]
    names = [os.environ.get("SERVICEBUS_QUEUE") or "chart-runs", os.environ.get("SERVICEBUS_QUEUE_LARGE") or ""]
    return list(dict.fromkeys(n.strip() for n in names if n.strip()))


def counts(conn_str: str, queue: str) -> Optional[tuple[int, int]]:
    """(waiting, dead-lettered), or None when the key may not read them."""
    try:
        from azure.servicebus.management import ServiceBusAdministrationClient

        with ServiceBusAdministrationClient.from_connection_string(conn_str) as admin:
            props = admin.get_queue_runtime_properties(queue)
            return int(props.active_message_count), int(props.dead_letter_message_count)
    except Exception as exc:  # noqa: BLE001 - counting is a courtesy
        print(f"  {queue}: counts not available ({type(exc).__name__}; needs Manage rights)")
        return None


def _drain_sessions(client: Any, queue: str) -> int:
    """One thread's share: take sessions in turn, receive-and-delete their messages, until no
    session with messages is left."""
    from azure.servicebus import NEXT_AVAILABLE_SESSION, ServiceBusReceiveMode

    deleted = 0
    while True:
        try:
            with client.get_queue_receiver(queue_name=queue, session_id=NEXT_AVAILABLE_SESSION,
                                           receive_mode=ServiceBusReceiveMode.RECEIVE_AND_DELETE,
                                           max_wait_time=SESSION_WAIT) as receiver:
                while True:
                    got = receiver.receive_messages(max_message_count=BATCH, max_wait_time=MESSAGE_WAIT)
                    if not got:
                        break
                    deleted += len(got)
        except Exception as exc:  # noqa: BLE001
            if type(exc).__name__ in {"OperationTimeoutError", "SessionCannotBeLockedError"}:
                return deleted  # no session left with messages
            raise


def purge_sessions(client_factory: Any, queue: str, parallel: int = PARALLEL) -> int:
    """Receive-and-delete every message of every session, ``parallel`` sessions at a time (one
    client per thread); the number deleted. ``client_factory()`` makes a client (a context
    manager)."""
    from concurrent.futures import ThreadPoolExecutor

    def one() -> int:
        with client_factory() as client:
            return _drain_sessions(client, queue)

    with ThreadPoolExecutor(max_workers=max(1, parallel)) as pool:
        return sum(pool.map(lambda _i: one(), range(max(1, parallel))))


def purge_dead_letters(client: Any, queue: str) -> int:
    from azure.servicebus import ServiceBusReceiveMode, ServiceBusSubQueue

    deleted = 0
    with client.get_queue_receiver(queue_name=queue, sub_queue=ServiceBusSubQueue.DEAD_LETTER,
                                   receive_mode=ServiceBusReceiveMode.RECEIVE_AND_DELETE,
                                   max_wait_time=SESSION_WAIT) as receiver:
        while True:
            got = receiver.receive_messages(max_message_count=BATCH, max_wait_time=MESSAGE_WAIT)
            if not got:
                return deleted
            deleted += len(got)


def waiting_jobs(cur: Any, names: list[str]) -> int:
    cur.execute("SELECT count(*) FROM pipeline_jobs WHERE status IN ('queued','running','retrying') "
                "AND queue_name = ANY(%s)", (names,))
    return int(cur.fetchone()[0])


def cancel_jobs(cur: Any, names: list[str]) -> int:
    cur.execute("UPDATE pipeline_jobs SET status = 'failed', completed_at = now(), error_message = %s "
                "WHERE status IN ('queued','running','retrying') AND queue_name = ANY(%s)",
                (CANCEL_NOTE, names))
    return int(cur.rowcount or 0)


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--queue", help="only this queue (default: SERVICEBUS_QUEUE and SERVICEBUS_QUEUE_LARGE)")
    ap.add_argument("--dry-run", action="store_true", help="counts only, change nothing")
    ap.add_argument("--yes", action="store_true", help="do not ask")
    ap.add_argument("--keep-dead-letters", action="store_true", help="leave the dead-lettered messages")
    ap.add_argument("--keep-jobs", action="store_true", help="leave pipeline_jobs as they are")
    args = ap.parse_args(argv)

    conn_str = (os.environ.get("SERVICEBUS_CONNECTION_STRING") or "").strip()
    if not conn_str:
        print("SERVICEBUS_CONNECTION_STRING is not set (core-pipeline/.env, section 14)")
        return 2
    names = queues(args.queue)
    print(f"queues: {', '.join(names)}")
    from common import queue as q

    for name in names:
        found = counts(q.connection_for(name), name)
        if found:
            print(f"  {name}: {found[0]} waiting, {found[1]} dead-lettered")

    db_url = (os.environ.get("DATABASE_URL") or "").strip()
    jobs = None
    if db_url and not args.keep_jobs:
        import psycopg

        with psycopg.connect(db_url) as conn, conn.cursor() as cur:
            jobs = waiting_jobs(cur, names)
        print(f"pipeline_jobs still queued / running / retrying on them: {jobs}")
    elif not args.keep_jobs:
        print("DATABASE_URL is not set: jobs left as they are")

    if args.dry_run:
        print("dry run: nothing changed")
        return 0
    if not args.yes:
        answer = input("Delete every waiting message" + ("" if args.keep_dead_letters else " and dead letter")
                       + (" and mark those jobs failed" if jobs else "") + "? Type yes: ")
        if answer.strip().lower() != "yes":
            print("nothing changed")
            return 1

    for name in names:
        # Each queue with its own key: SERVICEBUS_CONNECTION_STRING_LARGE for the large one when set.
        deleted = purge_sessions(lambda name=name: q.client_for(name), name)
        with q.client_for(name) as client:
            dead = 0 if args.keep_dead_letters else purge_dead_letters(client, name)
        print(f"  {name}: {deleted} waiting message(s) deleted, {dead} dead letter(s) deleted")
    if db_url and not args.keep_jobs:
        import psycopg

        with psycopg.connect(db_url) as conn, conn.cursor() as cur:
            changed = cancel_jobs(cur, names)
            conn.commit()
        print(f"pipeline_jobs marked failed ('{CANCEL_NOTE}'): {changed}")
    print("done. Start the workers again when ready: docker compose up -d --scale worker=3")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
