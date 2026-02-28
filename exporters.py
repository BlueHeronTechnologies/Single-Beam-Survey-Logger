import os, json, sqlite3, csv

def fetch_session_meta(conn: sqlite3.Connection, session_id: int) -> dict:
    cur = conn.execute("SELECT id, start_utc, start_local, tz_abbr, utc_offset, notes FROM sessions WHERE id=?", (session_id,))
    row = cur.fetchone()
    if not row:
        raise RuntimeError(f"Session {session_id} not found")
    return {
        "session_id": row[0],
        "start_utc": row[1],
        "start_local": row[2],
        "tz_abbr": row[3],
        "utc_offset": row[4],
        "notes": row[5] or "",
    }

def fetch_samples(conn: sqlite3.Connection, session_id: int):
    cur = conn.execute("""
        SELECT
          pc_time_utc_iso,
          gps_nmea_time_utc,
          gps_raw_lat_deg, gps_raw_lon_deg,
          gps_corr_lat_deg, gps_corr_lon_deg,
          gps_fix_quality, gps_num_sats, gps_hdop,
          gps_alt_m, gps_sog_knots, gps_cog_deg,
          ping_distance_m, ping_confidence, ping_ping_number,
          gps_stale, ping_stale,
          line_id
        FROM samples
        WHERE session_id=?
        ORDER BY id ASC
    """, (session_id,))
    cols = [d[0] for d in cur.description]
    for r in cur.fetchall():
        yield dict(zip(cols, r))

def export_csv(sqlite_path: str, session_id: int, out_path: str) -> str:
    conn = sqlite3.connect(sqlite_path)
    meta = fetch_session_meta(conn, session_id)
    rows = list(fetch_samples(conn, session_id))
    with open(out_path, "w", newline="", encoding="utf-8") as f:
        f.write(f"# Session Start (UTC): {meta['start_utc']}\n")
        f.write(f"# Session Start (Local): {meta['start_local']} {meta['tz_abbr']} {meta['utc_offset']}\n")
        if meta["notes"]:
            f.write(f"# Notes: {meta['notes']}\n")
        if rows:
            fieldnames = list(rows[0].keys())
            w = csv.DictWriter(f, fieldnames=fieldnames)
            w.writeheader()
            for row in rows:
                w.writerow(row)
        else:
            f.write("# No samples\n")
    conn.close()
    return out_path

def export_geojson(sqlite_path: str, session_id: int, out_path: str, use_corrected: bool = True) -> str:
    conn = sqlite3.connect(sqlite_path)
    meta = fetch_session_meta(conn, session_id)

    line_coords = {}  # line_id -> coords
    point_features = []

    for row in fetch_samples(conn, session_id):
        lat = row["gps_corr_lat_deg"] if use_corrected else row["gps_raw_lat_deg"]
        lon = row["gps_corr_lon_deg"] if use_corrected else row["gps_raw_lon_deg"]
        if lat is None or lon is None:
            continue
        coord = [float(lon), float(lat)]
        lid = row.get("line_id")
        line_coords.setdefault(lid, []).append(coord)

        props = {
            "pc_time_utc": row["pc_time_utc_iso"],
            "line_id": lid,
            "gps_stale": int(row["gps_stale"] or 0),
            "ping_stale": int(row["ping_stale"] or 0),
            "depth_m": row["ping_distance_m"],
            "confidence": row["ping_confidence"],
            "ping_number": row["ping_ping_number"],
            "fix_quality": row["gps_fix_quality"],
            "num_sats": row["gps_num_sats"],
            "hdop": row["gps_hdop"],
            "alt_m": row["gps_alt_m"],
            "sog_knots": row["gps_sog_knots"],
            "cog_deg": row["gps_cog_deg"],
            "raw_lat": row["gps_raw_lat_deg"],
            "raw_lon": row["gps_raw_lon_deg"],
            "corr_lat": row["gps_corr_lat_deg"],
            "corr_lon": row["gps_corr_lon_deg"],
        }

        point_features.append({
            "type": "Feature",
            "geometry": {"type": "Point", "coordinates": coord},
            "properties": props
        })

    fc = {
        "type": "FeatureCollection",
        "name": f"session_{session_id}",
        "properties": {
            "session_id": session_id,
            "start_utc": meta["start_utc"],
            "start_local": meta["start_local"],
            "tz_abbr": meta["tz_abbr"],
            "utc_offset": meta["utc_offset"],
            "notes": meta["notes"],
            "position_source": "corrected" if use_corrected else "raw",
        },
        "features": []
    }

    for lid, coords in line_coords.items():
        if coords and len(coords) >= 2:
            fc["features"].append({
                "type": "Feature",
                "geometry": {"type": "LineString", "coordinates": coords},
                "properties": {"kind": "track", "line_id": lid}
            })

    fc["features"].extend(point_features)

    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(fc, f)

    conn.close()
    return out_path

