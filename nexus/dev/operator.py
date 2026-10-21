"""Local operator commands for Development Mode authorization."""

from __future__ import annotations

import argparse

from nexus.registry.database import initialize_database
from nexus.registry.projects import (
    get_dev_enabled,
    get_project,
    set_dev_enabled,
    sync_projects,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Manage local Nexus Development Mode authorization."
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    for command in ("authorize", "revoke", "status"):
        child = subparsers.add_parser(command)
        child.add_argument("project_id")
    args = parser.parse_args(argv)

    initialize_database()
    sync_projects()
    project = get_project(args.project_id)
    if project is None:
        parser.error(f"unknown project: {args.project_id!r}")
    if args.command == "authorize":
        set_dev_enabled(args.project_id, True)
    elif args.command == "revoke":
        set_dev_enabled(args.project_id, False)

    state = "AUTHORIZED" if get_dev_enabled(args.project_id) else "NOT_AUTHORIZED"
    print(f"{args.project_id}: {state}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
