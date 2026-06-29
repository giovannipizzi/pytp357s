# TP357S BLE Protocol Reference

This document describes the reverse-engineered Bluetooth Low Energy protocol
used by ThermoPro TP357S temperature/humidity sensors, as implemented in
`pytp357s`. It's aimed at people who want to understand or extend the
implementation, debug issues, or port the protocol to another language.

Disclaimer: the procotol was reverse-engineered also using support from
the Claude LLM.

For usage instructions, see [README.md](README.md).

---

## Device Overview

The **TP357S** is a BLE temperature/humidity sensor by ThermoPro. Despite
sharing a name with the original TP357, the S variant uses a **different
GATT protocol** for history retrieval. Existing open-source tools written
for the original TP357 (e.g. `tpy357`, `pasky/tp357`) do not work correctly
with the S variant. They may connect and may return a live reading, but
history requests using the original opcodes (`0xa6`/`0xa7`/`0xa8`) return
only a live-reading echo, not historical data.

---

## BLE Characteristics

| Role | UUID |
|------|------|
| Write (commands) | `00010203-0405-0607-0809-0a0b0c0d2b11` |
| Notify (responses) | `00010203-0405-0607-0809-0a0b0c0d2b10` |

These live under service `00010203-0405-0607-0809-0a0b0c0d1910`. These
UUIDs are fixed by the firmware and identical across all TP357S units —
they are hardcoded as constants in `pytp357s.protocol`.

The device also exposes a Nordic DFU service (`0000fe59-...`) and a version
characteristic (`00000003-...`, readable, returns ASCII `"Version1.0"`), but
these are not used for data retrieval.

---

## Live Reading

The device broadcasts live temperature and humidity in BLE advertisement
packets (manufacturer data), decodable passively without connecting. When
connected, it also sends a `0xc2` notification on the notify characteristic:

```
c2 00 00 TL TH HH XX
```

| Bytes | Meaning |
|-------|---------|
| `c2` | Opcode (live reading) |
| `00 00` | Padding |
| `TL TH` | Temperature x 10, signed 16-bit little-endian |
| `HH` | Relative humidity, uint8 |
| `XX` | Unknown (see Open Questions) |

**Example:** `c2 00 00 ef 00 28 2c` -> temp = `0x00ef` = 239 -> **23.9degC**,
hum = `0x28` = **40%**

---

## Session Protocol

Every connection must follow this sequence:

### 1. Enable Notifications (CCCD)