def export_geotiff_depth(sqlite_path: str, session_id: int, out_path: str, grid_m: float = 2.0, method: str = "mean", use_corrected: bool = True) -> str:
    import numpy as np
    import rasterio
    from rasterio.transform import from_origin
    from rasterio.crs import CRS
    from pyproj import Transformer

    conn = sqlite3.connect(sqlite_path)

    xs, ys, ds = [], [], []
    for row in fetch_samples(conn, session_id):
        lat = row["gps_corr_lat_deg"] if use_corrected else row["gps_raw_lat_deg"]
        lon = row["gps_corr_lon_deg"] if use_corrected else row["gps_raw_lon_deg"]
        depth = row["ping_distance_m"]
        if lat is None or lon is None or depth is None:
            continue
        if int(row["ping_stale"] or 0) == 1:
            continue
        xs.append(float(lon)); ys.append(float(lat)); ds.append(float(depth))

    if len(ds) < 3:
        conn.close()
        raise RuntimeError("Not enough valid depth points to create a GeoTIFF (need >= 3).")

    transformer = Transformer.from_crs("EPSG:4326", "EPSG:3857", always_xy=True)
    X, Y = transformer.transform(xs, ys)
    X = np.array(X); Y = np.array(Y); D = np.array(ds)

    minx, maxx = float(X.min()), float(X.max())
    miny, maxy = float(Y.min()), float(Y.max())
    pad = grid_m
    minx -= pad; maxx += pad
    miny -= pad; maxy += pad

    width = int(np.ceil((maxx - minx) / grid_m))
    height = int(np.ceil((maxy - miny) / grid_m))

    if method == "mean":
        sum_grid = np.zeros((height, width), dtype=np.float32)
        cnt_grid = np.zeros((height, width), dtype=np.uint32)
    elif method == "min":
        grid = np.full((height, width), np.inf, dtype=np.float32)
    else:
        grid = np.full((height, width), -np.inf, dtype=np.float32)

    col = ((X - minx) / grid_m).astype(int)
    rowi = ((maxy - Y) / grid_m).astype(int)

    valid = (col >= 0) & (col < width) & (rowi >= 0) & (rowi < height)
    col = col[valid]; rowi = rowi[valid]; D = D[valid]

    if method == "mean":
        for r, c, d in zip(rowi, col, D):
            sum_grid[r, c] += d
            cnt_grid[r, c] += 1
        nodata = -9999.0
        out = np.full((height, width), nodata, dtype=np.float32)
        mask = cnt_grid > 0
        out[mask] = sum_grid[mask] / cnt_grid[mask]
    elif method == "min":
        for r, c, d in zip(rowi, col, D):
            if d < grid[r, c]:
                grid[r, c] = d
        nodata = -9999.0
        out = np.where(np.isfinite(grid), grid, nodata).astype(np.float32)
    else:
        for r, c, d in zip(rowi, col, D):
            if d > grid[r, c]:
                grid[r, c] = d
        nodata = -9999.0
        out = np.where(np.isfinite(grid), grid, nodata).astype(np.float32)

    transform = from_origin(minx, maxy, grid_m, grid_m)
    crs = CRS.from_epsg(3857)

    with rasterio.open(
        out_path,
        "w",
        driver="GTiff",
        height=height,
        width=width,
        count=1,
        dtype=np.float32,
        crs=crs,
        transform=transform,
        nodata=nodata,
        compress="DEFLATE",
        tiled=True,
    ) as dst:
        dst.write(out, 1)

    conn.close()
    return out_path
