# pytp357s

Fetch and visualize history from **ThermoPro TP357S** Bluetooth Low Energy
temperature/humidity sensors, without the official app.

The TP357S uses an undocumented, reverse-engineered protocol (different from
the original TP357) -- see [PROTOCOL.md](PROTOCOL.md) for the technical
details, open questions, and known limitations.

## Features

- Fetch full history or live readings over BLE
- Incremental fetching with overlap verification and gap detection
- SQLite storage, ready to use as a [Grafana](https://grafana.com/) data source
- Built-in plotting (interactive or PNG) as a lightweight alternative to Grafana
- Multi-device, with configurable concurrency

## Installation

```bash
pip install pytp357s
```

Or from source:

```bash
git clone https://github.com/giovannipizzi/pytp357s.git
cd pytp357s
pip install .
```

Requires Python 3.9+. Depends on `bleak` (BLE), `PyYAML`, and `matplotlib`.

## Configuration

All commands need a config file describing your devices. Copy the example
and edit it:

```bash
cp examples/devices.example.yaml devices.yaml
```

```yaml
defaults:
  max_fetch_count: 20000   # max records to request when no prior data exists
  overlap: 15
  timeout: 120
  scan_timeout: 20        # BLE discovery timeout (macOS needs ~20s)
  parallel: 2

devices:
  S1:
    mac: "AA:BB:CC:11:22:33"
    name: "Sensor Kitchen"
    room: "Kitchen"
  S2:
    mac: "AA:BB:CC:44:55:66"
    name: "Sensor Bedroom"
    room: "Bedroom"
```

**Finding your devices' MAC addresses** -- run a BLE scan:

```bash
python -c "from bleak import BleakScanner; import asyncio; \
           print(asyncio.run(BleakScanner.discover(timeout=20)))"
```

Look for entries named `TP357S (XXXX)`.
(The last 4 characters `XXXX` in brackets are typically the last 4 characters of the MAC address, e.g. `5566` if the MAC is `AA:BB:CC:44:55:66`).

### Config file location

Commands look for the config file in this order:

1. `--config /path/to/devices.yaml` (explicit, highest priority)
2. `./devices.yaml` (current working directory)
3. `~/.config/pytp357s/devices.yaml`

If none of these exist, the command exits with an error explaining all three
options.

The `defaults:` section sets global defaults for `--count`, `--overlap`,
`--timeout`, and `--parallel`; every value can still be overridden per
command via CLI flags.

## Usage

### Fetch live readings

(Note: This only gets current reading, not the historical data, and uses a different protocol, but it's useful e.g. to check which device is in range).
```bash
pytp357s fetch-data S1 --live
pytp357s fetch-data all --live
```

### Fetch full history (print to stdout, no storage)

```bash
pytp357s fetch-data S1
```

### Fetch full history into a SQLite database

```bash
pytp357s fetch-data all --db sensors.db
```

If `sensors.db` already has data for a device, this is refused (to avoid
accidental duplicate full fetches). Use instead `--incremental` or a different
`--db` path.

### Incremental updates (for cron/scheduled runs)

```bash
pytp357s fetch-data all --db sensors.db --incremental
```

This fetches only records newer than the last stored entry for each device
(plus a small overlap, used to verify continuity), and:

- **refuses to write** if the overlap doesn't match what's stored (possible
  clock issue: if you run the command from different computers, make sure your
  clocks are synchronized!) or if a gap is detected 
  (e.g. if the device was out of range too long and data is not stored anymore
  in the device). 
  Use `--force` to write anyway (a `GAP` marker row is inserted when forcing
  past a detected gap)
- **performs a full first fetch automatically** if the device has no prior
  data in this database

```bash
# Force past a mismatch/gap (e.g. after the device was out of range for a while)
pytp357s fetch-data all --db sensors.db --incremental --force

# Debug an overlap mismatch in detail
pytp357s fetch-data S1 --db sensors.db --incremental --debug-overlap

# Run devices serially instead of 2-at-a-time (helps avoid BLE adapter contention)
pytp357s fetch-data all --db sensors.db --incremental --parallel 1
```

### Update room names / device metadata only

`fetch-data` already syncs device metadata (mac, name, room) into the
database on every run, so this is rarely needed -- but if you've just
edited `devices.yaml` and want the database updated immediately without
a BLE fetch, you can run:

```bash
pytp357s update-rooms --db sensors.db
```

### Plot

```bash
# Interactive window
pytp357s plot --db sensors.db

# Save to PNG
pytp357s plot --db sensors.db --output plot.png

# Last 24 hours only
pytp357s plot --db sensors.db --hours 24
```

## Using as a Python library

```python
import asyncio
from pytp357s import protocol, storage
from pytp357s.config import load_config
from pytp357s.fetcher import process_devices

cfg = load_config("devices.yaml")
device = cfg["devices"]["S1"]

# Live reading
temp, hum = asyncio.run(protocol.ble_fetch_live(device["mac"]))
print(f"{temp:.1f} C, {hum}%")

# Full history for one or more devices (recommended high-level API)
results = asyncio.run(process_devices(
    devices={"S1": device},
    live=False,
    db_path=None,
    incremental=False,
    count=None,
    overlap=0,
    timeout=120,
    scan_timeout=20,
    parallelism=1,
    force=False,
    max_fetch_count=20000,
))
timestamped = results["S1"].data  # list of (datetime, temp, hum)

# Store it
storage.init_db("sensors.db")
storage.append("sensors.db", "S1", timestamped)
```

For the full fetch pipeline (incremental logic, overlap/gap verification),
see `pytp357s.fetcher.process_devices()`.

## Scanning devices
This is useful to see how many devices are in range. Note that this can
only see the instantaneous temperature, to download the historical
data you have to use the functions described above.

```python
import asyncio
from bleak import BleakScanner

async def main():
    # Note: 20 seconds might be needed e.g. on macOS
    # to see all devices
    print("Scanning for 20 seconds...")
    devices = await BleakScanner.discover(timeout=20)
    print(f"\nFound {len(devices)} device(s):\n")
    for d in devices:
        print(f"  {d.address}  {d.name!r}")

    tp357 = [d for d in devices if d.name and "TP357" in d.name]
    print(f"\nTP357 devices found: {len(tp357)}")
    for d in tp357:
        print(f"  {d.address}  {d.name!r}")

asyncio.run(main())
```

## Protocol details

See [PROTOCOL.md](PROTOCOL.md) for the full reverse-engineered protocol
specification, including open questions and known limitations.

## Acknowledgements

This project builds on the work of others:

Inspiration:

- [tpy357](https://pypi.org/project/tpy357/) and [pasky/tp357](https://github.com/pasky/tp357) -- prior TP357 implementations that were an invaluable starting point and reference while reverse-engineering the TP357S's different protocol
- The [BTSnoop format documentation](https://wiki.wireshark.org/Development/BtSnoop) on the Wireshark wiki, used while parsing Android HCI snoop logs during protocol analysis

Dependencies:
- [bleak](https://github.com/hbldh/bleak) -- the Bluetooth LE library this project is built on
- [PyYAML](https://pyyaml.org/) and [matplotlib](https://matplotlib.org/) -- configuration and plotting

Thanks to all of the above for making this possible.

## License

MIT

