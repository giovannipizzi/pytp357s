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


async def find_devices(
    addresses: list[str],
    scan_timeout: float = 20,
    verbose: bool = False,
) -> dict[str, object]:
    """
    Scan for multiple TP357S devices simultaneously.

    Runs a single BLE scan and matches each discovered device against the
    requested addresses by MAC address (Linux/Windows) or TP357S name suffix
    (all platforms, required on macOS where CoreBluetooth replaces MACs with
    UUIDs). Returns as soon as all requested devices are found, or after
    scan_timeout seconds.

    Returns a dict mapping uppercase MAC address to the discovered BLEDevice.
    Addresses not found within the timeout are absent from the result.
    """
    addresses_upper = {a.upper() for a in addresses}
    suffix_to_addr = {mac_to_name_suffix(a): a.upper() for a in addresses}

    found: dict[str, object] = {}
    all_found = asyncio.Event()

    def callback(device, _advertisement_data) -> None:
        if all_found.is_set():
            return
        addr = device.address.upper()
        matched: str | None = None
        if addr in addresses_upper:
            matched = addr
        elif (
            device.name
            and device.name.startswith("TP357S (")
            and device.name.endswith(")")
        ):
            suffix = device.name[8:-1]
            if suffix in suffix_to_addr:
                matched = suffix_to_addr[suffix]
        if matched and matched not in found:
            found[matched] = device
            if verbose:
                remaining = len(addresses_upper) - len(found)
                suffix_note = f" ({remaining} still missing)" if remaining else ""
                print(f"  Found {matched} ({device.name}){suffix_note}")
            if len(found) == len(addresses_upper):
                all_found.set()

    async with BleakScanner(callback):
        try:
            await asyncio.wait_for(all_found.wait(), timeout=scan_timeout)
        except asyncio.TimeoutError:
            pass

    return found


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
    devices_info: list[dict],
    count: int | None,
    last_ts_map: dict[str, datetime.datetime | None],
    overlap: int,
    interval_minutes: int,
    parallelism: int = 1,
    timeout: float = 120,
    scan_timeout: float = 20,
    verbose: bool = False,
) -> dict[str, tuple[list[tuple[float, int]], datetime.datetime] | BaseException]:
    """
    Scan for and fetch history from multiple TP357S devices in parallel.

    Runs a single BLE scan for all requested addresses, then connects to each
    found device concurrently (up to ``parallelism`` simultaneous connections,
    enforced by a semaphore placed around BleakClient). The fetch count is
    computed inside the established connection, right before the first BLE
    write, so the elapsed-time calculation reflects the actual protocol start.

    Args:
        devices_info: list of dicts with keys ``address`` and ``label``.
        count: explicit record count override (None = derive from last_ts).
        last_ts_map: per-device last stored timestamp, keyed by uppercase address.
        overlap: extra records beyond elapsed time, for overlap verification.
        interval_minutes: device recording interval.
        parallelism: max simultaneous BLE connections.
        timeout: per-device BLE response timeout in seconds.
        scan_timeout: BLE discovery timeout in seconds.
        verbose: print per-device progress lines.

    Returns:
        Dict mapping uppercase address to (readings, fetch_time) or an Exception.
    """
    addresses = [d["address"].upper() for d in devices_info]
    label_map = {d["address"].upper(): d.get("label", d["address"]) for d in devices_info}

    if verbose:
        print(f"Scanning for {len(addresses)} device(s)...")

    found_devices = await find_devices([d["address"] for d in devices_info], scan_timeout=scan_timeout, verbose=verbose)

    if verbose:
        missing = [a for a in addresses if a not in found_devices]
        if missing:
            print(f"Found {len(found_devices)}/{len(addresses)}; not in range: {', '.join(missing)}")
        else:
            print(f"All {len(addresses)} device(s) found.")

    semaphore = asyncio.Semaphore(parallelism)
    results: dict[str, tuple | BaseException] = {}

    async def _connect_and_fetch(address: str) -> None:
        label = label_map.get(address, address)
        prefix = f"[{label}] " if label else ""
        dev = found_devices.get(address)
        if dev is None:
            results[address] = RuntimeError("Device not found (not in range?)")
            return

        chunks: list[bytes] = []
        done = asyncio.Event()

        def on_notify(_sender, data: bytearray) -> None:
            d = bytes(data)
            if d[:2] == b"\xcc\xcc":
                chunks.clear()
                chunks.append(d)
            elif chunks:
                chunks.append(d)
            if chunks and chunks[-1][-2:] == b"\x66\x66":
                done.set()

        try:
            async with semaphore:
                async with BleakClient(dev, timeout=20) as client:
                    if verbose:
                        print(f"{prefix}Connected. Sending datetime sync...")
                    await client.write_gatt_char(UUID_WRITE, make_datetime_cmd(), response=False)
                    await asyncio.sleep(1)

                    await client.start_notify(UUID_NOTIFY, on_notify)

                    # Compute fetch count now, inside the established connection,
                    # so `now` accurately reflects the start of the protocol exchange.
                    now = datetime.datetime.now()
                    fetch_time = now.replace(second=0, microsecond=0)
                    last_ts = last_ts_map.get(address)
                    if count is not None:
                        fetch_count = count
                    elif last_ts is not None:
                        elapsed = max(1, int((fetch_time - last_ts).total_seconds() / 60 / interval_minutes))
                        fetch_count = elapsed + overlap
                    else:
                        fetch_count = 20000

                    if verbose:
                        print(f"{prefix}Requesting {fetch_count} records...")

                    for cmd in make_history_cmds(fetch_count, now=now):
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
            results[address] = (readings, fetch_time)
        except Exception as e:  # noqa: BLE001
            results[address] = e

    tasks = [asyncio.create_task(_connect_and_fetch(addr)) for addr in addresses]
    await asyncio.gather(*tasks)

    return results


async def ble_fetch_live(address: str, timeout: float = 10, scan_timeout: float = 20) -> tuple[float, int]:
    """
    Connect to a TP357S device and fetch a single live reading.

    Returns:
        (temperature_celsius, humidity_percent)

    Raises:
        RuntimeError if the device cannot be found or no reading arrives.
    """
    received: list[bytes] = []
    done = asyncio.Event()

    def on_notify(_sender, data: bytearray) -> None:
        d = bytes(data)
        if d[0] == 0xC2 and len(d) >= 6:
            received.append(d)
            done.set()

    found = await find_devices([address], scan_timeout=scan_timeout)
    dev = found.get(address.upper())
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
