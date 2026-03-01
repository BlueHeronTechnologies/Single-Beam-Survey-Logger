#!/usr/bin/env python3
import argparse
import json
import os
import threading
import requests
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

import serial
import pynmea2
from flask import Flask, jsonify, Response, abort, request, send_file

try:
    from bluerobotics_ping.ping1d import Ping1D
except Exception:
    Ping1D = None

from survey.geo import apply_antenna_offset
from survey.sqlite_logger import SQLiteLogger
from survey import exporters


CONFIG_PATH = "config.json"

def load_config_defaults(args) -> dict:
    cfg = {
        "gps_port": args.gps_port,
        "gps_baud": args.gps_baud,
        "ping_port": args.ping_port,
        "ping_baud": args.ping_baud,
    }
    if os.path.exists(CONFIG_PATH):
        try:
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                disk = json.load(f)
            for k in list(cfg.keys()):
                if k in disk and disk[k] is not None:
                    cfg[k] = disk[k]
        except Exception:
            pass
    return cfg

def save_config(cfg: dict) -> None:
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2)



def utc_iso_now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


@dataclass
class GPSState:
    pc_time_utc_iso: str = ""
    nmea_time_utc: str = ""
    lat_deg: Optional[float] = None
    lon_deg: Optional[float] = None
    fix_quality: Optional[int] = None
    num_sats: Optional[int] = None
    hdop: Optional[float] = None
    alt_m: Optional[float] = None
    sog_knots: Optional[float] = None
    cog_deg: Optional[float] = None


@dataclass
class PingState:
    distance_m: Optional[float] = None
    confidence: Optional[int] = None
    ping_number: Optional[int] = None


class SharedState:
    def __init__(self):
        self.lock = threading.Lock()
        self.gps = GPSState()
        self.ping = PingState()
        self.last_gps_update = 0.0
        self.last_ping_update = 0.0


@dataclass
class SurveyStatus:
    active: bool = False
    paused: bool = True
    session_id: Optional[int] = None
    line_id: Optional[int] = None
    line_number: int = 0
    line_label: str = ""


class SurveyController:
    def __init__(self):
        self.lock = threading.Lock()
        self.st = SurveyStatus()

    def snapshot(self) -> dict:
        with self.lock:
            return {
                "active": self.st.active,
                "paused": self.st.paused,
                "session_id": self.st.session_id,
                "line_id": self.st.line_id,
                "line_number": self.st.line_number,
                "line_label": self.st.line_label,
            }

    def can_log(self) -> bool:
        with self.lock:
            return self.st.active and (not self.st.paused)

    def set_started(self, session_id: int, line_id: int, line_number: int, line_label: str):
        with self.lock:
            self.st.active = True
            self.st.paused = False
            self.st.session_id = session_id
            self.st.line_id = line_id
            self.st.line_number = line_number
            self.st.line_label = line_label

    def set_paused(self, paused: bool):
        with self.lock:
            if not self.st.active:
                return
            self.st.paused = paused

    def set_stopped(self):
        with self.lock:
            self.st.paused = True
            self.st.active = False


class GPSReader:
    def __init__(self, shared: SharedState):
        self.shared = shared
        self._stop = threading.Event()
        self._thread = None
        self.port = None
        self.baud = None
        self.status = "stopped"

    def start(self, port: str, baud: int):
        self.stop()
        self._stop.clear()
        self.port, self.baud = port, int(baud)
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()

    def _run(self):
        try:
            ser = serial.Serial(self.port, baudrate=self.baud, timeout=1.0)
            self.status = f"connected {self.port}@{self.baud}"
        except Exception as e:
            self.status = f"error opening {self.port}: {e}"
            return

        while not self._stop.is_set():
            try:
                line = ser.readline().decode(errors="ignore").strip()
                if not line.startswith("$"):
                    continue
                try:
                    msg = pynmea2.parse(line)
                except pynmea2.ParseError:
                    continue

                with self.shared.lock:
                    self.shared.gps.pc_time_utc_iso = utc_iso_now()

                    if msg.sentence_type == "GGA":
                        self.shared.gps.nmea_time_utc = str(getattr(msg, "timestamp", "") or "")
                        self.shared.gps.lat_deg = getattr(msg, "latitude", None)
                        self.shared.gps.lon_deg = getattr(msg, "longitude", None)
                        try: self.shared.gps.fix_quality = int(getattr(msg, "gps_qual", "") or 0)
                        except Exception: self.shared.gps.fix_quality = None
                        try: self.shared.gps.num_sats = int(getattr(msg, "num_sats", "") or 0)
                        except Exception: self.shared.gps.num_sats = None
                        try: self.shared.gps.hdop = float(getattr(msg, "horizontal_dil", "") or 0.0)
                        except Exception: self.shared.gps.hdop = None
                        try: self.shared.gps.alt_m = float(getattr(msg, "altitude", "") or 0.0)
                        except Exception: self.shared.gps.alt_m = None
                        self.shared.last_gps_update = time.time()

                    elif msg.sentence_type == "RMC":
                        self.shared.gps.nmea_time_utc = str(getattr(msg, "timestamp", "") or "")
                        self.shared.gps.lat_deg = getattr(msg, "latitude", None)
                        self.shared.gps.lon_deg = getattr(msg, "longitude", None)
                        try: self.shared.gps.sog_knots = float(getattr(msg, "spd_over_grnd", "") or 0.0)
                        except Exception: self.shared.gps.sog_knots = None
                        try: self.shared.gps.cog_deg = float(getattr(msg, "true_course", "") or 0.0)
                        except Exception: self.shared.gps.cog_deg = None
                        if getattr(msg, "status", "") == "A":
                            self.shared.last_gps_update = time.time()
            except Exception:
                time.sleep(0.2)

        try:
            ser.close()
        except Exception:
            pass
        self.status = "stopped"


