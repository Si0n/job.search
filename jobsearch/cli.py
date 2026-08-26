from __future__ import annotations

import argparse
import json
import sys
from typing import Any

from jobsearch.models import APPLICATION_STATUSES


def emit(payload: Any) -> None:
    """The only path from a command to stdout. Always JSON, never prose."""
    json.dump(payload, sys.stdout, ensure_ascii=False, default=str, indent=2)
    sys.stdout.write("\n")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="jobsearch")
    parser.add_argument("--env", default=".env", help="path to the env file")
    parser.add_argument("--profile", default="profile.yaml")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("init-db", help="create the schema and apply migrations")

    harvest_parser = sub.add_parser("harvest", help="fetch and store postings from HTTP sources")
    harvest_parser.add_argument("--source", action="append", dest="sources")
    harvest_parser.add_argument("--dry-run", action="store_true")

    sub.add_parser("filter", help="apply hard rules from profile.yaml")
    dctx = sub.add_parser("draft-context", help="everything needed to draft an application")
    dctx.add_argument("--id", type=int, required=True)
    dsave = sub.add_parser("draft", help="store an application draft")
    dsave.add_argument("--id", type=int, required=True)
    dsave.add_argument("--json", required=True, help="the draft payload as JSON")

    sub.add_parser("sweep", help="age out postings that stopped appearing")

    sub.add_parser("run-start", help="open a scoring run").add_argument(
        "--kind", default="score", choices=["score"])
    finish_parser = sub.add_parser("run-finish", help="close a scoring run")
    finish_parser.add_argument("--id", type=int, required=True)

    queue_parser = sub.add_parser("queue", help="jobs awaiting scoring")
    mode = queue_parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--unscored", action="store_true")
    mode.add_argument("--coarse-passed", action="store_true")
    queue_parser.add_argument("--limit", type=int, default=60)
    queue_parser.add_argument("--min", type=int, default=6)

    score_parser = sub.add_parser("score", help="record a scoring verdict")
    score_parser.add_argument("--id", type=int, required=True)
    score_parser.add_argument("--run-id", type=int, required=True)
    score_parser.add_argument("--pass", type=int, choices=[1, 2], required=True, dest="pass_no")
    score_parser.add_argument("--json", required=True, help="the scoring payload as JSON")

    serve_parser = sub.add_parser("serve", help="run the local dashboard")
    serve_parser.add_argument("--port", type=int, default=8765)
    serve_parser.add_argument("--open", action="store_true", dest="open_browser")

    list_parser = sub.add_parser("list", help="list jobs")
    list_parser.add_argument("--status", choices=APPLICATION_STATUSES)
    list_parser.add_argument("--min-score", type=int)
    list_parser.add_argument("--since")
    list_parser.add_argument("--source")
    list_parser.add_argument("--limit", type=int, default=50)

    status_parser = sub.add_parser("status", help="set application status")
    status_parser.add_argument("--id", type=int, required=True)
    status_parser.add_argument("--status", required=True, choices=APPLICATION_STATUSES)
    status_parser.add_argument("--note")

    sources_parser = sub.add_parser("sources", help="show source health")
    sources_parser.add_argument("--degraded", action="store_true")

    sub.add_parser("prune-cache", help="delete cached raw fetches older than 7 days")

    ingest_parser = sub.add_parser("ingest", help="ingest postings from stdin as JSON")
    ingest_parser.add_argument("--source", required=True)
    ingest_parser.add_argument("--stdin", action="store_true", default=True)

    repair_parser = sub.add_parser("repair-context", help="what a repair proposal needs")
    repair_parser.add_argument("--source", required=True)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.command == "init-db":
        from jobsearch import commands

        emit(commands.init_db(args))
        return 0

    if args.command == "harvest":
        from jobsearch import commands

        emit(commands.harvest(args))
        return 0

    if args.command == "filter":
        from jobsearch import commands

        emit(commands.filter_jobs(args))
        return 0

    if args.command == "draft-context":
        from jobsearch import commands

        emit(commands.draft_context(args))
        return 0

    if args.command == "draft":
        from jobsearch import commands

        emit(commands.save_draft(args))
        return 0

    if args.command == "sweep":
        from jobsearch import commands

        emit(commands.sweep(args))
        return 0

    if args.command == "run-start":
        from jobsearch import commands

        emit(commands.run_start(args))
        return 0

    if args.command == "run-finish":
        from jobsearch import commands

        emit(commands.run_finish(args))
        return 0

    if args.command == "queue":
        from jobsearch import commands

        emit(commands.queue(args))
        return 0

    if args.command == "score":
        from jobsearch import commands

        emit(commands.score(args))
        return 0

    if args.command == "serve":
        from jobsearch import commands

        emit(commands.serve(args))
        return 0

    if args.command == "list":
        from jobsearch import commands

        emit(commands.list_jobs(args))
        return 0

    if args.command == "status":
        from jobsearch import commands

        emit(commands.set_status(args))
        return 0

    if args.command == "sources":
        from jobsearch import commands

        emit(commands.sources(args))
        return 0

    if args.command == "prune-cache":
        from jobsearch import commands

        emit(commands.prune_cache(args))
        return 0

    if args.command == "ingest":
        from jobsearch import commands

        emit(commands.ingest(args))
        return 0

    if args.command == "repair-context":
        from jobsearch import commands

        emit(commands.repair_context(args))
        return 0

    parser.error(f"unhandled command: {args.command}")
    return 2
