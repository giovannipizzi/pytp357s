"""
Command-line interface for pytp357s.

Subcommands:
  fetch-data    Fetch live readings or history from one or more devices
  scan-devices  Scan for BLE devices and report what's visible
  update-rooms  Sync device metadata (mac/name/room) from config into the DB
  plot          Plot temperature/humidity from a SQLite database
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
import time

from . import __version__, storage
from .config import ConfigError, load_config, resolve_device
from .fetcher import process_devices


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
        "--scan-timeout",
        type=float,
        default=None,
        metavar="SEC",
        help="BLE device discovery timeout (default from config; macOS needs ~20s)",
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
    scan_timeout = args.scan_timeout if args.scan_timeout is not None else defaults["scan_timeout"]
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

    results_dict = await process_devices(
        devices=selected,
        live=args.live,
        db_path=args.db,
        incremental=args.incremental,
        count=args.count,
        overlap=overlap,
        timeout=timeout,
        scan_timeout=scan_timeout,
        parallelism=parallelism,
        force=args.force,
        interval_minutes=interval_minutes,
        verbose=args.verbose,
        debug_overlap=args.debug_overlap,
    )

    ok = warn = fail = 0
    for key, result in results_dict.items():
        label = f"{key} ({selected[key].get('name', key)})"
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


# ── scan-devices ─────────────────────────────────────────────────────────────


def _build_scan_parser(subparsers) -> None:
    p = subparsers.add_parser(
        "scan-devices",
        help="Scan for BLE devices and report what's visible",
        description=(
            "Scan the BLE environment and report which devices are visible,\n"
            "along with their internal name (if configured), MAC address,\n"
            "Bluetooth advertisement name, and how long they took to appear.\n\n"
            "Without --all: scans for the specified DEVICE(s), or — if none\n"
            "are given — for all devices in the config file. The scan exits\n"
            "as soon as all requested devices are found (or times out).\n\n"
            "With --all: every BLE device found during the full scan duration\n"
            "is reported; configured devices are labelled with their internal\n"
            "name (e.g. T1, T2). DEVICE arguments and the config file are\n"
            "both optional in this mode."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument(
        "devices",
        nargs="*",
        metavar="DEVICE",
        help="Device key, name, or MAC to scan for (optional with --all)",
    )
    p.add_argument("--config", metavar="FILE", help="Path to devices.yaml")
    p.add_argument(
        "--all",
        action="store_true",
        help="Report all visible BLE devices, not only configured ones",
    )
    p.add_argument(
        "--scan-timeout",
        type=float,
        default=None,
        metavar="SEC",
        help="How long to scan in seconds (default from config or 20s)",
    )
    p.set_defaults(_handler=_cmd_scan_devices)


async def _run_scan(args, cfg) -> int:
    from bleak import BleakScanner
    from .protocol import mac_to_name_suffix

    defaults = cfg.get("defaults", {})
    devices_cfg = cfg.get("devices", {})
    scan_timeout = (
        args.scan_timeout if args.scan_timeout is not None
        else defaults.get("scan_timeout", 20)
    )

    # Determine which devices to explicitly scan for:
    #   device args given  → use those
    #   no args, no --all  → use all configured devices (early-exit scan)
    #   --all, no args     → no explicit targets (full-duration scan)
    if args.devices:
        selected: dict[str, dict] = {}
        unknown: list[str] = []
        for arg in args.devices:
            key, device = resolve_device(devices_cfg, arg)
            if device:
                selected[key] = device
            else:
                unknown.append(arg)
        if unknown:
            print(f"Warning: unknown device(s) ignored: {', '.join(unknown)}", file=sys.stderr)
        if not selected and not args.all:
            print("Error: no valid devices specified.", file=sys.stderr)
            return 1
    elif not args.all:
        selected = dict(devices_cfg)
        if not selected:
            print("Error: no devices in config; specify a DEVICE or use --all.", file=sys.stderr)
            return 1
    else:
        selected = {}

    # Label lookup always covers all config devices so --all mode labels known ones too.
    known_by_addr = {dev["mac"].upper(): key for key, dev in devices_cfg.items()}
    suffix_to_key = {mac_to_name_suffix(dev["mac"]): key for key, dev in devices_cfg.items()}
    target_keys = set(selected.keys())

    if target_keys:
        print(f"Scanning for {', '.join(selected)} (timeout {scan_timeout:.0f}s)...")
    else:
        print(f"Scanning for {scan_timeout:.0f}s...")
    print()

    # Fixed column widths: label from config keys, addr covers both MAC (17) and
    # CoreBluetooth UUID (36), bt_name is a reasonable cap for advertisement names.
    label_w = max((len(f"[{k}]") for k in devices_cfg), default=4)
    addr_w = 36
    name_w = 20
    print(f"  {'':>{label_w}}  {'Address':<{addr_w}}  {'BT Name':<{name_w}}  {'Time':>6}")

    start = time.monotonic()
    seen: set[str] = set()
    found_target_keys: set[str] = set()
    n_known = 0
    n_unknown = 0
    all_targets_found = asyncio.Event()

    def callback(device, _adv_data) -> None:
        nonlocal n_known, n_unknown
        addr = device.address.upper()
        if addr in seen:
            return
        elapsed = time.monotonic() - start
        key = known_by_addr.get(addr)
        if key is None and device.name:
            if device.name.startswith("TP357S (") and device.name.endswith(")"):
                key = suffix_to_key.get(device.name[8:-1])
        if args.all or (key is not None and key in target_keys):
            seen.add(addr)
            label = f"[{key}]" if key else ""
            bt_name = device.name or ""
            print(f"  {label:<{label_w}}  {addr:<{addr_w}}  {bt_name:<{name_w}}  {elapsed:5.1f}s")
            if key is not None:
                n_known += 1
            else:
                n_unknown += 1
        if key is not None and key in target_keys and key not in found_target_keys:
            found_target_keys.add(key)
            if found_target_keys >= target_keys:
                all_targets_found.set()

    async with BleakScanner(callback):
        if args.all:
            await asyncio.sleep(scan_timeout)
        else:
            try:
                await asyncio.wait_for(all_targets_found.wait(), timeout=scan_timeout)
            except asyncio.TimeoutError:
                pass

    if not seen:
        print("\nNo devices found.")
        return 0

    print()
    parts = []
    if target_keys:
        parts.append(f"{len(found_target_keys)}/{len(target_keys)} configured device(s) found")
    elif n_known:
        parts.append(f"{n_known} configured device(s) visible")
    if args.all and n_unknown:
        parts.append(f"{n_unknown} other BLE device(s) visible")
    if parts:
        print(", ".join(parts) + ".")

    return 0


def _cmd_scan_devices(args) -> int:
    try:
        cfg = load_config(args.config)
    except ConfigError:
        if args.all:
            cfg = {"defaults": {}, "devices": {}}
        else:
            raise
    return asyncio.run(_run_scan(args, cfg))


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
    parser.add_argument("-V", "--version", action="version", version=f"%(prog)s {__version__}")
    subparsers = parser.add_subparsers(dest="command", required=True)

    _build_fetch_parser(subparsers)
    _build_scan_parser(subparsers)
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