class PingReader:
    def __init__(self, shared: SharedState, poll_hz: float):
        self.shared = shared
        self.poll_hz = poll_hz
        self._stop = threading.Event()
        self._thread = None
        self.port = None
        self.baud = None
        self.status = "stopped"

    def start(self, port: str, baud: int):
        self.stop()
        self._stop.clear()
        self.port, self.baud = port, int(baud)
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()

    def _run(self):
        if Ping1D is None:
            self.status = "missing bluerobotics-ping"
            return

        ping = Ping1D()
        try:
            ok = ping.connect_serial(self.port, self.baud)
        except Exception as e:
            self.status = f"error opening {self.port}: {e}"
            return
        if not ok:
            self.status = f"connect failed {self.port}"
            return

        self.status = f"connected {self.port}@{self.baud}"
        period = 1.0 / max(0.5, self.poll_hz)

        while not self._stop.is_set():
            try:
                data = ping.get_distance()
                with self.shared.lock:
                    dist = data.get("distance")
                    if dist is not None:
                        self.shared.ping.distance_m = float(dist) / 1000.0 if dist > 50 else float(dist)
                    conf = data.get("confidence")
                    self.shared.ping.confidence = int(conf) if conf is not None else None
                    pn = data.get("ping_number")
                    self.shared.ping.ping_number = int(pn) if pn is not None else None
                    self.shared.last_ping_update = time.time()
            except Exception:
                time.sleep(0.2)
            time.sleep(period)

        self.status = "stopped"


def gps_reader_thread(state: SharedState, port: str, baud: int, timeout: float = 1.0):
    try:
        ser = serial.Serial(port, baudrate=baud, timeout=timeout)
    except Exception as e:
        print(f"[GPS] Failed to open {port}: {e}")
        return

    print(f"[GPS] Reading NMEA from {port} @ {baud} ...")
    while True:
        try:
            line = ser.readline().decode(errors="ignore").strip()
            if not line.startswith("$"):
                continue
            try:
                msg = pynmea2.parse(line)
            except pynmea2.ParseError:
                continue

            with state.lock:
                state.gps.pc_time_utc_iso = utc_iso_now()
                if msg.sentence_type == "GGA":
                    state.gps.nmea_time_utc = str(getattr(msg, "timestamp", "") or "")
                    state.gps.lat_deg = getattr(msg, "latitude", None)
                    state.gps.lon_deg = getattr(msg, "longitude", None)
                    try: state.gps.fix_quality = int(getattr(msg, "gps_qual", "") or 0)
                    except Exception: state.gps.fix_quality = None
                    try: state.gps.num_sats = int(getattr(msg, "num_sats", "") or 0)
                    except Exception: state.gps.num_sats = None
                    try: state.gps.hdop = float(getattr(msg, "horizontal_dil", "") or 0.0)
                    except Exception: state.gps.hdop = None
                    try: state.gps.alt_m = float(getattr(msg, "altitude", "") or 0.0)
                    except Exception: state.gps.alt_m = None
                    state.last_gps_update = time.time()

                elif msg.sentence_type == "RMC":
                    state.gps.nmea_time_utc = str(getattr(msg, "timestamp", "") or "")
                    state.gps.lat_deg = getattr(msg, "latitude", None)
                    state.gps.lon_deg = getattr(msg, "longitude", None)
                    try: state.gps.sog_knots = float(getattr(msg, "spd_over_grnd", "") or 0.0)
                    except Exception: state.gps.sog_knots = None
                    try: state.gps.cog_deg = float(getattr(msg, "true_course", "") or 0.0)
                    except Exception: state.gps.cog_deg = None
                    if getattr(msg, "status", "") == "A":
                        state.last_gps_update = time.time()

        except Exception as e:
            print(f"[GPS] Error: {e}")
            time.sleep(0.2)


