"""
SQLite storage backend for pytp357s.

Schema:

  readings(id, device, timestamp, temp_c, hum_rh, gap)
    - timestamp is Unix epoch seconds (UTC... actually local-naive, see note)
    - gap=1 marks a sentinel row indicating a known data gap

  devices(device, mac, name, room)
    - metadata table, kept in sync with the YAML config via
      `update_devices_table()`

Note on timestamps: readings are timestamped using the local naive
datetime returned by the device-fetch routines (``datetime.now()``-based).
``int(dt.timestamp())`` interprets a naive datetime as local time and
produces the corresponding Unix epoch second, which is what gets stored.
``datetime.fromtimestamp(epoch, tz=timezone.utc)`` is used symmetrically
when reading back, then converted to local naive time for comparisons.
This round-trip is consistent as long as the host timezone does not change
between writes.
"""

from __future__ import annotations

import datetime
import os
import sqlite3
from typing import Any, Optional


def init_db(path: str) -> None:
    """Create the readings and devices tables if they don't exist."""
    con = sqlite3.connect(path)
    con.execute(
        """
        CREATE TABLE IF NOT EXISTS readings (
            id        INTEGER PRIMARY KEY AUTOINCREMENT,
            device    TEXT    NOT NULL,
            timestamp INTEGER NOT NULL,
            temp_c    REAL,
            hum_rh    INTEGER,
            gap       INTEGER NOT NULL DEFAULT 0,
            UNIQUE (device, timestamp)
        )
        """
    )
    con.execute("CREATE INDEX IF NOT EXISTS idx_device_ts ON readings (device, timestamp)")
    con.execute(
        """
        CREATE TABLE IF NOT EXISTS devices (
            device  TEXT PRIMARY KEY,
            mac     TEXT,
            name    TEXT,
            room    TEXT
        )
        """
    )
    con.commit()
    con.close()


def update_devices_table(path: str, devices: dict[str, dict[str, Any]]) -> None:
    """
    Upsert device metadata (mac, name, room) from a config dict
    (as returned by ``config.load_config()['devices']``) into the
    devices table. Creates the table first if missing.
    """
    init_db(path)
    con = sqlite3.connect(path)
    for key, info in devices.items():
        con.execute(
            """
            INSERT INTO devices (device, mac, name, room)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(device) DO UPDATE SET
                mac  = excluded.mac,
                name = excluded.name,
                room = excluded.room
            """,
            (key, info.get("mac"), info.get("name"), info.get("room")),
        )
    con.commit()
    con.close()


def _epoch_to_local_naive(epoch: int) -> datetime.datetime:
    return (
        datetime.datetime.fromtimestamp(epoch, tz=datetime.timezone.utc)
        .astimezone()
        .replace(tzinfo=None)
    )


def last_timestamp(path: str, device: str) -> Optional[datetime.datetime]:
    """Return the most recent non-gap timestamp for a device, or None."""
    if not os.path.exists(path):
        return None
    con = sqlite3.connect(path)
    row = con.execute(
        "SELECT MAX(timestamp) FROM readings WHERE device=? AND gap=0",
        (device,),
    ).fetchone()
    con.close()
    if row and row[0] is not None:
        return _epoch_to_local_naive(row[0])
    return None


def tail(path: str, device: str, n: int) -> list[tuple[datetime.datetime, float, int]]:
    """Return the last n non-gap readings for a device, oldest first."""
    if not os.path.exists(path):
        return []
    con = sqlite3.connect(path)
    rows = con.execute(
        "SELECT timestamp, temp_c, hum_rh FROM readings "
        "WHERE device=? AND gap=0 ORDER BY timestamp DESC LIMIT ?",
        (device, n),
    ).fetchall()
    con.close()
    result = []
    for ts_epoch, temp, hum in reversed(rows):
        result.append((_epoch_to_local_naive(ts_epoch), temp, hum))
    return result


def append(
    path: str,
    device: str,
    rows: list[tuple[Optional[datetime.datetime], Optional[float], Optional[int]]],
) -> None:
    """
    Append rows to the readings table.

    A row of ``(None, None, None)`` inserts a gap-marker row (gap=1,
    timestamped at the moment of insertion).

    Duplicate (device, timestamp) pairs are silently ignored.
    """
    init_db(path)
    con = sqlite3.connect(path)
    for ts, temp, hum in rows:
        if ts is None:
            epoch = int(datetime.datetime.now(datetime.timezone.utc).timestamp())
            con.execute(
                "INSERT OR IGNORE INTO readings (device, timestamp, temp_c, hum_rh, gap) "
                "VALUES (?, ?, NULL, NULL, 1)",
                (device, epoch),
            )
        else:
            epoch = int(ts.timestamp())
            con.execute(
                "INSERT OR IGNORE INTO readings (device, timestamp, temp_c, hum_rh, gap) "
                "VALUES (?, ?, ?, ?, 0)",
                (device, epoch, temp, hum),
            )
    con.commit()
    con.close()


def all_readings(
    path: str, device: str, include_gaps: bool = False
) -> list[tuple[datetime.datetime, Optional[float], Optional[int], bool]]:
    """
    Return all readings for a device, oldest first, as
    (datetime, temp_c_or_None, hum_rh_or_None, is_gap) tuples.
    """
    if not os.path.exists(path):
        return []
    con = sqlite3.connect(path)
    if include_gaps:
        rows = con.execute(
            "SELECT timestamp, temp_c, hum_rh, gap FROM readings "
            "WHERE device=? ORDER BY timestamp ASC",
            (device,),
        ).fetchall()
    else:
        rows = con.execute(
            "SELECT timestamp, temp_c, hum_rh, gap FROM readings "
            "WHERE device=? AND gap=0 ORDER BY timestamp ASC",
            (device,),
        ).fetchall()
    con.close()
    return [
        (_epoch_to_local_naive(ts), temp, hum, bool(g)) for ts, temp, hum, g in rows
    ]


def list_devices_with_data(path: str) -> list[str]:
    """Return device keys that have at least one reading row."""
    if not os.path.exists(path):
        return []
    con = sqlite3.connect(path)
    rows = con.execute("SELECT DISTINCT device FROM readings ORDER BY device").fetchall()
    con.close()
    return [r[0] for r in rows]
