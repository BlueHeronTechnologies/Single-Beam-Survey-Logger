import sqlite3
from datetime import datetime, timezone

class SQLiteLogger:
    def __init__(self, db_path: str, batch_size: int = 50):
        self.db_path = db_path
        self.batch_size = batch_size
        self._buf = []
        self.conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self.conn.execute("PRAGMA journal_mode=WAL;")
        self.conn.execute("PRAGMA synchronous=NORMAL;")
        self.conn.execute("PRAGMA temp_store=MEMORY;")
        self.conn.execute("PRAGMA foreign_keys=ON;")
        self._init_schema()
        self.session_id = None
        self.line_id = None
        self.line_number = 0

    def _init_schema(self):
        self.conn.execute("""
        CREATE TABLE IF NOT EXISTS sessions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            start_utc TEXT NOT NULL,
            start_local TEXT NOT NULL,
            tz_abbr TEXT NOT NULL,
            utc_offset TEXT NOT NULL,
            gps_port TEXT,
            gps_baud INTEGER,
            ping_port TEXT,
            ping_baud INTEGER,
            log_hz REAL,
            ping_hz REAL,
            notes TEXT
        );
        """)
        self.conn.execute("""
        CREATE TABLE IF NOT EXISTS lines (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id INTEGER NOT NULL,
            line_number INTEGER NOT NULL,
            start_utc TEXT NOT NULL,
            start_local TEXT NOT NULL,
            label TEXT,
            FOREIGN KEY(session_id) REFERENCES sessions(id)
        );
        """)
        self.conn.execute("""
        CREATE TABLE IF NOT EXISTS samples (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id INTEGER NOT NULL,
            line_id INTEGER,

            pc_time_utc_iso TEXT NOT NULL,
            gps_nmea_time_utc TEXT,

            gps_raw_lat_deg REAL,
            gps_raw_lon_deg REAL,
            gps_corr_lat_deg REAL,
            gps_corr_lon_deg REAL,

            gps_fix_quality INTEGER,
            gps_num_sats INTEGER,
            gps_hdop REAL,
            gps_alt_m REAL,
            gps_sog_knots REAL,
            gps_cog_deg REAL,

            ping_distance_m REAL,
            ping_confidence INTEGER,
            ping_ping_number INTEGER,

            gps_stale INTEGER NOT NULL,
            ping_stale INTEGER NOT NULL,

            FOREIGN KEY(session_id) REFERENCES sessions(id),
            FOREIGN KEY(line_id) REFERENCES lines(id)
        );
        """)
        self.conn.execute("CREATE INDEX IF NOT EXISTS idx_samples_session_time ON samples(session_id, pc_time_utc_iso);")
        self.conn.execute("CREATE INDEX IF NOT EXISTS idx_lines_session_num ON lines(session_id, line_number);")
        self.conn.commit()

    def start_session(self, meta: dict) -> int:
        cols = ",".join(meta.keys())
        qmarks = ",".join(["?"] * len(meta))
        cur = self.conn.execute(f"INSERT INTO sessions ({cols}) VALUES ({qmarks})", list(meta.values()))
        self.conn.commit()
        self.session_id = int(cur.lastrowid)
        self.line_id = None
        self.line_number = 0
        return self.session_id

    def start_new_line(self, label: str = "") -> tuple[int, int]:
        if not self.session_id:
            raise RuntimeError("No active session.")
        self.line_number += 1
        now_local = datetime.now().astimezone()
        now_utc = datetime.now(timezone.utc)
        cur = self.conn.execute(
            "INSERT INTO lines (session_id, line_number, start_utc, start_local, label) VALUES (?,?,?,?,?)",
            (
                self.session_id,
                self.line_number,
                now_utc.strftime("%Y-%m-%dT%H:%M:%SZ"),
                now_local.strftime("%Y-%m-%d %H:%M:%S"),
                label or "",
            ),
        )
        self.conn.commit()
        self.line_id = int(cur.lastrowid)
        return self.line_id, self.line_number

    def log_row(self, row: dict):
        if not self.session_id:
            return
        self._buf.append((
            self.session_id,
            self.line_id,
            row.get("pc_time_utc_iso") or "",
            row.get("gps_nmea_time_utc") or "",

            row.get("gps_raw_lat_deg"),
            row.get("gps_raw_lon_deg"),
            row.get("gps_corr_lat_deg"),
            row.get("gps_corr_lon_deg"),

            row.get("gps_fix_quality"),
            row.get("gps_num_sats"),
            row.get("gps_hdop"),
            row.get("gps_alt_m"),
            row.get("gps_sog_knots"),
            row.get("gps_cog_deg"),

            row.get("ping_distance_m"),
            row.get("ping_confidence"),
            row.get("ping_ping_number"),

            int(row.get("gps_stale", 1)),
            int(row.get("ping_stale", 1)),
        ))
        if len(self._buf) >= self.batch_size:
            self.flush()

    def flush(self):
        if not self._buf:
            return
        self.conn.executemany("""
            INSERT INTO samples (
              session_id, line_id,
              pc_time_utc_iso, gps_nmea_time_utc,
              gps_raw_lat_deg, gps_raw_lon_deg,
              gps_corr_lat_deg, gps_corr_lon_deg,
              gps_fix_quality, gps_num_sats, gps_hdop, gps_alt_m,
              gps_sog_knots, gps_cog_deg,
              ping_distance_m, ping_confidence, ping_ping_number,
              gps_stale, ping_stale
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """, self._buf)
        self.conn.commit()
        self._buf.clear()

    def close(self):
        try:
            self.flush()
        finally:
            self.conn.close()