def ping_reader_thread(state: SharedState, port: str, baud: int, poll_hz: float = 10.0):
    if Ping1D is None:
        print("[PING] Missing Ping1D. Install: pip install bluerobotics-ping")
        return

    ping = Ping1D()
    try:
        ok = ping.connect_serial(port, baud)
    except Exception as e:
        print(f"[PING] Failed to open {port}: {e}")
        return
    if not ok:
        print(f"[PING] connect_serial() returned False for {port}")
        return

    print(f"[PING] Reading Ping2 from {port} @ {baud} ...")
    period = 1.0 / max(0.5, poll_hz)

    while True:
        try:
            data = ping.get_distance()
            with state.lock:
                dist = data.get("distance")
                if dist is not None:
                    state.ping.distance_m = float(dist) / 1000.0 if dist > 50 else float(dist)
                conf = data.get("confidence")
                state.ping.confidence = int(conf) if conf is not None else None
                pn = data.get("ping_number")
                state.ping.ping_number = int(pn) if pn is not None else None
                state.last_ping_update = time.time()
        except Exception as e:
            print(f"[PING] Error: {e}")
            time.sleep(0.2)
        time.sleep(period)


def load_basemaps(path: str) -> dict:
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    for k, v in data.items():
        if "path" not in v:
            raise ValueError(f"Basemap '{k}' missing 'path'")
        v.setdefault("label", k)
        v.setdefault("format", "png")
    return data


# ===== Offline tile downloader (MBTiles) =====
import math
import sqlite3
import uuid

def _deg2num(lat_deg: float, lon_deg: float, zoom: int):
    lat_rad = math.radians(lat_deg)
    n = 2.0 ** zoom
    xtile = int((lon_deg + 180.0) / 360.0 * n)
    ytile = int((1.0 - math.log(math.tan(lat_rad) + 1 / math.cos(lat_rad)) / math.pi) / 2.0 * n)
    return xtile, ytile

def _clamp(v, lo, hi): 
    return lo if v < lo else hi if v > hi else v

def estimate_tile_count(bounds, min_zoom: int, max_zoom: int) -> int:
    south, west, north, east = bounds
    total = 0
    for z in range(min_zoom, max_zoom + 1):
        x_min, y_s = _deg2num(south, west, z)
        x_max, y_n = _deg2num(north, east, z)
        x0, x1 = sorted((x_min, x_max))
        y0, y1 = sorted((y_n, y_s))
        total += (x1 - x0 + 1) * (y1 - y0 + 1)
    return int(total)

