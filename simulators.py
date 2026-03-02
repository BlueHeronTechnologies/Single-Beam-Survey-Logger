# simulators.py
import math
import random
import threading
import time
from datetime import datetime, timezone


def utc_iso_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class SimGPS:
    def __init__(self, shared, hz: float = 2.0):
        self.shared = shared
        self.hz = float(hz)
        self._stop = threading.Event()
        self._thread = None
        self.status = "stopped"

    def start(self, lat0=30.2750, lon0=-89.3300):
        self.stop()
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, args=(lat0, lon0), daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()

    def _run(self, lat0, lon0):
        self.status = "simulating"
        period = 1.0 / max(0.5, self.hz)

        # Pattern settings (degrees)
        # 0.002 deg ~ 220 m north/south, depending on latitude
        line_length_deg = 0.006    # about 650 m east/west near 30N
        line_spacing_deg = 0.001   # about 110 m north/south
        num_lines = 10

        # Start on the southwest corner of the pattern
        x_min = -line_length_deg / 2.0
        x_max = +line_length_deg / 2.0
        y_min = -(line_spacing_deg * (num_lines - 1)) / 2.0

        # Motion settings
        speed_knots = 2.5
        deg_per_sec = 0.00006  # tune for how fast you traverse the line

        # State
        line_idx = 0
        direction = 1  # +1 east, -1 west
        x = x_min
        y = y_min

        def utc_iso_now_local():
            return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

        while not self._stop.is_set():
            # Move along the current line
            x += direction * deg_per_sec * period

            # End-of-line handling: clamp, "turn", step to next line, reverse direction
            if direction == 1 and x >= x_max:
                x = x_max
                line_idx += 1
                direction = -1
                y = y_min + line_idx * line_spacing_deg
            elif direction == -1 and x <= x_min:
                x = x_min
                line_idx += 1
                direction = 1
                y = y_min + line_idx * line_spacing_deg

            # Loop back to the first line when done
            if line_idx >= num_lines:
                line_idx = 0
                direction = 1
                x = x_min
                y = y_min

            lat = lat0 + y
            lon = lon0 + x

            # Course over ground (approx)
            cog = 90.0 if direction == 1 else 270.0

            with self.shared.lock:
                self.shared.gps.pc_time_utc_iso = utc_iso_now_local()
                self.shared.gps.nmea_time_utc = self.shared.gps.pc_time_utc_iso
                self.shared.gps.lat_deg = float(lat)
                self.shared.gps.lon_deg = float(lon)
                self.shared.gps.fix_quality = 1
                self.shared.gps.num_sats = 12
                self.shared.gps.hdop = 0.8
                self.shared.gps.alt_m = 5.0
                self.shared.gps.sog_knots = float(speed_knots)
                self.shared.gps.cog_deg = float(cog)
                self.shared.last_gps_update = time.time()

            time.sleep(period)

        self.status = "stopped"


class SimPing:
    def __init__(self, shared, hz: float = 10.0):
        self.shared = shared
        self.hz = float(hz)
        self._stop = threading.Event()
        self._thread = None
        self.status = "stopped"

    def start(self, depth_m=3.0):
        self.stop()
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, args=(depth_m,), daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()

    def _run(self, depth_m):
        self.status = "simulating"
        t0 = time.time()
        period = 1.0 / max(1.0, self.hz)
        ping_number = 0

        while not self._stop.is_set():
            t = time.time() - t0

            # Smooth depth variation plus a little noise
            d = depth_m + 0.8 * math.sin(t / 8.0) + 0.2 * math.sin(t / 2.5)
            d = max(0.2, d + random.uniform(-0.05, 0.05))

            conf = int(max(0, min(100, 85 + 10 * math.sin(t / 6.0) + random.uniform(-5, 5))))
            ping_number += 1

            with self.shared.lock:
                self.shared.ping.distance_m = float(d)
                self.shared.ping.confidence = int(conf)
                self.shared.ping.ping_number = int(ping_number)
                self.shared.last_ping_update = time.time()

            time.sleep(period)

        self.status = "stopped"