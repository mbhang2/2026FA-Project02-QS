"""CLI: python3 -m audit <command> ..."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from audit.facade import (
    format_brief,
    format_json,
    list_experiments,
    read_ledger,
    run_audit,
)
from audit.engine import audit_ticket_dir, slim_audit


def _cmd_list(_: argparse.Namespace) -> int:
    rows = list_experiments()
    seen: set[str] = set()
    slim = []
    for row in rows:
        eid = row["experiment_id"]
        if eid in seen:
            continue
        seen.add(eid)
        slim.append(row)
    print(format_json(slim))
    return 0


def _cmd_run(args: argparse.Namespace) -> int:
    ledger = Path(args.ledger) if args.ledger else None
    print(f"auditing {args.id} …", file=sys.stderr, flush=True)
    try:
        packet = run_audit(
            args.id,
            seed=args.seed,
            ledger=ledger,
            store=not args.no_store,
        )
    except (FileNotFoundError, ValueError, OSError) as exc:
        print(f"AUDIT_FAILED: {exc}", file=sys.stderr)
        return 2
    slim = slim_audit(packet)
    if not args.no_store:
        print(f"stored → {ledger or 'research/audit_db.json'}", file=sys.stderr)
    if args.brief:
        print(format_brief(slim))
    else:
        print(format_json(slim))
    return 0 if packet.get("verdict") == "PASS" else 1


def _cmd_ticket(args: argparse.Namespace) -> int:
    ticket = Path(args.ticket)
    print(f"auditing ticket {ticket} …", file=sys.stderr, flush=True)
    try:
        packet = audit_ticket_dir(ticket, seed=args.seed)
    except (FileNotFoundError, ValueError, OSError) as exc:
        print(f"AUDIT_FAILED: {exc}", file=sys.stderr)
        return 2
    slim = slim_audit(packet)
    if args.brief:
        print(format_brief(slim))
    else:
        print(format_json(slim))
    return 0 if packet.get("verdict") == "PASS" else 1


def _cmd_ledger(args: argparse.Namespace) -> int:
    ledger = Path(args.ledger) if args.ledger else None
    rows = read_ledger(args.id, ledger=ledger)
    if args.id and not rows:
        print(f"no ledger row for {args.id!r}", file=sys.stderr)
        return 1
    print(format_json(rows if args.brief else {"records": rows}))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="audit",
        description="Quant audit agent MVP — frozen gates + ledger",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_list = sub.add_parser("list", help="list auditable experiment ids")
    p_list.set_defaults(func=_cmd_list)

    p_run = sub.add_parser("run", help="run frozen auditor for one experiment_id")
    p_run.add_argument("--id", required=True, help="experiment_id or alias kill|pause")
    p_run.add_argument("--seed", type=int, default=7)
    p_run.add_argument("--ledger", help="path to audit_db.json")
    p_run.add_argument("--brief", action="store_true", help="one-screen verdict")
    p_run.add_argument("--no-store", action="store_true", help="do not append ledger")
    p_run.set_defaults(func=_cmd_run)

    p_ticket = sub.add_parser("ticket", help="audit all experiments under a ticket dir")
    p_ticket.add_argument("ticket", help="path to ticket (contains experiments/)")
    p_ticket.add_argument("--seed", type=int, default=7)
    p_ticket.add_argument("--brief", action="store_true")
    p_ticket.set_defaults(func=_cmd_ticket)

    p_ledger = sub.add_parser("ledger", help="read audit_db rows")
    p_ledger.add_argument("--id", help="latest row for experiment_id")
    p_ledger.add_argument("--ledger", help="path to audit_db.json")
    p_ledger.add_argument("--brief", action="store_true", help="presentation fields only")
    p_ledger.set_defaults(func=_cmd_ledger)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