class TileDownloadManager:
    def __init__(self, basemaps_json_path: str, basemaps_dir: str = "basemaps"):
        self.lock = threading.Lock()
        self.jobs = {}
        self.basemaps_json_path = basemaps_json_path
        self.basemaps_dir = basemaps_dir
        os.makedirs(self.basemaps_dir, exist_ok=True)

    def start(self, *, bounds, min_zoom: int, max_zoom: int, name: str, source: str = "osm"):
        job_id = uuid.uuid4().hex
        cancel_ev = threading.Event()
        with self.lock:
            self.jobs[job_id] = {"status": "running", "progress": 0, "message": "Queued", "total": 0, "done": 0, "name": name, "source": source, "cancel_ev": cancel_ev, "error": None}
        t = threading.Thread(target=self._run, args=(job_id, bounds, min_zoom, max_zoom, name, source, cancel_ev), daemon=True)
        t.start()
        return job_id

    def cancel(self, job_id: str) -> bool:
        with self.lock:
            j = self.jobs.get(job_id)
            if not j: return False
            j["cancel_ev"].set()
            j["message"] = "Cancel requested…"
            return True

    def status(self, job_id: str):
        with self.lock:
            j = self.jobs.get(job_id)
            if not j: return None
            # return a serializable view (no Event)
            total = int(j.get("total") or 0)
            done = int(j.get("done") or 0)
            progress = int((done / total) * 100) if total > 0 else int(j.get("progress") or 0)
            return {
                "status": j.get("status"),
                "progress": progress,
                "message": j.get("message"),
                "total": total,
                "done": done,
                "name": j.get("name"),
                "source": j.get("source"),
                "error": j.get("error"),
            }

    def _tile_url(self, source: str, z: int, x: int, y: int) -> str:
        # y is XYZ scheme
        if source == "osm":
            return f"https://tile.openstreetmap.org/{z}/{x}/{y}.png"
        return f"https://tile.openstreetmap.org/{z}/{x}/{y}.png"

    def _run(self, job_id: str, bounds, min_zoom: int, max_zoom: int, name: str, source: str, cancel_ev: threading.Event):
        south, west, north, east = bounds
        min_zoom = int(_clamp(min_zoom, 0, 19))
        max_zoom = int(_clamp(max_zoom, 0, 19))
        if max_zoom < min_zoom:
            min_zoom, max_zoom = max_zoom, min_zoom

        # Build tile list
        tiles = []
        for z in range(min_zoom, max_zoom + 1):
            x_min, y_s = _deg2num(south, west, z)
            x_max, y_n = _deg2num(north, east, z)
            x0, x1 = sorted((x_min, x_max))
            y0, y1 = sorted((y_n, y_s))  # y_n is smaller (north), y_s larger (south)
            for x in range(x0, x1 + 1):
                for y in range(y0, y1 + 1):
                    tiles.append((z, x, y))

        mbtiles_path = os.path.join(self.basemaps_dir, f"{name}.mbtiles")
        try:
            if os.path.exists(mbtiles_path):
                # prevent overwrite; create unique suffix
                base = name
                k = 1
                while os.path.exists(os.path.join(self.basemaps_dir, f"{base}_{k}.mbtiles")):
                    k += 1
                name = f"{base}_{k}"
                mbtiles_path = os.path.join(self.basemaps_dir, f"{name}.mbtiles")

            with self.lock:
                self.jobs[job_id]["total"] = len(tiles)
                self.jobs[job_id]["done"] = 0
                self.jobs[job_id]["message"] = f"Downloading {len(tiles)} tiles…"

            conn = sqlite3.connect(mbtiles_path)
            conn.execute("PRAGMA synchronous=OFF;")
            conn.execute("PRAGMA journal_mode=WAL;")
            conn.execute("CREATE TABLE IF NOT EXISTS tiles (zoom_level INTEGER, tile_column INTEGER, tile_row INTEGER, tile_data BLOB);")
            conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS tile_index ON tiles (zoom_level, tile_column, tile_row);")
            conn.execute("CREATE TABLE IF NOT EXISTS metadata (name TEXT, value TEXT);")
            conn.commit()

            # Metadata (basic)
            meta = {
                "name": name,
                "format": "png",
                "type": "baselayer",
                "minzoom": str(min_zoom),
                "maxzoom": str(max_zoom),
                "bounds": f"{west},{south},{east},{north}",
            }
            conn.execute("DELETE FROM metadata;")
            for k, v in meta.items():
                conn.execute("INSERT INTO metadata (name, value) VALUES (?, ?);", (k, str(v)))
            conn.commit()

            sess = requests.Session()
            sess.headers.update({
                "User-Agent": "gps-ping-survey/1.0 (offline tile download; local field unit)"
            })

            ok_count = 0
            for i, (z, x, y) in enumerate(tiles, start=1):
                if cancel_ev.is_set():
                    with self.lock:
                        self.jobs[job_id]["status"] = "canceled"
                        self.jobs[job_id]["message"] = "Canceled."
                    conn.close()
                    try: os.remove(mbtiles_path)
                    except Exception: pass
                    return

                url = self._tile_url(source, z, x, y)
                try:
                    r = sess.get(url, timeout=10)
                    if r.status_code == 200 and r.content:
                        # Convert XYZ y to TMS tile_row
                        tms_y = (2 ** z - 1) - y
                        conn.execute("INSERT OR REPLACE INTO tiles VALUES (?, ?, ?, ?);", (z, x, tms_y, sqlite3.Binary(r.content)))
                        ok_count += 1
                    else:
                        # skip missing
                        pass
                except Exception:
                    pass

                if i % 25 == 0:
                    conn.commit()

                with self.lock:
                    self.jobs[job_id]["done"] = i
                    self.jobs[job_id]["message"] = f"Downloaded {i}/{len(tiles)} tiles ({ok_count} ok)"

                # Be polite to public servers
                time.sleep(0.05)

            conn.commit()
            conn.close()

            # Update basemaps.json to include new entry
            try:
                with open(self.basemaps_json_path, "r", encoding="utf-8") as f:
                    bm = json.load(f)
            except Exception:
                bm = {}

            map_id = name
            bm[map_id] = {"path": mbtiles_path, "label": f"{name} (offline)", "format": "png"}
            with open(self.basemaps_json_path, "w", encoding="utf-8") as f:
                json.dump(bm, f, indent=2)

            with self.lock:
                self.jobs[job_id]["status"] = "done"
                self.jobs[job_id]["message"] = f"Done. Saved {ok_count} tiles to {mbtiles_path}."
        except Exception as e:
            with self.lock:
                self.jobs[job_id]["status"] = "error"
                self.jobs[job_id]["error"] = str(e)
                self.jobs[job_id]["message"] = str(e)
            try:
                if os.path.exists(mbtiles_path):
                    os.remove(mbtiles_path)
            except Exception:
                pass