Write `01 00` to the CCCD descriptor of the notify characteristic to enable
notifications. (Handled automatically by `bleak`'s `start_notify`.)

### 2. Datetime Sync (Required Handshake)

The device **will not respond to history commands** without first receiving
a datetime sync on the current connection. Write to the write characteristic:

```
a5 YY MM DD HH MM SS DOW CS
```

| Field | Meaning |
|-------|---------|
| `a5` | Opcode |
| `YY` | Year minus 2000 |
| `MM` | Month (1-12) |
| `DD` | Day (1-31) |
| `HH` | Hour (0-23) |
| `MM` | Minute (0-59) |
| `SS` | Second (0-59) |
| `DOW` | Day of week (1=Sunday ... 7=Saturday) |
| `CS` | Checksum: `sum(all previous bytes) & 0xff` |

The device responds with two notifications: first a `0xc2` live reading,
then `a5 01 13 5a` as acknowledgement.

Implemented in `protocol.make_datetime_cmd()`.

### 3. History Request (Three Commands)

Send three write commands in sequence (~200ms apart):

**Command 1 -- Session init (fixed bytes):**
```
cc cc 02 01 00 00 01 04 66 66
```

**Command 2 -- Offset (appears ignored by firmware):**
```
cc cc 04 00 00 00 00 04 66 66
```

**Command 3 -- Data request:**
```
cc cc 01 09 00 00 00 YY MM DD HH MM SS NL NH CS 66 66
```

| Field | Meaning |
|-------|---------|
| `cc cc` | Magic prefix |
| `01 09` | Subcommand |
| `00 00 00` | Unknown, always zero in observed traffic |
| `YY MM DD HH MM SS` | Current datetime (same encoding as sync) |
| `NL NH` | Record count, **16-bit little-endian** |
| `CS` | Checksum: `sum(bytes from 0x01 onward, before CS) & 0xff` |
| `66 66` | Magic suffix |

Implemented in `protocol.make_history_cmds()`.

---

## History Response Format

The device responds with one or more BLE notification chunks (~50 bytes
each). The stream starts with `cc cc` and ends with `66 66`. Reassemble all
chunks before decoding.

```
cc cc 01 [N2 N1 N0] 00 [R R R] [R R R] ... [CS] 66 66
```

| Field | Meaning |
|-------|---------|
| `cc cc` | Magic prefix |
| `01` | Subcommand echo |
| `N2 N1 N0` | 3-byte little-endian: `(records_returned * 3) + 1` |
| `00` | Padding |
| `R R R` | Reading: 2-byte signed LE temp x 10, 1-byte humidity |
| `CS` | Checksum byte |
| `66 66` | Magic suffix |

**Reading triplet:**
- Bytes 0-1: temperature x 10, signed 16-bit little-endian -> divide by 10 for degC
- Byte 2: relative humidity, uint8, percent

**Ordering:** most-recent record first, stepping back one recording
interval (observed: 1 minute) per record.

Implemented in `protocol.decode_history_response()`.

---

## Key Behaviours

### Record count is 16-bit

The count field `NL NH` in command 3 is a **16-bit little-endian unsigned
integer** (max 65535), not 8-bit. Counts above 255 work correctly and are
the normal way to fetch large amounts of history.

### No real pagination

The device always returns the N most-recent records regardless of the
4-byte field in command 2. To fetch all available history, request a large
count (e.g. 20000). The device returns however much it has stored and
closes with `66 66`. Requesting more than the device has simply returns
everything it has.

### Response is streaming

Large responses arrive as many small BLE notification chunks. There is no
chunk count or per-chunk length prefix -- concatenate everything between
`cc cc` and `66 66`.

### Transfer speed

Roughly 1500-2000 records/second over BLE.

---

## Open Questions / Known Limitations

These are aspects of the protocol that are not yet fully understood.
While issues/pull requests investigating these are welcome, I might
not have the time to follow up and check on them.

### Buffer size / maximum history

It is unclear yet what is the maximum buffer size (history) on device,
but considering that the `count` is a 16-bit little-endian, probably
the device can store ~65535 entries (one per minute), so ~45 days.
But it is unclear if the actual storage is so long, or it is shorter.
Checking on a long-running device and fetching data from scratch will
allow to confirm this.
Note that internally, by default, only 20000 entries at most are fetched
(configurable under `max_count` in the YAML configuration file), so
a bit less than 2 weeks. Change this value to fetch for longer times,
or to test the buffer size.

### Recording interval

All observed data is at **1-minute intervals**. It's unknown whether the
official ThermoPro app can configure a different interval, and if so how
that would affect the protocol (the response format has no explicit
interval field -- it's assumed from context).

### Battery level

The `0xc2` live-reading packet has an unknown trailing byte (`XX`, observed
as `0x2c` in most captures). This may encode battery level, but the formula
is unconfirmed. (Note: the unrelated `tpy357` library computes
`battery_level / 2 * 100`, which produces nonsensical results like 1700% --
so that formula does not aply to the `TP357S` device.)

### Week/year history modes

The original TP357 protocol supports `day`/`week`/`year` history modes via
opcodes `0xa7`/`0xa6`/`0xa8`. These opcodes do **not** trigger history on the
TP357S -- they only return a `0xc2` live-reading echo. The TP357S appears to
use only the single `0xcccc` protocol with one large count parameter, with
no separate "week" or "year" mode.

### Checksum validation

The device appears to accept commands with incorrect checksums (it does not
seem to validate them strictly). The checksum formula
(`sum of body bytes & 0xff`) was confirmed empirically against real app
traffic but its enforcement by the firmware is unclear.

### Command 2 offset field

The 4-byte field in command 2 looks like it could be a record offset for
pagination, but varying it has no observed effect -- the device always
returns the most recent N records starting from record 0. True random-access
pagination into the history buffer may simply not be implemented in the
firmware.

---

## Troubleshooting

### `org.bluez.Error.InProgress` / `Software caused connection abort`

BlueZ rejects new connection attempts while another GATT operation is in
flight on the same adapter. This is common when one device has a large
pending transfer (e.g. a first-time fetch of many days of history) and
another connection is attempted concurrently. Fixes:

- Reduce `--parallel` (1 = fully serial). This seems to be needed on
  old software stack (Ubuntu 20.04 with `bleak<1`).
- Increase `--timeout` so slow transfers complete instead of timing out
  mid-stream. It seems that a large timeout is needed when not syncing
  for a long time, to allow all data to be transferred.

### `Device not found (not in range?)` after a failed connection

BlueZ sometimes leaves stale connection state after an aborted connection,
causing subsequent scans to miss the device even though it's in range:

```bash
bluetoothctl disconnect <MAC>
bluetoothctl remove <MAC>
```

If that doesn't help, restart the Bluetooth stack:

```bash
sudo systemctl restart bluetooth
```

### NOTES: Capturing BLE traffic for protocol debugging

If you need to reverse-engineer further commands (e.g. by comparing
against the official ThermoPro app), an Android HCI snoop log is the most
direct approach:

1. Developer Options -> enable **Bluetooth HCI snoop log** (on some OneUI
   versions, toggling alone isn't enough -- also toggle Bluetooth off/on
   afterwards, or `am force-stop com.android.bluetooth`)
2. Use the official app to perform the action you want to capture
3. **While Bluetooth is still on**, run `adb bugreport bugreport.zip`
   (toggling Bluetooth off first can flush/empty the log before the
   bugreport captures it)
4. Extract the log:
   ```bash
   unzip -p bugreport.zip "FS/data/log/bt/btsnoop_hci.log" > btsnoop_hci.log
   ```
   (the exact path inside the zip varies by device -- use
   `unzip -l bugreport.zip | grep -i snoop` to find it)
5. Open in Wireshark, or parse the BTSnoop format directly (it's a simple
   24-byte-record format documented at
   https://wiki.wireshark.org/Development/BtSnoop)

On some Samsung devices, `adb shell ls /data/log/bt/` returns
`Permission denied` even when the directory exists and is being written --
this is expected; `adb bugreport` runs with sufficient privilege to read it
even though an interactive `adb shell` cannot.

---

## Deployment Notes

- The datetime sync must be sent on every new connection -- it is not
  persisted by the device between sessions. It is not even clear to me how
  this is used.
- **macOS device addressing.** CoreBluetooth (macOS's BLE stack) does not
  expose real MAC addresses to applications -- it returns a per-host
  randomized UUID instead (e.g. `5A641DE0-533F-...`), which differs between
  scans and between machines. `pytp357s` works around this by falling back
  to scanning for the advertised name `TP357S (XXXX)`, where `XXXX` is the
  last two bytes of the configured MAC (see `protocol.find_devices()` and
  `protocol.mac_to_name_suffix()`). This means the same `devices.yaml` with
  real MAC addresses works on both Linux/BlueZ (direct address match) and
  macOS (name-suffix fallback), at the cost of a slower discovery scan
  (15-20s) when the fallback path is used. Two devices sharing the same
  last-two-MAC-bytes would be indistinguishable under this scheme: this
  is extremely unlikely in practice, but we aware of this issue.
- **macOS BLE advertisement visibility.** Individual devices may not appear
  in every scan window -- a 10-second scan can miss devices that a
  20-second scan finds. `find_devices()` defaults to a 20-second fallback
  scan for this reason.
