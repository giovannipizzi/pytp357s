"""
Command-line interface for pytp357s.

Subcommands:
  fetch-data    Fetch live readings or history from one or more devices
  update-rooms  Sync device metadata (mac/name/room) from config into the DB
  plot          Plot temperature/humidity from a SQLite database
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys

from . import storage
from .config import ConfigError, load_config, resolve_device
from .fetcher import process_device


# ── fetch-data ────────────────────────────────────────────────────────────────


def _build_fetch_parser(subparsers) -> None:
    p = subparsers.add_parser(
        "fetch-data",
        help="Fetch live readings or history from one or more devices",
        description=(
            "Fetch data from TP357S devices over BLE.\n\n"
            "Without --db, fetched history is printed to stdout and nothing "
            "is stored.\n\n"
            "With --db but without --incremental: a full fetch is performed "
            "and written to the database. If the database already has data "
            "for a device, the command refuses to proceed for that device "
            "(use --incremental to append, or point --db at a new file).\n\n"
            "With --db --incremental: only new records since the last stored "
            "entry are fetched (plus a small overlap for verification). If "
            "the database has no prior data for a device, --incremental "
            "performs a full first fetch automatically."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument(
        "devices",
        nargs="+",
        metavar="DEVICE",
        help="Device key, name, MAC address, or 'all' (multiple allowed)",
    )
    p.add_argument("--config", metavar="FILE", help="Path to devices.yaml")
    p.add_argument("--live", action="store_true", help="Fetch a live reading and exit")
    p.add_argument("--db", metavar="FILE", help="SQLite database path")
    p.add_argument(
        "--incremental",
        action="store_true",
        help="Fetch only new records since the last stored entry",
    )
    p.add_argument(
        "--overlap",
        type=int,
        default=None,
        metavar="N",
        help="Overlap records for incremental verification (default from config)",
    )
    p.add_argument(
        "--force",
        action="store_true",
        help="Append despite overlap mismatch or gap (inserts a GAP marker)",
    )
    p.add_argument(
        "--count",
        type=int,
        default=None,
        metavar="N",
        help="Override number of records to request",
    )
    p.add_argument(
        "--timeout",
        type=float,
        default=None,
        metavar="SEC",
        help="BLE response timeout per device (default from config)",
    )
    p.add_argument(
        "--parallel",
        type=int,
        default=None,
        metavar="N",
        help="Max simultaneous BLE connections (default from config; 1=serial)",
    )
    p.add_argument("--verbose", action="store_true", help="Show extra debug info")
    p.add_argument(
        "--debug-overlap",
        action="store_true",
        help="Print full overlap comparison table for each device",
    )
    p.set_defaults(_handler=_cmd_fetch_data)


async def _run_fetch(args, cfg) -> int:
    devices_cfg = cfg["devices"]
    defaults = cfg["defaults"]

    raw_devices = args.devices
    if any(d.upper() == "ALL" for d in raw_devices):
        raw_devices = list(devices_cfg.keys())

    selected, unknown = {}, []
    for arg in raw_devices:
        key, device = resolve_device(devices_cfg, arg)
        if device:
            selected[key] = device
        else:
            unknown.append(arg)

    if unknown:
        print(f"Warning: unknown device(s) ignored: {', '.join(unknown)}", file=sys.stderr)
    if not selected:
        print("Error: no valid devices specified.", file=sys.stderr)
        return 1

    overlap = args.overlap if args.overlap is not None else defaults["overlap"]
    timeout = args.timeout if args.timeout is not None else defaults["timeout"]
    parallel = args.parallel if args.parallel is not None else defaults["parallel"]
    interval_minutes = defaults["interval_minutes"]

    if args.incremental and not args.db:
        print("Error: --incremental requires --db.", file=sys.stderr)
        return 1

    # Non-incremental + existing DB with data for these devices: refuse.
    if args.db and not args.incremental and not args.live:
        storage.init_db(args.db)
        existing = set(storage.list_devices_with_data(args.db))
        clash = existing & set(selected.keys())
        if clash:
            print(
                f"Error: database '{args.db}' already has data for: "
                f"{', '.join(sorted(clash))}.\n"
                "Use --incremental to append, or use a different/new --db file.",
                file=sys.stderr,
            )
            return 1

    if args.db:
        storage.init_db(args.db)
        # fetch-data always keeps device metadata in sync
        storage.update_devices_table(args.db, devices_cfg)

    parallelism = max(1, parallel)
    mode = "serial" if parallelism == 1 else f"up to {parallelism} simultaneous"
    n = len(selected)
    storage_desc = f"\u2192 {args.db} (SQLite)" if args.db else "stdout"
    print(
        f"Fetching {n} device{'s' if n > 1 else ''}: {', '.join(selected)}  "
        f"({'live' if args.live else 'history'}"
        f"{' incremental' if args.incremental else ''})  "
        f"[{mode}]  {storage_desc}"
    )
    print()

    semaphore = asyncio.Semaphore(parallelism)
    tasks = [
        asyncio.create_task(
            process_device(
                key,
                dev,
                semaphore,
                live=args.live,
                db_path=args.db,
                incremental=args.incremental,
                count=args.count,
                overlap=overlap,
                timeout=timeout,
                force=args.force,
                interval_minutes=interval_minutes,
                verbose=args.verbose,
                debug_overlap=args.debug_overlap,
            ),
            name=key,
        )
        for key, dev in selected.items()
    ]
    results = await asyncio.gather(*tasks)

    ok = warn = fail = 0
    for result in results:
        label = f"{result.key} ({selected[result.key].get('name', result.key)})"
        icon = {"ok": "\u2713", "warning": "\u26a0", "error": "\u2717"}[result.status]
        print(f"  {icon}  {label}: {result.message}")
        if result.status == "ok" and result.data is not None and not args.db and not args.live:
            print(f"\n     {'Timestamp':<20} {'Temp':>7} {'Hum':>5}")
            print(f"     {'-' * 34}")
            for ts, temp, hum in result.data:
                print(f"     {ts.strftime('%Y-%m-%d %H:%M'):<20} {temp:>6.1f}\u00b0C {hum:>4}%")
            print()
        if result.status == "ok":
            ok += 1
        elif result.status == "warning":
            warn += 1
        else:
            fail += 1

    print()
    summary = f"Done: {ok} succeeded"
    if warn:
        summary += f", {warn} warning{'s' if warn > 1 else ''}"
    if fail:
        summary += f", {fail} failed"
    print(summary + ".")
    return 1 if fail else 0


def _cmd_fetch_data(args) -> int:
    cfg = load_config(args.config)
    return asyncio.run(_run_fetch(args, cfg))


# ── update-rooms ──────────────────────────────────────────────────────────────


def _build_update_rooms_parser(subparsers) -> None:
    p = subparsers.add_parser(
        "update-rooms",
        help="Sync device metadata (mac/name/room) from config into the database",
        description=(
            "Sync the devices table in the SQLite database with the current "
            "contents of the config file (mac, name, room). Creates the "
            "database and table if they don't exist.\n\n"
            "Note: 'fetch-data' already does this automatically on every "
            "run, so this command is mainly useful if you've edited the "
            "config (e.g. added room names) and don't want to wait for the "
            "next scheduled fetch."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("--config", metavar="FILE", help="Path to devices.yaml")
    p.add_argument("--db", required=True, metavar="FILE", help="SQLite database path")
    p.set_defaults(_handler=_cmd_update_rooms)


def _cmd_update_rooms(args) -> int:
    cfg = load_config(args.config)
    storage.update_devices_table(args.db, cfg["devices"])
    for key, info in cfg["devices"].items():
        print(f"  {key}: mac={info.get('mac')}  name={info.get('name')}  room={info.get('room')!r}")
    print(f"Updated device metadata in {args.db}")
    return 0


# ── plot ──────────────────────────────────────────────────────────────────────


def _build_plot_parser(subparsers) -> None:
    p = subparsers.add_parser(
        "plot",
        help="Plot temperature/humidity from a SQLite database",
        description=(
            "Plot temperature and humidity for all devices with data in the "
            "database. Shows an interactive matplotlib window by default, "
            "or saves to a file with --output."
        ),
    )
    p.add_argument("--config", metavar="FILE", help="Path to devices.yaml")
    p.add_argument("--db", required=True, metavar="FILE", help="SQLite database path")
    p.add_argument("--output", metavar="FILE", help="Save plot to file (e.g. plot.png) instead of showing it")
    p.add_argument("--hours", type=float, metavar="N", help="Only plot the last N hours")
    p.set_defaults(_handler=_cmd_plot)


def _cmd_plot(args) -> int:
    from .plotting import plot_devices

    cfg = load_config(args.config)
    if not os.path.exists(args.db):
        print(f"Error: database '{args.db}' does not exist.", file=sys.stderr)
        return 1
    plot_devices(args.db, cfg["devices"], output=args.output, hours=args.hours)
    if args.output:
        print(f"Saved plot to {args.output}")
    return 0


# ── main ──────────────────────────────────────────────────────────────────────


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="pytp357s",
        description="Fetch and visualize history from ThermoPro TP357S BLE sensors.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    _build_fetch_parser(subparsers)
    _build_update_rooms_parser(subparsers)
    _build_plot_parser(subparsers)

    args = parser.parse_args(argv)

    try:
        return args._handler(args)
    except ConfigError as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\nInterrupted.", file=sys.stderr)
        return 130


if __name__ == "__main__":
    sys.exit(main())