def start_web_ui(shared: SharedState, survey: SurveyController, sqlite_logger: Optional[SQLiteLogger], sqlite_path: Optional[str],
                 basemaps: dict, ant_forward_m: float, ant_right_m: float, stale_seconds: float, host: str, port: int,
                 config: dict, gps_mgr: GPSReader, ping_mgr: PingReader):
    app = Flask(__name__, static_folder="web", static_url_path="/static")
    app.basemaps = basemaps
    tile_mgr = TileDownloadManager(basemaps_json_path=os.path.abspath(os.environ.get('BASEMAPS_JSON', 'basemaps.json')),
                                 basemaps_dir=os.path.abspath('basemaps'))

    def mbtiles_get_tile(mbtiles_path: str, z: int, x: int, y_xyz: int):
        import sqlite3
        tms_y = (2 ** z - 1) - y_xyz
        conn = sqlite3.connect(mbtiles_path)
        try:
            cur = conn.execute("SELECT tile_data FROM tiles WHERE zoom_level=? AND tile_column=? AND tile_row=?", (z, x, tms_y))
            row = cur.fetchone()
            return row[0] if row else None
        finally:
            conn.close()

    @app.get("/basemaps")
    def basemaps_endpoint():
        out = []
        for map_id, cfg in app.basemaps.items():
            out.append({"id": map_id, "label": cfg.get("label", map_id), "format": cfg.get("format", "png")})
        return jsonify(out)


@app.post("/basemaps/reload")
def basemaps_reload():
    try:
        app.basemaps = load_basemaps(os.environ.get("BASEMAPS_JSON", "basemaps.json"))
        return jsonify({"ok": True, "count": len(app.basemaps)})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


    @app.get("/tiles/<map_id>/<int:z>/<int:x>/<int:y>.<ext>")
    def tiles(map_id: str, z: int, x: int, y: int, ext: str):
        cfg = app.basemaps.get(map_id)
        if not cfg:
            abort(404)
        fmt = cfg.get("format", "png").lower()
        if ext.lower() != fmt:
            abort(404)
        data = mbtiles_get_tile(cfg["path"], z, x, y)
        if data is None:
            abort(404)
        mimetype = "image/png" if fmt == "png" else "image/jpeg"
        return Response(data, mimetype=mimetype)



@app.post("/tilepack/estimate")
def tilepack_estimate():
    data = request.get_json(force=True) or {}
    bounds = data.get("bounds")
    if not bounds or len(bounds) != 4:
        return jsonify({"error": "bounds must be [south, west, north, east]"}), 400
    min_zoom = int(data.get("min_zoom", 12))
    max_zoom = int(data.get("max_zoom", 16))
    count = estimate_tile_count(bounds, min_zoom, max_zoom)
    # crude estimate ~20 KB per PNG tile
    est_bytes = int(count * 20_000)
    return jsonify({"tile_count": count, "estimated_bytes": est_bytes})

@app.post("/tilepack/start")
def tilepack_start():
    data = request.get_json(force=True) or {}
    bounds = data.get("bounds")
    if not bounds or len(bounds) != 4:
        return jsonify({"error": "bounds must be [south, west, north, east]"}), 400
    name = (data.get("name") or "offline_tiles").strip()
    min_zoom = int(data.get("min_zoom", 12))
    max_zoom = int(data.get("max_zoom", 16))
    source = (data.get("source") or "osm").strip()
    job_id = tile_mgr.start(bounds=bounds, min_zoom=min_zoom, max_zoom=max_zoom, name=name, source=source)
    return jsonify({"job_id": job_id})

@app.get("/tilepack/status/<job_id>")
def tilepack_status(job_id: str):
    st = tile_mgr.status(job_id)
    if not st:
        return jsonify({"error": "unknown job"}), 404
    return jsonify(st)

