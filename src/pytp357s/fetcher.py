"""
Multi-device fetch pipeline: scans over BLE, fetches readings for all
requested devices in parallel, verifies overlap and continuity against
existing storage, and writes results.

This module contains no CLI/argparse code so it can be used directly from
Python. See ``cli.py`` for the command-line interface.
"""

from __future__ import annotations

import asyncio
import datetime
from dataclasses import dataclass, field
from typing import Any, Optional

from . import protocol, storage

TS_FORMAT = "%Y-%m-%d %H:%M"


@dataclass
class FetchResult:
    """Result of processing one device."""

    key: str
    status: str  # "ok" | "warning" | "error"
    message: str
    data: Optional[list[tuple[datetime.datetime, float, int]]] = None
    extra: dict[str, Any] = field(default_factory=dict)


async def fetch_live(key: str, device: dict[str, Any], scan_timeout: float = 20) -> FetchResult:
    """Fetch a single live reading from a device."""
    address = device["mac"]
    try:
        temp, hum = await protocol.ble_fetch_live(address, scan_timeout=scan_timeout)
        ts = datetime.datetime.now().strftime(TS_FORMAT)
        return FetchResult(
            key=key,
            status="ok",
            message=f"{temp:.1f}°C  {hum}%  ({ts})",
            extra={"temp": temp, "hum": hum},
        )
    except Exception as e:  # noqa: BLE001
        return FetchResult(key=key, status="error", message=str(e))


