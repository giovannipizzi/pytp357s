"""
Low-level BLE protocol implementation for ThermoPro TP357S sensors.

Reverse-engineered June 2026. See PROTOCOL.md in the repository for full
documentation of the wire format, including open questions and limitations.
"""

from __future__ import annotations

import asyncio
import datetime

from bleak import BleakClient, BleakScanner

# ── BLE characteristic UUIDs ────────────────────────────────────────────────
# These are fixed by the TP357S firmware and identical across all units.

UUID_NOTIFY = "00010203-0405-0607-0809-0a0b0c0d2b10"
UUID_WRITE = "00010203-0405-0607-0809-0a0b0c0d2b11"


# ── Device discovery ─────────────────────────────────────────────────────────


def mac_to_name_suffix(mac: str) -> str:
    """
    Compute the expected TP357S advertisement name suffix from a MAC address.

    The device advertises as "TP357S (XXXX)" where XXXX is the last two
    bytes of the MAC address, uppercase hex, no separator.

    >>> mac_to_name_suffix("F9:84:14:CB:2A:AA")
    '2AAA'
    """
    parts = mac.upper().split(":")
    return "".join(parts[-2:])


async def find_device(address: str, scan_timeout: float = 20):
    """
    Find a TP357S device by MAC address, with a fallback for platforms
    where BLE addresses are not real MAC addresses (notably macOS, where
    CoreBluetooth exposes a per-host randomized UUID instead).

    Strategy:
      1. Try ``BleakScanner.find_device_by_address(address)`` directly.
         This works on Linux/BlueZ and Windows, where the address is a
         real MAC.
      2. If that returns nothing, perform a full discovery scan and match
         on the advertised name "TP357S (XXXX)", where XXXX is derived
         from the last two bytes of ``address`` (see
         :func:`mac_to_name_suffix`). This is the path used on macOS.

    Returns a ``BLEDevice`` or ``None`` if not found by either method.

    Note: on macOS, BLE advertisements are not perfectly continuous --
    a single device may not appear in every scan window. A
    ``scan_timeout`` of 15-20 seconds is more reliable than the bleak
    default of 10 seconds.
    """
    dev = await BleakScanner.find_device_by_address(address, timeout=10)
    if dev:
        return dev

    # Fallback: scan and match by advertised name suffix (macOS)
    suffix = mac_to_name_suffix(address)
    expected_name = f"TP357S ({suffix})"

    devices = await BleakScanner.discover(timeout=scan_timeout)
    for d in devices:
        if d.name == expected_name:
            return d

    return None

# ── Command builders ─────────────────────────────────────────────────────────


def make_datetime_cmd(now: datetime.datetime | None = None) -> bytes:
    """
    Build the datetime-sync command (0xa5).

    This is a required handshake: the device will not respond to history
    requests until it has received a datetime sync on the current connection.
    """
    if now is None:
        now = datetime.datetime.now()
    p = bytes(
        [
            0xA5,
            now.year % 100,
            now.month,
            now.day,
            now.hour,
            now.minute,
            now.second,
            now.weekday() + 1,
        ]
    )
    return p + bytes([sum(p) & 0xFF])


def make_history_cmds(count: int, now: datetime.datetime | None = None) -> tuple[bytes, bytes, bytes]:
    """
    Build the three-command sequence used to request history records.

    Args:
        count: number of records to request (16-bit, max 65535). The device
            returns however many records it actually has, up to ``count``.
        now: current datetime to embed in the request (defaults to now).

    Returns:
        (cmd1, cmd2, cmd3) — write these in order, ~200ms apart.
    """
    if now is None:
        now = datetime.datetime.now()
    lo, hi = count & 0xFF, (count >> 8) & 0xFF
    cmd1 = bytes.fromhex("cccc0201000001046666")
    cmd2 = bytes.fromhex("cccc04000000046666")
    body = bytes(
        [
            0x01,
            0x09,
            0x00,
            0x00,
            0x00,
            now.year % 100,
            now.month,
            now.day,
            now.hour,
            now.minute,
            now.second,
            lo,
            hi,
        ]
    )
    cs = sum(body) & 0xFF
    cmd3 = b"\xcc\xcc" + body + bytes([cs]) + b"\x66\x66"
    return cmd1, cmd2, cmd3


# ── Response decoding ────────────────────────────────────────────────────────