@app.post("/tilepack/cancel/<job_id>")
def tilepack_cancel(job_id: str):
    ok = tile_mgr.cancel(job_id)
    if not ok:
        return jsonify({"error": "unknown job"}), 404
    return jsonify({"ok": True})


    @app.get("/")
    def index():
        with open(os.path.join("web", "index.html"), "r", encoding="utf-8") as f:
            return Response(f.read(), mimetype="text/html")

    @app.get("/data")
    def data():
        now = time.time()
        with shared.lock:
            gps_age = now - shared.last_gps_update if shared.last_gps_update else 1e9
            ping_age = now - shared.last_ping_update if shared.last_ping_update else 1e9

            raw_lat = shared.gps.lat_deg
            raw_lon = shared.gps.lon_deg
            cog = shared.gps.cog_deg
            corr_lat, corr_lon = apply_antenna_offset(raw_lat, raw_lon, cog, ant_forward_m, ant_right_m)

            payload = {
                "pc_time_utc_iso": utc_iso_now(),
                "gps_raw_lat_deg": raw_lat,
                "gps_raw_lon_deg": raw_lon,
                "gps_corr_lat_deg": corr_lat,
                "gps_corr_lon_deg": corr_lon,
                "gps_nmea_time_utc": shared.gps.nmea_time_utc,
                "gps_fix_quality": shared.gps.fix_quality,
                "gps_num_sats": shared.gps.num_sats,
                "gps_hdop": shared.gps.hdop,
                "gps_alt_m": shared.gps.alt_m,
                "gps_sog_knots": shared.gps.sog_knots,
                "gps_cog_deg": cog,
                "ping_distance_m": shared.ping.distance_m,
                "ping_confidence": shared.ping.confidence,
                "ping_ping_number": shared.ping.ping_number,
                "gps_age_s": float(gps_age),
                "ping_age_s": float(ping_age),
            }
        payload["gps_stale"] = payload["gps_age_s"] > stale_seconds
        payload["ping_stale"] = payload["ping_age_s"] > stale_seconds
        return jsonify(payload)


@app.get("/config")
def get_config():
    return jsonify({
        "gps_port": config.get("gps_port", ""),
        "gps_baud": config.get("gps_baud", 9600),
        "ping_port": config.get("ping_port", ""),
        "ping_baud": config.get("ping_baud", 115200),
        "gps_status": gps_mgr.status,
        "ping_status": ping_mgr.status,
    })

