"""
Plotting routines for pytp357s SQLite databases.

Produces a two-panel (temperature + humidity) overlay of all devices
that have data, either as an interactive matplotlib window or saved
to a PNG file.
"""

from __future__ import annotations

from typing import Any, Optional

import matplotlib

from . import storage

# Default color cycle, used for devices in the order they appear in the config.
DEFAULT_COLORS = [
    "#2196F3",  # blue
    "#FF9800",  # orange
    "#4CAF50",  # green
    "#F44336",  # red
    "#9C27B0",  # purple
    "#009688",  # teal
    "#795548",  # brown
    "#E91E63",  # pink
]


def plot_devices(
    db_path: str,
    devices: dict[str, dict[str, Any]],
    output: Optional[str] = None,
    hours: Optional[float] = None,
) -> "matplotlib.figure.Figure":
    """
    Plot temperature and humidity for all devices with data in ``db_path``.

    Args:
        db_path: path to the SQLite database.
        devices: device config dict (key -> {mac, name, room, ...}), used
            for legend labels and color assignment order.
        output: if given, save the figure to this path (PNG/PDF/etc. based
            on extension) instead of showing it interactively.
        hours: if given, only plot the last N hours of data.

    Returns:
        The matplotlib Figure object.
    """
    if output:
        matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import matplotlib.dates as mdates

    available = storage.list_devices_with_data(db_path)

    fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(14, 7), sharex=True)
    fig.suptitle("TP357S sensor history", fontsize=13, fontweight="bold")

    plotted = 0
    for i, key in enumerate(devices.keys()):
        if key not in available:
            continue
        rows = storage.all_readings(db_path, key, include_gaps=True)
        if not rows:
            continue

        if hours is not None:
            import datetime

            cutoff = datetime.datetime.now() - datetime.timedelta(hours=hours)
            rows = [r for r in rows if r[0] >= cutoff]
            if not rows:
                continue

        info = devices[key]
        label = info.get("name", key)
        room = info.get("room")
        if room:
            label = f"{label} ({room})"

        color = DEFAULT_COLORS[i % len(DEFAULT_COLORS)]

        # Split on gap markers so gaps render as breaks in the line
        times, temps, hums = [], [], []
        for ts, temp, hum, is_gap in rows:
            if is_gap:
                times.append(None)
                temps.append(None)
                hums.append(None)
            else:
                times.append(ts)
                temps.append(temp)
                hums.append(hum)

        ax1.plot(times, temps, color=color, linewidth=1.0, label=label)
        ax2.plot(times, hums, color=color, linewidth=1.0, label=label)
        plotted += 1

    ax1.set_ylabel("Temperature (\u00b0C)")
    ax1.grid(True, alpha=0.3)
    ax1.legend(loc="best", fontsize=9)

    ax2.set_ylabel("Relative Humidity (%)")
    ax2.grid(True, alpha=0.3)

    ax2.xaxis.set_major_formatter(mdates.DateFormatter("%b %d\n%H:%M"))
    fig.autofmt_xdate(rotation=0, ha="center")

    plt.tight_layout(rect=[0, 0, 1, 0.97])

    if output:
        plt.savefig(output, dpi=150, bbox_inches="tight")
    elif plotted == 0:
        print("No data found for any configured device.")
    else:
        plt.show()

    return fig