def decode_history_response(chunks: list[bytes]) -> list[tuple[float, int]]:
    """
    Decode reassembled BLE notification chunks into a list of
    (temperature_celsius, humidity_percent) tuples, most-recent record first.
    """
    buf = b"".join(chunks)
    if buf[:2] != b"\xcc\xcc":
        return []
    buf = buf[2:]
    if buf[-2:] == b"\x66\x66":
        buf = buf[:-2]
    pairs_raw = buf[5:-1]  # skip 5-byte header and trailing checksum
    readings = []
    for i in range(len(pairs_raw) // 3):
        raw = pairs_raw[i * 3 : i * 3 + 3]
        temp = int.from_bytes(raw[0:2], "little", signed=True) / 10
        hum = raw[2]
        readings.append((temp, hum))
    return readings


def decode_live_response(data: bytes) -> tuple[float, int]:
    """
    Decode a live-reading notification (0xc2 packet) into
    (temperature_celsius, humidity_percent).

    Packet format: c2 00 00 TL TH HH XX
    """
    temp = int.from_bytes(data[3:5], "little", signed=True) / 10
    hum = data[5]
    return temp, hum


def assign_timestamps(
    readings: list[tuple[float, int]],
    fetch_time: datetime.datetime | None = None,
    interval_minutes: int = 1,
) -> list[tuple[datetime.datetime, float, int]]:
    """
    Assign timestamps to readings.

    Readings arrive most-recent-first, one record per ``interval_minutes``.
    Returns a list of (datetime, temp, hum) tuples, oldest first.
    """
    if fetch_time is None:
        fetch_time = datetime.datetime.now().replace(second=0, microsecond=0)
    timestamped = [
        (fetch_time - datetime.timedelta(minutes=i * interval_minutes), t, h)
        for i, (t, h) in enumerate(readings)
    ]
    timestamped.reverse()
    return timestamped


# ── BLE communication ────────────────────────────────────────────────────────


async def ble_fetch_history(
    address: str,
    count: int,
    timeout: float = 120,
    verbose: bool = False,
    label: str = "",
) -> tuple[list[tuple[float, int]], datetime.datetime]:
    """
    Connect to a TP357S device, perform the datetime-sync handshake, and
    request up to ``count`` history records.

    Returns:
        (readings, fetch_time) — readings is most-recent-first.

    Raises:
        RuntimeError if the device cannot be found, the response times out,
        or no valid readings are decoded.
    """
    prefix = f"[{label}] " if label else ""

    dev = await find_device(address)
    if not dev:
        raise RuntimeError("Device not found (not in range?)")

    chunks: list[bytes] = []
    done = asyncio.Event()

    def on_notify(_sender, data):
        d = bytes(data)
        if d[:2] == b"\xcc\xcc":
            chunks.clear()
            chunks.append(d)
        elif chunks:
            chunks.append(d)
        if chunks and chunks[-1][-2:] == b"\x66\x66":
            done.set()

    fetch_time = datetime.datetime.now().replace(second=0, microsecond=0)

    async with BleakClient(dev, timeout=20) as client:
        if verbose:
            print(f"{prefix}Connected. Sending datetime sync...")
        await client.write_gatt_char(UUID_WRITE, make_datetime_cmd(), response=False)
        await asyncio.sleep(1)

        await client.start_notify(UUID_NOTIFY, on_notify)
        if verbose:
            print(f"{prefix}Requesting {count} records...")
        for cmd in make_history_cmds(count):
            await client.write_gatt_char(UUID_WRITE, cmd, response=False)
            await asyncio.sleep(0.2)

        try:
            await asyncio.wait_for(done.wait(), timeout=timeout)
        except asyncio.TimeoutError:
            raise RuntimeError(
                f"Response timed out after {timeout}s "
                f"({len(chunks)} chunks received). "
                f"Try a larger --timeout."
            )
        finally:
            await client.stop_notify(UUID_NOTIFY)

    readings = decode_history_response(chunks)
    if not readings:
        raise RuntimeError("Response received but contained no valid readings.")
    if verbose:
        print(f"{prefix}Received {len(readings)} records.")
    return readings, fetch_time


async def ble_fetch_live(address: str, timeout: float = 10) -> tuple[float, int]:
    """
    Connect to a TP357S device and fetch a single live reading.

    Returns:
        (temperature_celsius, humidity_percent)

    Raises:
        RuntimeError if the device cannot be found or no reading arrives.
    """
    received: list[bytes] = []
    done = asyncio.Event()

    def on_notify(_sender, data):
        d = bytes(data)
        if d[0] == 0xC2 and len(d) >= 6:
            received.append(d)
            done.set()

    dev = await find_device(address)
    if not dev:
        raise RuntimeError("Device not found (not in range?)")

    async with BleakClient(dev, timeout=20) as client:
        await client.start_notify(UUID_NOTIFY, on_notify)
        await client.write_gatt_char(UUID_WRITE, make_datetime_cmd(), response=False)
        try:
            await asyncio.wait_for(done.wait(), timeout=timeout)
        except asyncio.TimeoutError:
            raise RuntimeError("No live reading received within timeout.")
        finally:
            await client.stop_notify(UUID_NOTIFY)

    return decode_live_response(received[0])