@app.post("/config")
def set_config_endpoint():
    data = request.get_json(force=True) or {}
    gp = (data.get("gps_port") or "").strip()
    gb = int(data.get("gps_baud") or 9600)
    pp = (data.get("ping_port") or "").strip()
    pb = int(data.get("ping_baud") or 115200)

    if not gp or not pp:
        return jsonify({"error": "gps_port and ping_port are required"}), 400

    config.update({"gps_port": gp, "gps_baud": gb, "ping_port": pp, "ping_baud": pb})
    save_config(config)

    gps_mgr.start(gp, gb)
    ping_mgr.start(pp, pb)

    return jsonify({"ok": True, "gps_status": gps_mgr.status, "ping_status": ping_mgr.status, **config})


    @app.get("/survey/status")
    def survey_status():
        return jsonify(survey.snapshot())

    @app.post("/survey/start")
    def survey_start():
        if sqlite_logger is None or sqlite_path is None:
            return jsonify({"error": "SQLite not configured"}), 400

        data = request.get_json(force=True) or {}
        notes = (data.get("notes") or "").strip()
        line_label = (data.get("line_label") or "Line 1").strip()

        st = survey.snapshot()
        if st["active"]:
            return jsonify({"error": "Survey already active"}), 400

        now_local = datetime.now().astimezone()
        now_utc = datetime.now(timezone.utc)

        session_meta = dict(
            start_utc=now_utc.strftime("%Y-%m-%dT%H:%M:%SZ"),
            start_local=now_local.strftime("%Y-%m-%d %H:%M:%S"),
            tz_abbr=now_local.strftime("%Z") or "LOCAL",
            utc_offset=f"UTC{now_local.strftime('%z')}",
            gps_port=os.environ.get("GPS_PORT", ""),
            gps_baud=int(os.environ.get("GPS_BAUD", "9600")),
            ping_port=os.environ.get("PING_PORT", ""),
            ping_baud=int(os.environ.get("PING_BAUD", "115200")),
            log_hz=float(os.environ.get("LOG_HZ", "2.0")),
            ping_hz=float(os.environ.get("PING_HZ", "10.0")),
            notes=notes,
        )

        sid = sqlite_logger.start_session(session_meta)
        lid, lnum = sqlite_logger.start_new_line(line_label)
        survey.set_started(sid, lid, lnum, line_label)
        return jsonify({"status": "started", **survey.snapshot()})

    @app.post("/survey/pause")
    def survey_pause():
        st = survey.snapshot()
        if not st["active"]:
            return jsonify({"error": "No active survey"}), 400
        survey.set_paused(True)
        if sqlite_logger is not None:
            sqlite_logger.flush()
        return jsonify({"status": "paused", **survey.snapshot()})

    @app.post("/survey/resume")
    def survey_resume():
        st = survey.snapshot()
        if not st["active"]:
            return jsonify({"error": "No active survey"}), 400
        survey.set_paused(False)
        return jsonify({"status": "resumed", **survey.snapshot()})

    @app.post("/survey/new_line")
    def survey_new_line():
        if sqlite_logger is None:
            return jsonify({"error": "SQLite not configured"}), 400
        st = survey.snapshot()
        if not st["active"]:
            return jsonify({"error": "No active survey"}), 400
        data = request.get_json(force=True) or {}
        label = (data.get("label") or "").strip()
        lid, lnum = sqlite_logger.start_new_line(label or f"Line {st['line_number'] + 1}")
        with survey.lock:
            survey.st.line_id = lid
            survey.st.line_number = lnum
            survey.st.line_label = label or f"Line {lnum}"
        return jsonify({"status": "new_line", **survey.snapshot()})

    @app.post("/survey/stop")
    def survey_stop():
        st = survey.snapshot()
        if not st["active"]:
            return jsonify({"error": "No active survey"}), 400
        survey.set_stopped()
        if sqlite_logger is not None:
            sqlite_logger.flush()
        return jsonify({"status": "stopped", **survey.snapshot()})

    export_lock = threading.Lock()
    export_jobs = {}

    def job_update(job_id: str, **kwargs):
        with export_lock:
            export_jobs[job_id].update(kwargs)

    @app.post("/export/start")
    def export_start():
        if sqlite_path is None:
            return jsonify({"error": "SQLite not configured"}), 400

        st = survey.snapshot()
        session_id = st.get("session_id")
        if session_id is None:
            return jsonify({"error": "No session to export. Start a survey first."}), 400

        data = request.get_json(force=True) or {}
        fmt = data.get("format", "csv")
        export_dir = data.get("export_dir", "exports")
        grid_m = float(data.get("grid_m", 2.0))
        method = data.get("method", "mean")
        pos_source = data.get("position_source", "corr")
        use_corr = (pos_source != "raw")

        import uuid
        job_id = uuid.uuid4().hex
        with export_lock:
            export_jobs[job_id] = {"status": "running", "progress": 0, "message": "Queued", "outputs": []}

        def runner():
            try:
                os.makedirs(export_dir, exist_ok=True)
                base = f"session_{session_id}_{datetime.now().astimezone().strftime('%Y%m%d_%H%M%S_%Z')}_UTC{datetime.now().astimezone().strftime('%z')}"
                outs = []

                def setp(p, msg=""):
                    job_update(job_id, progress=int(p), message=msg)

                setp(5, "Starting export...")

                if fmt in ("csv", "all"):
                    setp(15, "Exporting CSV...")
                    out_csv = os.path.join(export_dir, f"{base}.csv")
                    exporters.export_csv(sqlite_path, session_id, out_csv)
                    outs.append({"label": "CSV", "path": os.path.abspath(out_csv)})
                    setp(25, "CSV complete.")

                if fmt in ("geojson", "all"):
                    setp(35, "Exporting GeoJSON...")
                    out_gj = os.path.join(export_dir, f"{base}.geojson")
                    exporters.export_geojson(sqlite_path, session_id, out_gj, use_corrected=use_corr)
                    outs.append({"label": "GeoJSON", "path": os.path.abspath(out_gj)})
                    setp(55, "GeoJSON complete.")

                if fmt in ("geotiff", "all"):
                    setp(65, "Exporting GeoTIFF (gridding depth)...")
                    out_tif = os.path.join(export_dir, f"{base}_depth.tif")
                    exporters.export_geotiff_depth(sqlite_path, session_id, out_tif, grid_m=grid_m, method=method, use_corrected=use_corr)
                    outs.append({"label": "GeoTIFF", "path": os.path.abspath(out_tif)})
                    setp(95, "GeoTIFF complete.")

                job_update(job_id, status="done", progress=100, message="Export complete.", outputs=outs)
            except Exception as e:
                job_update(job_id, status="error", progress=100, message=str(e), outputs=[])

        threading.Thread(target=runner, daemon=True).start()
        return jsonify({"job_id": job_id})

    @app.get("/export/status/<job_id>")
    def export_status(job_id: str):
        with export_lock:
            j = export_jobs.get(job_id)
        if not j:
            return jsonify({"error": "unknown job"}), 404
        return jsonify(j)

    @app.get("/export/download")
    def export_download():
        path = request.args.get("path", "")
        if not path:
            return jsonify({"error": "missing path"}), 400

        export_root = os.path.abspath("exports")
        abs_path = os.path.abspath(path)
        if not abs_path.startswith(export_root + os.sep):
            return jsonify({"error": "forbidden"}), 403
        if not os.path.exists(abs_path):
            return jsonify({"error": "not found"}), 404
        return send_file(abs_path, as_attachment=True)

    print(f"[WEB] Serving on http://{host}:{port}/")
    app.run(host=host, port=port, debug=False, threaded=True, use_reloader=False)