def _post_process_history(
    key: str,
    timestamped: list[tuple[datetime.datetime, float, int]],
    new_only_from: Optional[datetime.datetime],
    db_path: Optional[str],
    incremental: bool,
    force: bool,
    overlap: int,
    interval_minutes: int,
    debug_overlap: bool,
) -> FetchResult:
    """Verify overlap, detect gaps, write to storage, and return a FetchResult."""

    # ── No storage: just return the data ────────────────────────────────────
    if db_path is None:
        oldest, newest = timestamped[0][0], timestamped[-1][0]
        span = newest - oldest
        return FetchResult(
            key=key,
            status="ok",
            message=(
                f"{len(timestamped)} records  "
                f"[{oldest.strftime(TS_FORMAT)} → {newest.strftime(TS_FORMAT)}, "
                f"span {int(span.total_seconds() // 3600)}h "
                f"{int((span.total_seconds() % 3600) // 60)}m]"
            ),
            data=timestamped,
        )

    # ── Non-incremental write ─────────────────────────────────────────────
    if not incremental:
        try:
            storage.append(db_path, key, timestamped)
        except Exception as e:  # noqa: BLE001
            return FetchResult(key=key, status="error", message=f"Write failed: {e}")
        oldest, newest = timestamped[0][0], timestamped[-1][0]
        span = newest - oldest
        return FetchResult(
            key=key,
            status="ok",
            message=(
                f"{len(timestamped)} records written  "
                f"[{oldest.strftime(TS_FORMAT)} → {newest.strftime(TS_FORMAT)}, "
                f"span {int(span.total_seconds() // 3600)}h "
                f"{int((span.total_seconds() % 3600) // 60)}m]"
            ),
            data=timestamped,
        )

    # ── Incremental: first ever fetch for this device ────────────────────────
    if new_only_from is None:
        try:
            storage.append(db_path, key, timestamped)
        except Exception as e:  # noqa: BLE001
            return FetchResult(key=key, status="error", message=f"Write failed: {e}")
        oldest, newest = timestamped[0][0], timestamped[-1][0]
        span = newest - oldest
        return FetchResult(
            key=key,
            status="ok",
            message=(
                f"{len(timestamped)} records written (first fetch)  "
                f"[{oldest.strftime(TS_FORMAT)} → {newest.strftime(TS_FORMAT)}, "
                f"span {int(span.total_seconds() // 3600)}h "
                f"{int((span.total_seconds() % 3600) // 60)}m]"
            ),
            data=timestamped,
        )

    # ── Incremental: overlap/gap verification ────────────────────────────────
    stored_tail = storage.tail(db_path, key, overlap)
    new_rows = [(ts, t, h) for ts, t, h in timestamped if ts > new_only_from]
    mismatches: list[str] = []
    gap_warning: Optional[str] = None

    oldest_fetched = timestamped[0][0]
    gap_minutes = int(
        (oldest_fetched - new_only_from).total_seconds() / 60 / interval_minutes
    )
    if gap_minutes > 1:
        gap_warning = (
            f"Gap detected: oldest fetched record is "
            f"{oldest_fetched.strftime(TS_FORMAT)}, but storage ends at "
            f"{new_only_from.strftime(TS_FORMAT)} "
            f"({gap_minutes} intervals unaccounted for). "
            f"Re-run with --count {gap_minutes + overlap + 10} to cover the gap."
        )

    # Overlap verification (with +/-1 interval tolerance for boundary rounding)
    if stored_tail and overlap > 0:
        fetched_by_time = {ts: (t, h) for ts, t, h in timestamped}

        if debug_overlap:
            print(
                f"\n  [{key}] Overlap debug "
                f"({len(stored_tail)} stored vs {len(timestamped)} fetched):"
            )
            print(
                f"  {'Stored timestamp':<22} {'Stored':>12}  "
                f"{'Fetched timestamp':<22} {'Fetched':>12}  {'Match':>8}"
            )
            print(f"  {'-' * 84}")

        for sts, stemp, shum in stored_tail:
            match_ts = None
            for delta in (0, -1, 1):
                candidate = sts + datetime.timedelta(minutes=delta * interval_minutes)
                if candidate in fetched_by_time:
                    match_ts = candidate
                    break

            if match_ts is None:
                mismatches.append(
                    f"{sts.strftime(TS_FORMAT)}: not found in fetch "
                    f"(checked ±1 interval)"
                )
                if debug_overlap:
                    dash = "—"
                    print(
                        f"  {sts.strftime(TS_FORMAT):<22} "
                        f"{stemp:>6.1f}°C {shum:>3}%  "
                        f"{dash:<22} {dash:>12}  {'MISSING':>8}"
                    )
                continue

            ft, fh = fetched_by_time[match_ts]
            offset = int((match_ts - sts).total_seconds() // 60 // interval_minutes)
            offset_str = f"(+{offset})" if offset != 0 else ""
            if abs(ft - stemp) > 0.15 or abs(fh - shum) > 1:
                mismatches.append(
                    f"{sts.strftime(TS_FORMAT)}: "
                    f"stored={stemp:.1f}°C/{shum}% "
                    f"fetch={ft:.1f}°C/{fh}% {offset_str}"
                )
                if debug_overlap:
                    print(
                        f"  {sts.strftime(TS_FORMAT):<22} "
                        f"{stemp:>6.1f}°C {shum:>3}%  "
                        f"{match_ts.strftime(TS_FORMAT):<22} "
                        f"{ft:>6.1f}°C {fh:>3}%  "
                        f"{'MISMATCH':>8} {offset_str}"
                    )
            else:
                if debug_overlap:
                    print(
                        f"  {sts.strftime(TS_FORMAT):<22} "
                        f"{stemp:>6.1f}°C {shum:>3}%  "
                        f"{match_ts.strftime(TS_FORMAT):<22} "
                        f"{ft:>6.1f}°C {fh:>3}%  "
                        f"{'OK':>8} {offset_str}"
                    )
        if debug_overlap:
            print()

    # ── Mismatch handling ─────────────────────────────────────────────────
    if mismatches:
        detail = "; ".join(mismatches[:3])
        if len(mismatches) > 3:
            detail += f" … (+{len(mismatches) - 3} more)"
        if not force:
            return FetchResult(
                key=key,
                status="error",
                message=(
                    f"{len(mismatches)}/{len(stored_tail)} overlap records "
                    f"mismatched — NOT writing to protect data integrity. "
                    f"Details: {detail}. Re-run with --force to append anyway, "
                    f"or delete storage to start fresh."
                ),
            )
        try:
            storage.append(db_path, key, timestamped)
        except Exception as e:  # noqa: BLE001
            return FetchResult(key=key, status="error", message=f"Write failed: {e}")
        return FetchResult(
            key=key,
            status="warning",
            message=(
                f"Forced append: {len(timestamped)} records written despite "
                f"{len(mismatches)} overlap mismatch(es). Details: {detail}"
            ),
            data=timestamped,
        )

    # ── Gap handling ───────────────────────────────────────────────────────
    if gap_warning:
        if not force:
            return FetchResult(
                key=key,
                status="warning",
                message=(
                    gap_warning
                    + " — NOT writing to avoid gap. Re-run with --force "
                    "to write with a GAP marker inserted."
                ),
            )
        try:
            storage.append(db_path, key, [(None, None, None)] + timestamped)
        except Exception as e:  # noqa: BLE001
            return FetchResult(key=key, status="error", message=f"Write failed: {e}")
        return FetchResult(
            key=key,
            status="warning",
            message=(
                f"Forced append with gap marker: {len(timestamped)} records "
                f"written. ({gap_warning})"
            ),
            data=timestamped,
        )

    # ── All clear ─────────────────────────────────────────────────────────
    if new_rows:
        try:
            storage.append(db_path, key, new_rows)
        except Exception as e:  # noqa: BLE001
            return FetchResult(key=key, status="error", message=f"Write failed: {e}")

    verified = (
        f"overlap {len(stored_tail)}/{overlap} verified ✓"
        if stored_tail
        else "no overlap (first run)"
    )
    return FetchResult(
        key=key,
        status="ok",
        message=(
            f"{len(new_rows)} new records written  ({verified})  "
            f"[{timestamped[0][0].strftime(TS_FORMAT)} → "
            f"{timestamped[-1][0].strftime(TS_FORMAT)}]"
        ),
        data=new_rows,
    )


async def process_devices(
    devices: dict[str, dict[str, Any]],
    live: bool,
    db_path: Optional[str],
    incremental: bool,
    count: Optional[int],
    overlap: int,
    timeout: float,
    scan_timeout: float,
    parallelism: int,
    force: bool,
    interval_minutes: int,
    verbose: bool = False,
    debug_overlap: bool = False,
) -> dict[str, FetchResult]:
    """
    Fetch data from all requested devices, returning a FetchResult per device key.

    For live readings: each device is queried independently in parallel (up to
    ``parallelism`` simultaneous connections via an internal semaphore).

    For history: a single BLE scan locates all devices, then connections are
    established in parallel (semaphore inside ``ble_fetch_history``), and
    results are post-processed per device.
    """
    if live:
        sem = asyncio.Semaphore(parallelism)

        async def _live_one(key: str, device: dict) -> FetchResult:
            async with sem:
                return await fetch_live(key, device, scan_timeout=scan_timeout)

        results_list = await asyncio.gather(
            *[_live_one(k, d) for k, d in devices.items()]
        )
        return {r.key: r for r in results_list}

    # ── History fetch ────────────────────────────────────────────────────────

    # Collect per-device last_ts before the BLE scan starts.
    last_ts_map: dict[str, Optional[datetime.datetime]] = {}
    new_only_from_map: dict[str, Optional[datetime.datetime]] = {}
    for key, device in devices.items():
        address = device["mac"].upper()
        last_ts: Optional[datetime.datetime] = None
        if incremental and db_path:
            last_ts = storage.last_timestamp(db_path, key)
        last_ts_map[address] = last_ts
        new_only_from_map[key] = last_ts

    devices_info = [
        {"address": device["mac"], "label": key}
        for key, device in devices.items()
    ]

    raw_results = await protocol.ble_fetch_history(
        devices_info,
        count=count,
        last_ts_map=last_ts_map,
        overlap=overlap,
        interval_minutes=interval_minutes,
        parallelism=parallelism,
        timeout=timeout,
        scan_timeout=scan_timeout,
        verbose=verbose,
    )

    # Post-process each result: assign timestamps, verify overlap, write.
    fetch_results: dict[str, FetchResult] = {}
    for key, device in devices.items():
        address = device["mac"].upper()
        raw = raw_results.get(address)

        if isinstance(raw, BaseException):
            fetch_results[key] = FetchResult(key=key, status="error", message=str(raw))
            continue
        if raw is None:
            fetch_results[key] = FetchResult(key=key, status="error", message="No result received.")
            continue

        readings, fetch_time = raw
        timestamped = protocol.assign_timestamps(readings, fetch_time, interval_minutes)
        fetch_results[key] = _post_process_history(
            key=key,
            timestamped=timestamped,
            new_only_from=new_only_from_map[key],
            db_path=db_path,
            incremental=incremental,
            force=force,
            overlap=overlap,
            interval_minutes=interval_minutes,
            debug_overlap=debug_overlap,
        )

    return fetch_results
