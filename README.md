# GPS + Ping2 Survey Logger (Raspberry Pi + WebUI)

A Raspberry Pi–friendly logger for:
- USB GPS (NMEA over serial)
- Blue Robotics Ping2 echosounder (Ping Protocol)
- SQLite logging with **Sessions** + **Lines** + **Samples**
- Web UI (Flask + Leaflet) with:
  - Offline MBTiles basemaps (multiple) + opacity slider
  - Swath-style depth coloring + opacity slider
  - Live vessel marker (directional arrow) + live track
  - Raw vs corrected GNSS positions (antenna offset correction)
  - Units toggle (m / ft) for display
  - Survey controls: **Start / Pause-Resume / New Line / Stop**
  - Exports with progress: **CSV / GeoJSON / GeoTIFF**
    - Buttons disabled during export; spinner + progress bar; download links

## Quick Start

### 1) Install dependencies

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

> GeoTIFF export is optional. If you want GeoTIFF support, also install:
```bash
pip install -r requirements-geotiff.txt
```

### 2) Configure basemaps (offline tiles)

Edit `basemaps.json` and set absolute paths to your `.mbtiles` files.

### 3) Run

```bash
python app.py \
  --gps-port /dev/ttyUSB0 \
  --ping-port /dev/ttyUSB1 \
  --sqlite logs.db \
  --basemaps basemaps.json \
  --web-host 0.0.0.0 --web-port 5000 \
  --ant-forward-m 0.0 --ant-right-m 0.0
```

From your remote computer on the same Wi‑Fi network:
- open: `http://<PI_IP>:5000/`

Find Pi IP:
```bash
hostname -I
```

## Notes

- **Survey model**
  - Start Survey: creates a new SQLite session and Line 1
  - Pause/Resume: stops/starts writing samples for the active session
  - New Line: creates a new line; samples are tagged with the current line
  - Stop: stops logging and flushes buffers

- **Antenna offset correction**
  - `--ant-forward-m`: antenna forward of reference point (+bow)
  - `--ant-right-m`: antenna right of reference point (+starboard)
  - Correction uses COG (course over ground) by default

- **Offline basemaps**
  - MBTiles typically store tiles in TMS row order; the server converts XYZ→TMS automatically.

## Repo layout

- `app.py` – main entry (serial readers + logger + web server)
- `survey/` – SQLite + export code
- `web/` – HTML/CSS/JS for the WebUI
- `basemaps.json` – example basemap catalog


## Settings (Ports/Baud)

Use the **⚙ Settings** button in the header to configure GPS/Ping ports and baud rates. Settings are saved to `config.json` and the app will reconnect immediately.


## Download Offline Tiles

Use **Download Map Area** in the sidebar. Draw a rectangle on the map, choose min/max zoom, and download tiles to the Pi as a new `.mbtiles` basemap. The new basemap will appear automatically in the basemap selector.