def main():
    ap = argparse.ArgumentParser(description="Raspberry Pi GPS + Ping2 survey logger with WebUI.")
    ap.add_argument("--gps-port", required=True)
    ap.add_argument("--gps-baud", type=int, default=9600)
    ap.add_argument("--ping-port", required=True)
    ap.add_argument("--ping-baud", type=int, default=115200)
    ap.add_argument("--log-hz", type=float, default=2.0)
    ap.add_argument("--ping-hz", type=float, default=10.0)
    ap.add_argument("--stale-seconds", type=float, default=2.0)
    ap.add_argument("--sqlite", default="logs.db", help="SQLite DB path")
    ap.add_argument("--sqlite-batch", type=int, default=50)
    ap.add_argument("--basemaps", default="basemaps.json", help="Basemap catalog JSON")
    ap.add_argument("--web-host", default="0.0.0.0")
    ap.add_argument("--web-port", type=int, default=5000)
    ap.add_argument("--ant-forward-m", type=float, default=0.0)
    ap.add_argument("--ant-right-m", type=float, default=0.0)
    args = ap.parse_args()

    os.environ["GPS_PORT"] = args.gps_port
    os.environ["GPS_BAUD"] = str(args.gps_baud)
    os.environ["PING_PORT"] = args.ping_port
    os.environ["PING_BAUD"] = str(args.ping_baud)
    os.environ["LOG_HZ"] = str(args.log_hz)
    os.environ["BASEMAPS_JSON"] = args.basemaps
    os.environ["PING_HZ"] = str(args.ping_hz)

    basemaps = load_basemaps(args.basemaps)

    cfg = load_config_defaults(args)
    save_config(cfg)

    shared = SharedState()
    survey = SurveyController()

    gps_mgr = GPSReader(shared)
    ping_mgr = PingReader(shared, poll_hz=args.ping_hz)
    gps_mgr.start(cfg["gps_port"], cfg["gps_baud"])
    ping_mgr.start(cfg["ping_port"], cfg["ping_baud"])

    sqlite_logger = SQLiteLogger(args.sqlite, batch_size=args.sqlite_batch) if args.sqlite else None

    threading.Thread(
        target=start_web_ui,
        args=(shared, survey, sqlite_logger, args.sqlite, basemaps, args.ant_forward_m, args.ant_right_m, args.stale_seconds, args.web_host, args.web_port, cfg, gps_mgr, ping_mgr),
        daemon=True
    ).start()

    print("[LOG] Waiting for survey start from WebUI...")
    period = 1.0 / max(0.1, args.log_hz)

    try:
        while True:
            now = time.time()
            with shared.lock:
                gps_age = now - shared.last_gps_update if shared.last_gps_update else 1e9
                ping_age = now - shared.last_ping_update if shared.last_ping_update else 1e9
                gps_stale = gps_age > args.stale_seconds
                ping_stale = ping_age > args.stale_seconds

                raw_lat = shared.gps.lat_deg
                raw_lon = shared.gps.lon_deg
                cog = shared.gps.cog_deg
                corr_lat, corr_lon = apply_antenna_offset(raw_lat, raw_lon, cog, args.ant_forward_m, args.ant_right_m)

                row = {
                    "pc_time_utc_iso": utc_iso_now(),
                    "gps_nmea_time_utc": shared.gps.nmea_time_utc,
                    "gps_raw_lat_deg": raw_lat,
                    "gps_raw_lon_deg": raw_lon,
                    "gps_corr_lat_deg": corr_lat,
                    "gps_corr_lon_deg": corr_lon,
                    "gps_fix_quality": shared.gps.fix_quality,
                    "gps_num_sats": shared.gps.num_sats,
                    "gps_hdop": shared.gps.hdop,
                    "gps_alt_m": shared.gps.alt_m,
                    "gps_sog_knots": shared.gps.sog_knots,
                    "gps_cog_deg": cog,
                    "ping_distance_m": shared.ping.distance_m,
                    "ping_confidence": shared.ping.confidence,
                    "ping_ping_number": shared.ping.ping_number,
                    "gps_stale": int(gps_stale),
                    "ping_stale": int(ping_stale),
                }

            if survey.can_log() and sqlite_logger is not None:
                sqlite_logger.log_row(row)

            time.sleep(period)

    except KeyboardInterrupt:
        print("\n[LOG] Stopped.")
    finally:
        if sqlite_logger is not None:
            sqlite_logger.close()


if __name__ == "__main__":
    main()
