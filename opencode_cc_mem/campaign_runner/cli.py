"""Command-line interface for campaign state."""

from __future__ import annotations

import argparse
import json
import sys
from typing import Sequence

from .models import CampaignError, load_protocol, protocol_sha256
from .state import CampaignState, CampaignStore


def _print_state(state: CampaignState) -> None:
    print(json.dumps(state.to_dict(), indent=2, sort_keys=True))


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="magnolia-campaign")
    subparsers = parser.add_subparsers(dest="command", required=True)

    validate = subparsers.add_parser("validate", help="validate a protocol file")
    validate.add_argument("protocol")

    init = subparsers.add_parser("init", help="initialize a campaign")
    init.add_argument("protocol")
    init.add_argument("--run-dir", required=True)
    init.add_argument("--brief", required=True)

    status = subparsers.add_parser("status", help="show campaign state")
    status.add_argument("run_dir")

    approve = subparsers.add_parser("approve", help="approve a waiting campaign")
    approve.add_argument("run_dir")
    approve.add_argument("--approver", required=True)
    approve.add_argument("--reason", required=True)

    pause = subparsers.add_parser("pause", help="pause a campaign")
    pause.add_argument("run_dir")
    pause.add_argument("--reason", default="operator pause")

    resume = subparsers.add_parser("resume", help="resume a paused campaign")
    resume.add_argument("run_dir")
    resume.add_argument("--reason", default="operator resume")

    halt = subparsers.add_parser("halt", help="halt a campaign")
    halt.add_argument("run_dir")
    halt.add_argument("--reason", default="operator halt")

    reconcile = subparsers.add_parser("reconcile", help="repair state from journal")
    reconcile.add_argument("run_dir")

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "validate":
            protocol = load_protocol(args.protocol)
            print(
                json.dumps(
                    {
                        "id": protocol.id,
                        "project": protocol.project,
                        "sha256": protocol_sha256(protocol),
                        "stages": [stage.id for stage in protocol.stages],
                    },
                    indent=2,
                    sort_keys=True,
                )
            )
            return 0
        if args.command == "init":
            store = CampaignStore(args.run_dir)
            state = store.initialize(args.protocol, args.brief)
            _print_state(state)
            return 0
        store = CampaignStore(args.run_dir)
        if args.command == "status":
            _print_state(store.load())
            return 0
        if args.command == "approve":
            _print_state(store.approve(args.approver, args.reason))
            return 0
        if args.command == "pause":
            _print_state(store.pause(args.reason))
            return 0
        if args.command == "resume":
            _print_state(store.resume(args.reason))
            return 0
        if args.command == "halt":
            _print_state(store.halt(args.reason))
            return 0
        if args.command == "reconcile":
            _print_state(store.reconcile())
            return 0
    except CampaignError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    parser.error(f"unknown command {args.command}")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
