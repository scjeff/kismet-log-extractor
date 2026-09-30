# Kismet Log Extractor

Pull a timeline for one MAC address out of a [Kismet](https://www.kismetwireless.net/) log.

Kismet stores a survey in a SQLite file named `*.kismet`. This tool opens those files read-only and writes one CSV row per time that MAC was heard: **time**, **GPS** when the log has a fix, and **signal strength** in dBm.

```bash
python3 kismet-log-extractor.py mywirelesslogfile.kismet "AA:BB:CC:DD:EE:FF"
```

Python 3.9 or newer. Standard library only. No `pip` packages.

**Authorized use only.** The tool reads logs you already have. It does not capture, transmit, associate, pair, or decode payloads. You are responsible for having the right to collect and keep the survey those logs came from.

## Install

Copy the directory, or clone this repository, then run the script with `python3`. Nothing else is required.

```bash
git clone <your-gitea-url>/kismet-log-extractor.git
cd kismet-log-extractor
python3 kismet-log-extractor.py --help
```

## Usage

```bash
# Log file first, then the MAC address
python3 kismet-log-extractor.py mywirelesslogfile.kismet "AA:BB:CC:DD:EE:FF"

# Same order, written to a file
python3 kismet-log-extractor.py -o timeline.csv mywirelesslogfile.kismet "AA:BB:CC:DD:EE:FF"
```

The first argument is one `*.kismet` file. The second is the MAC address to keep. The MAC may use colons, hyphens, or plain hex (`AA:BB:CC:DD:EE:FF`, `aa-bb-cc-dd-ee-ff`, `aabbccddeeff`). Matching is case-insensitive.

CSV goes to stdout unless you pass `-o`. Progress (how many rows, how many have GPS) is printed on stderr, so this still works in a pipeline:

```bash
python3 kismet-log-extractor.py mywirelesslogfile.kismet "AA:BB:CC:DD:EE:FF" > timeline.csv
```

| Exit code | Meaning |
| --- | --- |
| 0 | At least one observation was written |
| 1 | The MAC is absent from that log, or the log could not be read |
| 2 | The log path or the MAC is invalid, or the output file could not be written |

## Output

| Column | Meaning |
| --- | --- |
| `time` | UTC timestamp of the observation (`YYYY-MM-DDTHH:MM:SS.ffffff+00:00`) |
| `mac` | MAC address, uppercase, colon-separated |
| `latitude` | Survey GPS latitude. Blank when that observation has no fix |
| `longitude` | Survey GPS longitude. Blank when that observation has no fix |
| `altitude_m` | Altitude in meters. Blank when Kismet did not report one |
| `signal_dbm` | Received signal strength in dBm. Blank when the log has no reading |
| `phy` | Kismet PHY name, such as `IEEE802.11` or `Bluetooth` |
| `frequency_mhz` | Frequency in MHz when the packet record has one |
| `source` | Where the row came from. See below |
| `log` | Path of the `*.kismet` file, relative to the directory you ran from |

Rows are sorted by time. The same sighting is not written twice.

GPS columns are the survey receiver’s fix at the moment the frame was received.

## Where the rows come from

Kismet does not always keep a full history.

1. **`packet`** — Wi-Fi (and any other PHY) frames whose transmitter address is the MAC you asked for (`sourcemac` or `transmac`). Each frame can carry its own time, signal, and GPS. This is the timeline when Kismet was started with packet logging (`kis_log_packets=true`).
2. **`advertisement`** — Bluetooth and other records in Kismet’s `data` table, matched on `devmac`. Linux Bluetooth sightings usually land here even when the packet row has an empty address.
3. **`signal`** or **`device`** — used only when that log has no per-packet or advertisement rows for the MAC. Kismet’s device record still has the last sighting, the strongest-signal location, and sometimes the last minute of per-second signal samples. Older minutes are not kept in that record.

A log captured with packet logging off will still return a short timeline. A log captured with packet logging on returns one row per frame.

Signal strength is the level Kismet recorded for a frame **sent by** that MAC. Frames addressed to the MAC, but transmitted by someone else, are left out.

## License

Copyright (C) 2026 The Magic Flute contributors.

Licensed under **GPL-3.0-or-later**. See [LICENSE](LICENSE). There is no warranty.
