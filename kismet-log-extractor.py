#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Export a time / GPS / signal timeline for one MAC from Kismet logs.

Kismet ``*.kismet`` files are SQLite databases. This reads them read-only
and writes one CSV row per observation of the given device:

    python3 kismet-log-extractor.py "AA:BB:CC:DD:EE:FF"
    python3 kismet-log-extractor.py "AA:BB:CC:DD:EE:FF" capture.kismet
    python3 kismet-log-extractor.py -o timeline.csv "AA:BB:CC:DD:EE:FF" survey/

With no log path, every ``*.kismet`` file under the current directory is
searched. Wi-Fi rows come from logged packets (the transmitter MAC).
Bluetooth rows come from Kismet's ``data`` table. Coordinates are the
survey GPS fix stored on that observation; they are left blank when the
log has no fix. If a log never stored per-packet history, the device
record supplies the last sighting and any per-second signal samples
Kismet still has.

Copyright (C) 2026 The Magic Flute contributors
Licensed under GPL-3.0-or-later. See LICENSE.
"""

from __future__ import annotations

import argparse
import bisect
import csv
import json
import sqlite3
import sys
import zlib
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable


CSV_FIELDS = [
    "time",
    "mac",
    "latitude",
    "longitude",
    "altitude_m",
    "signal_dbm",
    "phy",
    "frequency_mhz",
    "source",
    "log",
]

# A GPS snapshot may be a few seconds off the packet that should wear it.
GPS_MATCH_SECONDS = 30


def normalize_mac(text: str) -> str:
    raw = text.strip().lower().replace("-", "").replace(":", "").replace(".", "")
    if len(raw) != 12 or any(c not in "0123456789abcdef" for c in raw):
        raise ValueError(
            f"MAC must be six hex bytes, for example AA:BB:CC:DD:EE:FF (got {text!r})"
        )
    return ":".join(raw[i : i + 2] for i in range(0, 12, 2)).upper()


def is_sqlite(path: Path) -> bool:
    try:
        with path.open("rb") as handle:
            return handle.read(16).startswith(b"SQLite format 3")
    except OSError:
        return False


def find_logs(paths: list[str]) -> list[Path]:
    roots = [Path(p) for p in paths] if paths else [Path(".")]
    found: list[Path] = []
    seen: set[Path] = set()
    missing: list[str] = []
    for root in roots:
        if not root.exists():
            missing.append(str(root))
            continue
        candidates = [root] if root.is_file() else sorted(root.rglob("*.kismet"))
        for candidate in candidates:
            if not candidate.is_file() or candidate.name.startswith("."):
                continue
            resolved = candidate.resolve()
            if resolved in seen or not is_sqlite(candidate):
                continue
            seen.add(resolved)
            found.append(candidate)
    if missing:
        raise FileNotFoundError("path not found: " + ", ".join(missing))
    return found


def load_json(raw: Any) -> dict[str, Any]:
    if not raw:
        return {}
    if isinstance(raw, memoryview):
        raw = raw.tobytes()
    if isinstance(raw, (bytes, bytearray)):
        try:
            raw = zlib.decompress(raw)
        except zlib.error:
            pass
        raw = bytes(raw).decode("utf-8", errors="replace")
    if isinstance(raw, dict):
        return raw
    try:
        parsed = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return {}
    if isinstance(parsed, str):
        try:
            parsed = json.loads(parsed)
        except json.JSONDecodeError:
            return {}
    return parsed if isinstance(parsed, dict) else {}


def valid_coord(lat: Any, lon: Any) -> bool:
    try:
        lat_f = float(lat)
        lon_f = float(lon)
    except (TypeError, ValueError):
        return False
    if lat_f == 0.0 and lon_f == 0.0:
        return False
    return -90.0 <= lat_f <= 90.0 and -180.0 <= lon_f <= 180.0


def fmt_time(ts: Any, usec: Any = 0) -> str:
    try:
        seconds = int(ts)
    except (TypeError, ValueError):
        return ""
    if seconds <= 0:
        return ""
    try:
        moment = datetime.fromtimestamp(seconds, tz=timezone.utc)
    except (OSError, OverflowError, ValueError):
        return str(seconds)
    try:
        micros = int(usec or 0)
    except (TypeError, ValueError):
        micros = 0
    if 0 < micros < 1_000_000:
        moment = moment.replace(microsecond=micros)
    return moment.isoformat()


def fmt_coord(value: Any) -> str:
    if value in (None, ""):
        return ""
    return f"{float(value):.6f}"


def fmt_alt(value: Any) -> str:
    try:
        alt = float(value)
    except (TypeError, ValueError):
        return ""
    # Kismet stores 0 when altitude was not reported.
    if alt == 0.0:
        return ""
    return f"{alt:.1f}"


def fmt_signal(value: Any) -> str:
    try:
        signal = float(value)
    except (TypeError, ValueError):
        return ""
    # Kismet uses 0 as "no signal reading".
    if signal == 0.0:
        return ""
    if abs(signal - round(signal)) < 1e-6:
        return str(int(round(signal)))
    return f"{signal:.1f}"


def fmt_mhz(frequency: Any) -> str:
    try:
        freq = float(frequency)
    except (TypeError, ValueError):
        return ""
    if freq <= 0:
        return ""
    mhz = freq / 1000.0 if freq > 10000 else freq
    return f"{mhz:.3f}"


def log_label(path: Path) -> str:
    try:
        return str(path.resolve().relative_to(Path.cwd().resolve()))
    except ValueError:
        return str(path)


def table_names(cur: sqlite3.Cursor) -> set[str]:
    cur.execute("SELECT name FROM sqlite_master WHERE type='table'")
    return {row[0] for row in cur.fetchall()}


def observation(
    *,
    ts: Any,
    usec: Any,
    mac: str,
    lat: Any,
    lon: Any,
    alt: Any,
    signal: Any,
    phy: str,
    frequency: Any,
    source: str,
    log: str,
) -> dict[str, str] | None:
    stamp = fmt_time(ts, usec)
    if not stamp:
        return None
    if valid_coord(lat, lon):
        latitude, longitude = fmt_coord(lat), fmt_coord(lon)
        altitude = fmt_alt(alt)
    else:
        latitude = longitude = altitude = ""
    return {
        "time": stamp,
        "mac": mac,
        "latitude": latitude,
        "longitude": longitude,
        "altitude_m": altitude,
        "signal_dbm": fmt_signal(signal),
        "phy": phy or "",
        "frequency_mhz": fmt_mhz(frequency),
        "source": source,
        "log": log,
    }


def packet_rows(cur: sqlite3.Cursor, mac: str, log: str) -> list[dict[str, str]]:
    cur.execute(
        """
        SELECT ts_sec, ts_usec, phyname, frequency, lat, lon, alt, signal
        FROM packets
        WHERE upper(sourcemac) = ? OR upper(transmac) = ?
        ORDER BY ts_sec, ts_usec, packetid
        """,
        (mac, mac),
    )
    rows: list[dict[str, str]] = []
    for ts, usec, phy, freq, lat, lon, alt, signal in cur.fetchall():
        row = observation(
            ts=ts,
            usec=usec,
            mac=mac,
            lat=lat,
            lon=lon,
            alt=alt,
            signal=signal,
            phy=phy or "",
            frequency=freq,
            source="packet",
            log=log,
        )
        if row:
            rows.append(row)
    return rows


def data_rows(cur: sqlite3.Cursor, mac: str, log: str) -> list[dict[str, str]]:
    cur.execute(
        """
        SELECT ts_sec, ts_usec, phyname, lat, lon, alt, signal
        FROM data
        WHERE upper(devmac) = ?
        ORDER BY ts_sec, ts_usec
        """,
        (mac,),
    )
    rows: list[dict[str, str]] = []
    for ts, usec, phy, lat, lon, alt, signal in cur.fetchall():
        row = observation(
            ts=ts,
            usec=usec,
            mac=mac,
            lat=lat,
            lon=lon,
            alt=alt,
            signal=signal,
            phy=phy or "",
            frequency="",
            source="advertisement",
            log=log,
        )
        if row:
            rows.append(row)
    return rows


def geopoint(blob: Any) -> tuple[float, float, Any] | None:
    """Kismet geopoints are [longitude, latitude]."""
    if not isinstance(blob, dict):
        return None
    point = blob.get("kismet.common.location.geopoint")
    if not isinstance(point, (list, tuple)) or len(point) < 2:
        return None
    lon, lat = point[0], point[1]
    if not valid_coord(lat, lon):
        return None
    return float(lat), float(lon), blob.get("kismet.common.location.alt")


def gps_track(cur: sqlite3.Cursor, tables: set[str]) -> list[tuple[int, float, float, Any]]:
    points: list[tuple[int, float, float, Any]] = []
    if "snapshots" not in tables:
        return points
    cur.execute(
        "SELECT ts_sec, lat, lon FROM snapshots WHERE snaptype = 'GPS' AND lat != 0 AND lon != 0"
    )
    for ts, lat, lon in cur.fetchall():
        if valid_coord(lat, lon):
            points.append((int(ts), float(lat), float(lon), ""))
    points.sort()
    return points


def nearest_fix(
    track: list[tuple[int, float, float, Any]], ts: int
) -> tuple[float, float, Any] | None:
    if not track:
        return None
    index = bisect.bisect_left(track, (ts,))
    choices = []
    if index < len(track):
        choices.append(track[index])
    if index > 0:
        choices.append(track[index - 1])
    best = min(choices, key=lambda item: abs(item[0] - ts))
    if abs(best[0] - ts) > GPS_MATCH_SECONDS:
        return None
    return best[1], best[2], best[3]


def minute_signal_samples(rrd: Any, first_time: int, last_time: int) -> list[tuple[int, float]]:
    """Per-second signal from Kismet's minute RRD.

    ``minute_vec[i]`` is the sample whose timestamp mod 60 equals ``i``.
    Serialization fast-forwards empty seconds, so only non-zero samples
    inside the device's first/last window are kept.
    """
    if not isinstance(rrd, dict):
        return []
    vec = rrd.get("kismet.common.rrd.minute_vec") or []
    if len(vec) != 60:
        return []
    serial = rrd.get("kismet.common.rrd.serial_time") or rrd.get("kismet.common.rrd.last_time")
    try:
        serial_i = int(serial)
    except (TypeError, ValueError):
        return []
    if serial_i <= 0:
        return []
    blank = float(rrd.get("kismet.common.rrd.blank_val") or 0)
    second = serial_i % 60
    minute_start = serial_i - second
    samples: list[tuple[int, float]] = []
    for index, value in enumerate(vec):
        try:
            signal = float(value)
        except (TypeError, ValueError):
            continue
        if signal == 0.0 or signal == blank:
            continue
        ts = minute_start + index if index <= second else minute_start - 60 + index
        if ts < first_time - 1 or ts > last_time + 1:
            continue
        samples.append((ts, signal))
    return samples


def device_rows(
    cur: sqlite3.Cursor, mac: str, log: str, tables: set[str]
) -> list[dict[str, str]]:
    if "devices" not in tables:
        return []
    cur.execute(
        """
        SELECT first_time, last_time, phyname, strongest_signal,
               avg_lat, avg_lon, device
        FROM devices
        WHERE upper(devmac) = ?
        """,
        (mac,),
    )
    # Fetch before gps_track(), which runs its own query on this cursor.
    device_records = cur.fetchall()
    track = gps_track(cur, tables)
    rows: list[dict[str, str]] = []
    seen_seconds: set[int] = set()

    def add(
        ts: Any,
        usec: Any,
        lat: Any,
        lon: Any,
        alt: Any,
        signal: Any,
        phy: str,
        source: str,
    ) -> None:
        try:
            second = int(ts)
        except (TypeError, ValueError):
            return
        if second in seen_seconds:
            return
        row = observation(
            ts=ts,
            usec=usec,
            mac=mac,
            lat=lat,
            lon=lon,
            alt=alt,
            signal=signal,
            phy=phy,
            frequency="",
            source=source,
            log=log,
        )
        if row:
            seen_seconds.add(second)
            rows.append(row)

    for first_time, last_time, phy, strongest, avg_lat, avg_lon, raw in device_records:
        phy_name = phy or ""
        blob = load_json(raw)
        signal = blob.get("kismet.device.base.signal") or {}
        last_signal = signal.get("kismet.common.signal.last_signal", strongest)
        max_signal = signal.get("kismet.common.signal.max_signal", strongest)
        location = blob.get("kismet.device.base.location") or {}
        last = location.get("kismet.common.location.last") or {}
        peak = signal.get("kismet.common.signal.peak_loc") or {}
        last_pt = geopoint(last)
        peak_pt = geopoint(peak)
        try:
            first_i = int(first_time or 0)
            last_i = int(last_time or 0)
        except (TypeError, ValueError):
            first_i, last_i = 0, 0

        for ts, sample in minute_signal_samples(
            signal.get("kismet.common.signal.signal_rrd"), first_i, last_i or first_i
        ):
            fix = nearest_fix(track, ts)
            if fix is None and last_pt is not None:
                try:
                    loc_ts = int(last.get("kismet.common.location.time_sec") or 0)
                except (TypeError, ValueError):
                    loc_ts = 0
                if loc_ts and abs(loc_ts - ts) <= GPS_MATCH_SECONDS:
                    fix = last_pt
            lat = lon = alt = ""
            if fix is not None:
                lat, lon, alt = fix
            add(ts, 0, lat, lon, alt, sample, phy_name, "signal")

        if peak_pt is not None:
            add(
                peak.get("kismet.common.location.time_sec") or last_time,
                peak.get("kismet.common.location.time_usec") or 0,
                peak_pt[0],
                peak_pt[1],
                peak_pt[2],
                max_signal,
                phy_name,
                "device",
            )
        if last_pt is not None:
            add(
                last.get("kismet.common.location.time_sec") or last_time,
                last.get("kismet.common.location.time_usec") or 0,
                last_pt[0],
                last_pt[1],
                last_pt[2],
                last_signal,
                phy_name,
                "device",
            )
        elif valid_coord(avg_lat, avg_lon):
            add(last_time, 0, avg_lat, avg_lon, "", last_signal or strongest, phy_name, "device")
        else:
            add(last_time or first_time, 0, "", "", "", last_signal or strongest, phy_name, "device")
    return rows


def extract_log(path: Path, mac: str) -> tuple[list[dict[str, str]], str]:
    label = log_label(path)
    conn = sqlite3.connect(f"file:{path.resolve()}?mode=ro", uri=True)
    try:
        cur = conn.cursor()
        tables = table_names(cur)
        rows: list[dict[str, str]] = []
        if "packets" in tables:
            rows.extend(packet_rows(cur, mac, label))
        if "data" in tables:
            rows.extend(data_rows(cur, mac, label))
        if rows:
            packet_n = sum(1 for row in rows if row["source"] == "packet")
            data_n = sum(1 for row in rows if row["source"] == "advertisement")
            parts = []
            if packet_n:
                parts.append(f"{packet_n} packet")
            if data_n:
                parts.append(f"{data_n} advertisement")
            return rows, f"{label}: {', '.join(parts)}"
        fallback = device_rows(cur, mac, label, tables)
        if not fallback:
            return [], f"{label}: MAC not present"
        signal_n = sum(1 for row in fallback if row["source"] == "signal")
        if signal_n:
            note = (
                f"{label}: no per-packet history; {signal_n} per-second signal "
                "sample(s) from the device record"
            )
        else:
            note = f"{label}: no per-packet history; device summary only"
        return fallback, note
    finally:
        conn.close()


def dedupe(rows: Iterable[dict[str, str]]) -> list[dict[str, str]]:
    seen: set[tuple[str, ...]] = set()
    unique: list[dict[str, str]] = []
    for row in rows:
        key = (
            row["time"],
            row["log"],
            row["signal_dbm"],
            row["latitude"],
            row["longitude"],
            row["source"],
            row["phy"],
        )
        if key in seen:
            continue
        seen.add(key)
        unique.append(row)
    unique.sort(key=lambda row: (row["time"], row["log"], row["source"]))
    return unique


def write_csv(rows: list[dict[str, str]], output: str | None) -> None:
    if output:
        handle = open(output, "w", encoding="utf-8", newline="")
    else:
        handle = sys.stdout
    try:
        writer = csv.DictWriter(handle, fieldnames=CSV_FIELDS)
        writer.writeheader()
        writer.writerows(rows)
    finally:
        if output:
            handle.close()


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Export time, GPS (when the log has a fix), and signal strength "
            "for one MAC address from Kismet *.kismet logs."
        )
    )
    parser.add_argument(
        "mac",
        help='MAC address to filter, for example "AA:BB:CC:DD:EE:FF"',
    )
    parser.add_argument(
        "paths",
        nargs="*",
        help="Kismet log file or directory. Default: search the current directory.",
    )
    parser.add_argument(
        "-o",
        "--output",
        help="CSV file to write. Default: print CSV to stdout.",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv if argv is not None else sys.argv[1:])
    try:
        mac = normalize_mac(args.mac)
    except ValueError as exc:
        print(f"kismet-log-extractor: {exc}", file=sys.stderr)
        return 2
    try:
        logs = find_logs(args.paths)
    except FileNotFoundError as exc:
        print(f"kismet-log-extractor: {exc}", file=sys.stderr)
        return 2
    if not logs:
        print(
            "kismet-log-extractor: no *.kismet SQLite logs found.",
            file=sys.stderr,
        )
        return 1

    collected: list[dict[str, str]] = []
    failures = 0
    for path in logs:
        try:
            rows, note = extract_log(path, mac)
        except sqlite3.Error as exc:
            failures += 1
            print(f"kismet-log-extractor: skip {path}: {exc}", file=sys.stderr)
            continue
        # A directory search hits many logs that never saw this MAC.
        if rows or args.paths:
            print(note, file=sys.stderr)
        collected.extend(rows)

    timeline = dedupe(collected)
    try:
        write_csv(timeline, args.output)
    except OSError as exc:
        print(f"kismet-log-extractor: cannot write output: {exc}", file=sys.stderr)
        return 1

    with_gps = sum(1 for row in timeline if row["latitude"])
    destination = args.output or "stdout"
    print(
        f"{mac}: {len(timeline)} observation(s), {with_gps} with GPS, "
        f"from {len(logs) - failures} log(s) -> {destination}",
        file=sys.stderr,
    )
    if not timeline:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
