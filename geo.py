import math

def apply_antenna_offset(lat_deg, lon_deg, heading_deg, ant_forward_m, ant_right_m):
    """
    Shift lat/lon from antenna position to reference point using heading.

    heading_deg: 0=N, 90=E (COG / heading)
    ant_forward_m: antenna forward of reference (+bow)
    ant_right_m: antenna right of reference (+starboard)

    Correction moves from antenna -> reference => (-forward, -right) in vessel frame.
    Returns (lat, lon) corrected.
    """
    if lat_deg is None or lon_deg is None or heading_deg is None:
        return (lat_deg, lon_deg)

    fwd = -float(ant_forward_m)
    rgt = -float(ant_right_m)

    hdg = math.radians(float(heading_deg))
    dE = fwd * math.sin(hdg) + rgt * math.cos(hdg)
    dN = fwd * math.cos(hdg) - rgt * math.sin(hdg)

    try:
        from pyproj import Geod
        g = Geod(ellps="WGS84")
        az = math.degrees(math.atan2(dE, dN))
        dist = math.hypot(dE, dN)
        lon2, lat2, _ = g.fwd(lon_deg, lat_deg, az, dist)
        return (lat2, lon2)
    except Exception:
        R = 6378137.0
        lat = math.radians(lat_deg)
        dLat = dN / R
        dLon = dE / (R * math.cos(lat) if abs(math.cos(lat)) > 1e-12 else R)
        return (lat_deg + math.degrees(dLat), lon_deg + math.degrees(dLon))
