"""
Per-device fetch pipeline: connects over BLE, fetches readings, verifies
overlap and continuity against existing storage, and writes results.

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


async def fetch_live(key: str, device: dict[str, Any]) -> FetchResult:
    """Fetch a single live reading from a device."""
    address = device["mac"]
    try:
        temp, hum = await protocol.ble_fetch_live(address)
        ts = datetime.datetime.now().strftime(TS_FORMAT)
        return FetchResult(
            key=key,
            status="ok",
            message=f"{temp:.1f}\u00b0C  {hum}%  ({ts})",
            extra={"temp": temp, "hum": hum},
        )
    except Exception as e:  # noqa: BLE001 - report any BLE error per-device
        return FetchResult(key=key, status="error", message=str(e))


async def fetch_history(
    key: str,
    device: dict[str, Any],
    db_path: Optional[str],
    incremental: bool,
    count: Optional[int],
    overlap: int,
    timeout: float,
    scan_timeout: float,
    force: bool,
    interval_minutes: int,
    verbose: bool = False,
    debug_overlap: bool = False,
) -> FetchResult:
    """
    Fetch history for one device and optionally write it to ``db_path``.

    If ``db_path`` is None, the fetched readings are returned in
    ``FetchResult.data`` and nothing is written.

    If ``incremental`` is True, only records newer than the last stored
    timestamp are appended, with ``overlap`` records re-fetched and
    compared against storage to detect clock drift, mismatches, or gaps.

    ``force`` controls behaviour when mismatches or gaps are detected:
    if False, the device is reported as an error/warning and nothing is
    written; if True, data is written anyway (with a GAP marker row if
    a gap was detected).
    """
    address = device["mac"]

    new_only_from = None
    fetch_count = count or 20000

    if incremental:
        if db_path is None:
            return FetchResult(
                key=key,
                status="error",
                message="--incremental requires --db.",
            )
        last_ts = storage.last_timestamp(db_path, key)
        if last_ts is not None:
            now = datetime.datetime.now().replace(second=0, microsecond=0)
            elapsed = max(1, int((now - last_ts).total_seconds() / 60 / interval_minutes))
            fetch_count = (count if count is not None else elapsed) + overlap
            new_only_from = last_ts

    try:
        readings, fetch_time = await protocol.ble_fetch_history(
            address, fetch_count, timeout=timeout, scan_timeout=scan_timeout, verbose=verbose, label=key
        )
    except Exception as e:  # noqa: BLE001
        return FetchResult(key=key, status="error", message=str(e))

    timestamped = protocol.assign_timestamps(readings, fetch_time, interval_minutes)

    # ── No storage: just return the data ────────────────────────────────────
    if db_path is None:
        oldest, newest = timestamped[0][0], timestamped[-1][0]
        span = newest - oldest
        return FetchResult(
            key=key,
            status="ok",
            message=(
                f"{len(timestamped)} records  "
                f"[{oldest.strftime(TS_FORMAT)} \u2192 {newest.strftime(TS_FORMAT)}, "
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
                f"[{oldest.strftime(TS_FORMAT)} \u2192 {newest.strftime(TS_FORMAT)}, "
                f"span {int(span.total_seconds() // 3600)}h "
                f"{int((span.total_seconds() % 3600) // 60)}m]"
            ),
            data=timestamped,
        )

    # ── Incremental write with overlap/gap verification ──────────────────────
    assert new_only_from is not None or storage.last_timestamp(db_path, key) is None

    if new_only_from is None:
        # First-ever fetch for this device into this DB
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
                f"[{oldest.strftime(TS_FORMAT)} \u2192 {newest.strftime(TS_FORMAT)}, "
                f"span {int(span.total_seconds() // 3600)}h "
                f"{int((span.total_seconds() % 3600) // 60)}m]"
            ),
            data=timestamped,
        )

    stored_tail = storage.tail(db_path, key, overlap)
    new_rows = [(ts, t, h) for ts, t, h in timestamped if ts > new_only_from]
    mismatches: list[str] = []
    gap_warning: Optional[str] = None

    # Gap detection
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
                    f"(checked \u00b11 interval)"
                )
                if debug_overlap:
                    dash = "\u2014"
                    print(
                        f"  {sts.strftime(TS_FORMAT):<22} "
                        f"{stemp:>6.1f}\u00b0C {shum:>3}%  "
                        f"{dash:<22} {dash:>12}  {'MISSING':>8}"
                    )
                continue

            ft, fh = fetched_by_time[match_ts]
            offset = int((match_ts - sts).total_seconds() // 60 // interval_minutes)
            offset_str = f"(+{offset})" if offset != 0 else ""
            if abs(ft - stemp) > 0.15 or abs(fh - shum) > 1:
                mismatches.append(
                    f"{sts.strftime(TS_FORMAT)}: "
                    f"stored={stemp:.1f}\u00b0C/{shum}% "
                    f"fetch={ft:.1f}\u00b0C/{fh}% {offset_str}"
                )
                if debug_overlap:
                    print(
                        f"  {sts.strftime(TS_FORMAT):<22} "
                        f"{stemp:>6.1f}\u00b0C {shum:>3}%  "
                        f"{match_ts.strftime(TS_FORMAT):<22} "
                        f"{ft:>6.1f}\u00b0C {fh:>3}%  "
                        f"{'MISMATCH':>8} {offset_str}"
                    )
            else:
                if debug_overlap:
                    print(
                        f"  {sts.strftime(TS_FORMAT):<22} "
                        f"{stemp:>6.1f}\u00b0C {shum:>3}%  "
                        f"{match_ts.strftime(TS_FORMAT):<22} "
                        f"{ft:>6.1f}\u00b0C {fh:>3}%  "
                        f"{'OK':>8} {offset_str}"
                    )
        if debug_overlap:
            print()

    # ── Mismatch handling ─────────────────────────────────────────────────
    if mismatches:
        detail = "; ".join(mismatches[:3])
        if len(mismatches) > 3:
            detail += f" \u2026 (+{len(mismatches) - 3} more)"
        if not force:
            return FetchResult(
                key=key,
                status="error",
                message=(
                    f"{len(mismatches)}/{len(stored_tail)} overlap records "
                    f"mismatched \u2014 NOT writing to protect data integrity. "
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
                    + " \u2014 NOT writing to avoid gap. Re-run with --force "
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
        f"overlap {len(stored_tail)}/{overlap} verified \u2713"
        if stored_tail
        else "no overlap (first run)"
    )
    return FetchResult(
        key=key,
        status="ok",
        message=(
            f"{len(new_rows)} new records written  ({verified})  "
            f"[{timestamped[0][0].strftime(TS_FORMAT)} \u2192 "
            f"{timestamped[-1][0].strftime(TS_FORMAT)}]"
        ),
        data=new_rows,
    )


async def process_device(
    key: str,
    device: dict[str, Any],
    semaphore: asyncio.Semaphore,
    **kwargs: Any,
) -> FetchResult:
    """Wrapper that applies a concurrency-limiting semaphore around fetch_*."""
    async with semaphore:
        if kwargs.pop("live", False):
            return await fetch_live(key, device)
        return await fetch_history(key, device, **kwargs)
