from __future__ import annotations

import argparse
import json
import sys
from typing import Any


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

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.command == "init-db":
        from jobsearch import commands

        emit(commands.init_db(args))
        return 0

    parser.error(f"unhandled command: {args.command}")
    return 2
