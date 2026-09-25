#!/usr/bin/env python3
"""
Build a self-contained interactive HTML map from the BacoorCity_Waterways KML.

- Parses placemarks (LineStrings and Points) from doc.kml
- Computes metadata (type, length, point count, start/end/center coords)
- Emits a single self-contained index.html using Leaflet + satellite imagery
  that behaves like Google Earth: click a tributary -> map flies to it and an
  info panel appears.
"""

import json
import math
import os
import re
import statistics
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET

HERE = os.path.dirname(os.path.abspath(__file__))
KML_PATH = os.path.join(HERE, "doc.kml")
OUT_PATH = os.path.join(HERE, "index.html")
ELEV_CACHE_PATH = os.path.join(HERE, "elevation_cache.json")

# Number of evenly-spaced samples per waterway used to draw the elevation profile.
# Open-Meteo allows up to 100 locations per request, so keep this <= 100.
PROFILE_SAMPLES = 100

KML_NS = "{http://www.opengis.net/kml/2.2}"


def tag(name):
    return f"{KML_NS}{name}"


def parse_coords(text):
    """Parse a KML <coordinates> block into [[lng, lat], ...]."""
    coords = []
    for token in text.replace("\n", " ").split():
        parts = token.split(",")
        if len(parts) >= 2:
            try:
                lng = float(parts[0])
                lat = float(parts[1])
                coords.append([lng, lat])
            except ValueError:
                continue
    return coords


def geodesic_km(a, b):
    """WGS84 ellipsoidal (Vincenty inverse) distance in km between two
    [lng, lat] points. This matches Google Earth's length measurement."""
    A = 6378137.0                 # WGS84 semi-major axis (m)
    f = 1 / 298.257223563         # WGS84 flattening
    B = A * (1 - f)               # semi-minor axis (m)

    L = math.radians(b[0] - a[0])
    U1 = math.atan((1 - f) * math.tan(math.radians(a[1])))
    U2 = math.atan((1 - f) * math.tan(math.radians(b[1])))
    sU1, cU1 = math.sin(U1), math.cos(U1)
    sU2, cU2 = math.sin(U2), math.cos(U2)

    lam = L
    cos2_alpha = 0.0
    sin_sigma = 0.0
    cos_sigma = 0.0
    sigma = 0.0
    cos_2sigma_m = 0.0
    for _ in range(200):
        sl, cl = math.sin(lam), math.cos(lam)
        sin_sigma = math.sqrt((cU2 * sl) ** 2 + (cU1 * sU2 - sU1 * cU2 * cl) ** 2)
        if sin_sigma == 0:
            return 0.0  # coincident points
        cos_sigma = sU1 * sU2 + cU1 * cU2 * cl
        sigma = math.atan2(sin_sigma, cos_sigma)
        sin_alpha = cU1 * cU2 * sl / sin_sigma
        cos2_alpha = 1 - sin_alpha ** 2
        cos_2sigma_m = cos_sigma - 2 * sU1 * sU2 / cos2_alpha if cos2_alpha != 0 else 0.0
        C = f / 16 * cos2_alpha * (4 + f * (4 - 3 * cos2_alpha))
        lam_prev = lam
        lam = L + (1 - C) * f * sin_alpha * (
            sigma + C * sin_sigma * (cos_2sigma_m + C * cos_sigma * (-1 + 2 * cos_2sigma_m ** 2))
        )
        if abs(lam - lam_prev) < 1e-12:
            break

    u2 = cos2_alpha * (A ** 2 - B ** 2) / (B ** 2)
    Aa = 1 + u2 / 16384 * (4096 + u2 * (-768 + u2 * (320 - 175 * u2)))
    Bb = u2 / 1024 * (256 + u2 * (-128 + u2 * (74 - 47 * u2)))
    d_sigma = Bb * sin_sigma * (
        cos_2sigma_m + Bb / 4 * (
            cos_sigma * (-1 + 2 * cos_2sigma_m ** 2)
            - Bb / 6 * cos_2sigma_m * (-3 + 4 * sin_sigma ** 2) * (-3 + 4 * cos_2sigma_m ** 2)
        )
    )
    return (B * Aa * (sigma - d_sigma)) / 1000.0


def line_length_km(coords):
    return sum(geodesic_km(coords[i], coords[i + 1]) for i in range(len(coords) - 1))


# ---------------------------------------------------------------------------
# Elevation profile support
#
# The KMZ stores every point at altitude 0, so (exactly like Google Earth) the
# elevation profile must be derived by sampling terrain elevation along the
# path from a Digital Elevation Model. We use the free Open-Meteo elevation API
# (Copernicus GLO-90 DEM), sampling evenly-spaced points and caching results so
# repeated builds do not re-hit the network.
# ---------------------------------------------------------------------------

def _load_elev_cache():
    try:
        with open(ELEV_CACHE_PATH, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:
        return {}


def _save_elev_cache(cache):
    try:
        with open(ELEV_CACHE_PATH, "w", encoding="utf-8") as fh:
            json.dump(cache, fh)
    except Exception:
        pass


def resample_line(coords, n):
    """Return n points [lng, lat] evenly spaced by distance along the polyline,
    together with each point's cumulative distance in km."""
    if len(coords) < 2:
        return list(coords), [0.0] * len(coords)

    # cumulative distance at each original vertex
    cum = [0.0]
    for i in range(1, len(coords)):
        cum.append(cum[-1] + geodesic_km(coords[i - 1], coords[i]))
    total = cum[-1]
    if total == 0:
        return [coords[0]] * n, [0.0] * n

    samples = []
    dists = []
    seg = 0
    for i in range(n):
        target = total * i / (n - 1)
        while seg < len(cum) - 2 and cum[seg + 1] < target:
            seg += 1
        seg_len = cum[seg + 1] - cum[seg]
        t = 0.0 if seg_len == 0 else (target - cum[seg]) / seg_len
        a, b = coords[seg], coords[seg + 1]
        lng = a[0] + (b[0] - a[0]) * t
        lat = a[1] + (b[1] - a[1]) * t
        samples.append([lng, lat])
        dists.append(target)
    return samples, dists


def fetch_elevations(points, cache):
    """Return a list of elevations (m) for [lng, lat] points, using cache and
    the Open-Meteo elevation API. Returns None on failure."""
    result = [None] * len(points)
    missing = []
    missing_idx = []
    for i, (lng, lat) in enumerate(points):
        key = f"{round(lat, 5)},{round(lng, 5)}"
        if key in cache:
            result[i] = cache[key]
        else:
            missing.append((lng, lat))
            missing_idx.append(i)

    if missing:
        lats = ",".join(f"{lat:.5f}" for (lng, lat) in missing)
        lngs = ",".join(f"{lng:.5f}" for (lng, lat) in missing)
        url = ("https://api.open-meteo.com/v1/elevation?latitude="
               + urllib.parse.quote(lats) + "&longitude=" + urllib.parse.quote(lngs))

        elevs = None
        for attempt in range(6):
            try:
                with urllib.request.urlopen(url, timeout=30) as resp:
                    data = json.loads(resp.read().decode("utf-8"))
                elevs = data.get("elevation")
                break
            except urllib.error.HTTPError as exc:
                if exc.code == 429:                 # rate limited -> back off and retry
                    wait = 20 * (attempt + 1)
                    print(f"  ... rate limited (429), waiting {wait}s and retrying")
                    time.sleep(wait)
                    continue
                print("  ! elevation fetch failed:", exc)
                return None
            except Exception as exc:
                print("  ! elevation fetch failed:", exc)
                return None

        if not elevs or len(elevs) != len(missing):
            return None
        for j, idx in enumerate(missing_idx):
            lng, lat = missing[j]
            key = f"{round(lat, 5)},{round(lng, 5)}"
            cache[key] = elevs[j]
            result[idx] = elevs[j]
        _save_elev_cache(cache)   # persist progress incrementally
        time.sleep(1.0)           # be polite to the free API

    if any(v is None for v in result):
        return None
    return result


def build_profile(coords, cache):
    """Build an elevation profile for a line: sampled distances (km) and
    elevations (m). Returns a dict or None."""
    n = min(PROFILE_SAMPLES, max(2, len(coords)))
    samples, dists = resample_line(coords, n)
    elevs = fetch_elevations(samples, cache)
    if elevs is None:
        return None
    elevs_r = [round(e, 1) for e in elevs]
    return {
        "d": [round(d, 3) for d in dists],
        "e": elevs_r,
        "min": round(min(elevs_r), 1),
        "max": round(max(elevs_r), 1),
        "median": round(statistics.median(elevs_r), 1),
    }


def classify(name, geom_type):
    """Return (category, category_label) for grouping/coloring."""
    n = (name or "").lower()
    if geom_type == "Point":
        if "dam" in n:
            return "landmark", "Dam"
        if "bay" in n:
            return "landmark", "Bay"
        if "park" in n or "eco" in n:
            return "landmark", "Park"
        return "landmark", "Landmark"
    # Line features
    if "tributary" in n or "untitled" in n or n == "tributary":
        return "tributary", "Tributary"
    if "river" in n:
        return "river", "River"
    return "tributary", "Waterway"


def get_view_hint(placemark):
    """Extract a LookAt/Camera center + range if available."""
    for holder in ("LookAt", "Camera"):
        el = placemark.find(tag(holder))
        if el is not None:
            lon = el.find(tag("longitude"))
            lat = el.find(tag("latitude"))
            rng = el.find(tag("range"))
            if lon is not None and lat is not None:
                return {
                    "lng": float(lon.text),
                    "lat": float(lat.text),
                    "range": float(rng.text) if rng is not None and rng.text else None,
                }
    return None


def main():
    tree = ET.parse(KML_PATH)
    root = tree.getroot()

    features = []
    unnamed_counter = 0
    elev_cache = _load_elev_cache()

    for pm in root.iter(tag("Placemark")):
        name_el = pm.find(tag("name"))
        name = name_el.text.strip() if name_el is not None and name_el.text else ""

        line = pm.find(tag("LineString"))
        point = pm.find(tag("Point"))

        if line is not None:
            coord_el = line.find(tag("coordinates"))
            if coord_el is None or not coord_el.text:
                continue
            coords = parse_coords(coord_el.text)
            if len(coords) < 2:
                continue
            geom_type = "LineString"
            display_name = name
            if not display_name or display_name.lower() == "untitled path":
                unnamed_counter += 1
                display_name = f"Unnamed Tributary {unnamed_counter}"
            category, cat_label = classify(name if name else display_name, "LineString")
            length = line_length_km(coords)
            lats = [c[1] for c in coords]
            lngs = [c[0] for c in coords]
            center = [sum(lngs) / len(lngs), sum(lats) / len(lats)]
            props = {
                "name": display_name,
                "raw_name": name,
                "category": category,
                "category_label": cat_label,
                "geom": "line",
                "length_km": round(length, 3),
                "num_points": len(coords),
                "start": coords[0],
                "end": coords[-1],
                "center": center,
                "bbox": [min(lngs), min(lats), max(lngs), max(lats)],
                "view": get_view_hint(pm),
            }
            print(f"  elevation profile: {display_name} ...")
            profile = build_profile(coords, elev_cache)
            if profile is not None:
                props["profile"] = profile
            features.append({
                "type": "Feature",
                "properties": props,
                "geometry": {"type": "LineString", "coordinates": coords},
            })
        elif point is not None:
            coord_el = point.find(tag("coordinates"))
            if coord_el is None or not coord_el.text:
                continue
            coords = parse_coords(coord_el.text)
            if not coords:
                continue
            c = coords[0]
            display_name = name if name else "Unnamed Point"
            category, cat_label = classify(name, "Point")
            props = {
                "name": display_name,
                "raw_name": name,
                "category": category,
                "category_label": cat_label,
                "geom": "point",
                "center": c,
                "view": get_view_hint(pm),
            }
            features.append({
                "type": "Feature",
                "properties": props,
                "geometry": {"type": "Point", "coordinates": c},
            })

    _save_elev_cache(elev_cache)

    geojson = {"type": "FeatureCollection", "features": features}

    # Assign stable ids
    for i, f in enumerate(geojson["features"]):
        f["properties"]["id"] = i

    print(f"Parsed {len(features)} features")
    cats = {}
    for f in features:
        cats[f["properties"]["category"]] = cats.get(f["properties"]["category"], 0) + 1
    print("By category:", cats)

    html = HTML_TEMPLATE.replace("__GEOJSON__", json.dumps(geojson, separators=(",", ":")))
    with open(OUT_PATH, "w", encoding="utf-8") as fh:
        fh.write(html)
    print(f"Wrote {OUT_PATH} ({os.path.getsize(OUT_PATH)} bytes)")


HTML_TEMPLATE = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8" />
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover" />
<title>Bacoor City Waterways &mdash; Interactive Map</title>
<link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css"
      integrity="sha256-p4NxAoJBhIIN+hmNHrzRCf9tD/miZyoHS5obTRR9BMY=" crossorigin="" />
<style>
  :root {
    --bg: #0d1b2a;
    --panel: #10233a;
    --panel-2: #16324f;
    --accent: #38bdf8;
    --accent-2: #22d3ee;
    --river: #1565c0;
    --tributary: #16a34a;
    --landmark: #fbbf24;
    --text: #e6f0f7;
    --muted: #9db4c7;
  }
  * { box-sizing: border-box; }
  html, body { height: 100%; margin: 0; font-family: "Segoe UI", Roboto, Helvetica, Arial, sans-serif; }
  #app { display: flex; height: 100vh; height: 100dvh; overflow: hidden; background: var(--bg); color: var(--text); }

  /* Sidebar */
  #sidebar {
    width: 340px; min-width: 340px; height: 100%;
    background: var(--panel); display: flex; flex-direction: column;
    border-right: 1px solid rgba(255,255,255,0.08); z-index: 1200;
  }
  #sidebar header { padding: 16px 18px 12px; border-bottom: 1px solid rgba(255,255,255,0.08); }
  #sidebar header h1 { margin: 0; font-size: 18px; letter-spacing: .3px; }
  #sidebar header p { margin: 4px 0 0; font-size: 12px; color: var(--muted); }
  .search-wrap { padding: 12px 14px; }
  #search {
    width: 100%; padding: 9px 12px; border-radius: 8px; border: 1px solid rgba(255,255,255,0.12);
    background: var(--panel-2); color: var(--text); font-size: 13px; outline: none;
  }
  #search::placeholder { color: var(--muted); }
  .list-scroll { flex: 1; overflow-y: auto; padding: 4px 8px 16px; }
  .group-title {
    font-size: 11px; text-transform: uppercase; letter-spacing: .8px; color: var(--muted);
    padding: 12px 10px 6px; display: flex; align-items: center; gap: 8px;
  }
  .group-title .count { margin-left: auto; background: rgba(255,255,255,0.08); border-radius: 10px; padding: 1px 8px; font-size: 11px; }
  .feature-item {
    display: flex; align-items: center; gap: 10px; padding: 9px 10px; border-radius: 8px;
    cursor: pointer; font-size: 13.5px; transition: background .12s; user-select: none;
  }
  .feature-item:hover { background: var(--panel-2); }
  .feature-item.active { background: var(--panel-2); box-shadow: inset 3px 0 0 var(--accent); }
  .swatch { width: 12px; height: 12px; border-radius: 3px; flex: 0 0 auto; }
  .feature-item .fname { flex: 1; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .feature-item .fmeta { font-size: 11px; color: var(--muted); }

  /* Map */
  #map-wrap { flex: 1; position: relative; }
  #map { position: absolute; inset: 0; background: #0b1622; }

  /* Info panel */
  #info-panel {
    position: absolute; top: 16px; right: 16px; width: 320px; max-width: calc(100% - 32px);
    background: rgba(16,35,58,0.96); backdrop-filter: blur(6px);
    border: 1px solid rgba(255,255,255,0.12); border-radius: 12px; color: var(--text);
    box-shadow: 0 12px 40px rgba(0,0,0,0.45); z-index: 1000; overflow: hidden;
    transform: translateX(calc(100% + 24px)); transition: transform .28s ease; }
  #info-panel.open { transform: translateX(0); }
  #info-panel .ip-head { padding: 14px 16px; display: flex; align-items: flex-start; gap: 10px;
    border-bottom: 1px solid rgba(255,255,255,0.1); }
  #info-panel .ip-badge { font-size: 10.5px; text-transform: uppercase; letter-spacing: .6px;
    padding: 3px 8px; border-radius: 20px; color: #06202f; font-weight: 700; }
  #info-panel h2 { margin: 0; font-size: 17px; line-height: 1.25; }
  #info-panel .ip-close { margin-left: auto; cursor: pointer; color: var(--muted); background: none;
    border: none; font-size: 20px; line-height: 1; padding: 0 2px; }
  #info-panel .ip-close:hover { color: var(--text); }
  #info-panel .ip-body { padding: 0; max-height: 70vh; overflow-y: auto; }
  /* Google-Earth-style sections */
  .ip-section { padding: 14px 16px; border-bottom: 1px solid rgba(255,255,255,0.08); }
  .ip-section:last-child { border-bottom: none; }
  .ip-section .sec-label { font-size: 13px; font-weight: 600; color: var(--text); margin-bottom: 6px; }
  .ip-section .sec-value { font-size: 20px; font-weight: 600; color: #fff; font-variant-numeric: tabular-nums; }
  .coord-grid { display: grid; grid-template-columns: auto 1fr; gap: 4px 14px; font-size: 13.5px; }
  .coord-grid .ck { color: var(--muted); }
  .coord-grid .cv { text-align: right; font-variant-numeric: tabular-nums; color: var(--text); }
  .coord-sub { font-size: 11.5px; color: var(--muted); text-transform: uppercase; letter-spacing: .5px;
    margin: 10px 0 4px; }
  .coord-sub:first-child { margin-top: 0; }
  .ip-note { font-size: 11.5px; color: var(--muted); line-height: 1.5; }
  .profile-wrap { background: rgba(255,255,255,0.04); border: 1px solid rgba(255,255,255,0.08);
    border-radius: 8px; padding: 8px 6px 4px; }
  .profile-wrap svg { display: block; width: 100%; height: auto; }
  .profile-stats { display: flex; gap: 14px; margin-top: 8px; font-size: 11.5px; color: var(--muted); }
  .profile-stats b { color: var(--text); font-weight: 600; }
  .profile-axis { fill: var(--muted); font-size: 9px; }
  .profile-grid { stroke: rgba(255,255,255,0.10); stroke-width: 1; }
  .ip-actions a { display: inline-block; color: var(--accent); font-size: 13px; text-decoration: none;
    margin-top: 4px; }
  .ip-actions a:hover { text-decoration: underline; }

  /* Mobile: reopen details after closing the bottom sheet */
  #detail-reopen { display: none; }

  /* Legend */
  #legend {
    position: absolute; bottom: 16px; left: 16px; z-index: 900;
    background: rgba(16,35,58,0.92); border: 1px solid rgba(255,255,255,0.12);
    border-radius: 10px; padding: 10px 12px; font-size: 12px; color: var(--text);
  }
  #legend .legend-title { display: none; font-weight: 600; }
  #legend .legend-rows { display: block; }
  #legend .row { display: flex; align-items: center; gap: 8px; margin: 3px 0; }
  #legend .swatch { width: 14px; height: 8px; border-radius: 2px; flex: 0 0 auto; }
  #legend-toggle { display: none; }

  .leaflet-popup-content-wrapper { background: var(--panel); color: var(--text); }
  .leaflet-popup-tip { background: var(--panel); }
  .leaflet-popup-content { font-size: 13px; }

  /* Mobile */
  #menu-toggle { display: none; }
  @media (max-width: 760px) {
    #sidebar { position: absolute; left: 0; top: 0; bottom: 0; transform: translateX(-100%);
      transition: transform .25s ease; box-shadow: 0 0 40px rgba(0,0,0,.5); }
    #app.nav-open #sidebar { transform: translateX(0); }
    /* Title must sit to the right of the fixed hamburger (12px + 42px button + gap) */
    #sidebar header {
      padding-top: calc(12px + env(safe-area-inset-top));
      padding-left: calc(12px + 42px + 14px);
      padding-right: 18px;
    }
    #menu-toggle { display: flex; position: absolute;
      top: calc(12px + env(safe-area-inset-top)); left: 12px; z-index: 1300;
      background: var(--panel); color: var(--text); border: 1px solid rgba(255,255,255,.15);
      border-radius: 8px; width: 42px; height: 42px; align-items: center; justify-content: center;
      font-size: 20px; cursor: pointer; }

    /* Push Leaflet's top-left zoom controls below the hamburger so they don't overlap */
    .leaflet-top.leaflet-left { margin-top: calc(58px + env(safe-area-inset-top)); }
    .leaflet-top.leaflet-left .leaflet-control { margin-left: 12px; }
    .leaflet-top.leaflet-right { margin-top: env(safe-area-inset-top); }

    /* Bottom sheet: map stays visible above; panel scrolls inside */
    #info-panel {
      left: 0; right: 0; top: auto; bottom: 0; width: 100%; max-width: 100%;
      border-radius: 16px 16px 0 0;
      transform: translateY(110%);
      max-height: min(48dvh, 380px);
      display: flex; flex-direction: column;
      padding-bottom: env(safe-area-inset-bottom);
    }
    #info-panel.open { transform: translateY(0); }
    #info-panel .ip-head { flex: 0 0 auto; }
    #info-panel .ip-body {
      flex: 1 1 auto; min-height: 0; max-height: none;
      overflow-y: auto; -webkit-overflow-scrolling: touch;
    }
    /* Keep legend above the bottom sheet when panel is open */
    #app.panel-open #legend {
      bottom: calc(min(48dvh, 380px) + 12px + env(safe-area-inset-bottom));
    }

    /* Collapsible, always-visible legend that stays above the browser chrome */
    #legend {
      left: 12px; bottom: calc(12px + env(safe-area-inset-bottom));
      padding: 0; font-size: 11.5px; max-width: calc(100% - 24px);
    }
    #legend .legend-title {
      display: flex; align-items: center; gap: 8px; cursor: pointer;
      padding: 9px 12px; user-select: none;
    }
    #legend .legend-title::before { content: "\25BC"; font-size: 9px; color: var(--muted); }
    #legend.collapsed .legend-title::before { content: "\25B6"; }
    #legend .legend-rows { padding: 0 12px 10px; }
    #legend.collapsed .legend-rows { display: none; }

    #detail-reopen {
      display: none; align-items: center; justify-content: center; gap: 8px;
      position: absolute; left: 12px; right: 12px; z-index: 950;
      bottom: calc(12px + env(safe-area-inset-bottom));
      padding: 11px 14px; border-radius: 10px; cursor: pointer;
      background: rgba(16,35,58,0.96); border: 1px solid rgba(255,255,255,0.15);
      color: var(--text); font-size: 13.5px; font-weight: 600;
      box-shadow: 0 8px 28px rgba(0,0,0,0.4);
    }
    #detail-reopen.visible { display: flex; }
    #detail-reopen .chev { color: var(--accent); font-size: 11px; }
    #detail-reopen.visible + #legend {
      bottom: calc(56px + 12px + env(safe-area-inset-bottom));
    }
    #app.panel-open #detail-reopen { display: none !important; }
  }
</style>
</head>
<body>
<div id="app">
  <button id="menu-toggle" title="Toggle list">&#9776;</button>

  <aside id="sidebar">
    <header>
      <h1>Bacoor City Waterways</h1>
      <p>Click a waterway to fly to it and view details</p>
    </header>
    <div class="search-wrap">
      <input id="search" type="text" placeholder="Search rivers, tributaries, landmarks..." />
    </div>
    <div class="list-scroll" id="list"></div>
  </aside>

  <div id="map-wrap">
    <div id="map"></div>

    <div id="info-panel">
      <div class="ip-head">
        <span class="ip-badge" id="ip-badge">River</span>
        <h2 id="ip-title">Feature</h2>
        <button class="ip-close" id="ip-close" title="Close">&times;</button>
      </div>
      <div class="ip-body" id="ip-body"></div>
    </div>

    <button type="button" id="detail-reopen" aria-label="View details for selected waterway">
      <span id="detail-reopen-label">View details</span><span class="chev">&#9650;</span>
    </button>

    <div id="legend">
      <div class="legend-title" id="legend-title">Legend</div>
      <div class="legend-rows">
        <div class="row"><span class="swatch" style="background:#1565c0"></span> Major River</div>
        <div class="row"><span class="swatch" style="background:#16a34a"></span> Tributary / Waterway</div>
        <div class="row"><span class="swatch" style="background:#fbbf24;border-radius:50%;width:10px;height:10px"></span> Landmark / Dam / Bay</div>
        <div class="row"><span class="swatch" style="background:#ff2d55"></span> Selected</div>
      </div>
    </div>
  </div>
</div>

<script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"
        integrity="sha256-20nQCchB9co0qIjJZRGuk2/Z9VM+kNiyxNV1lvTlZBo=" crossorigin=""></script>
<script>
const DATA = __GEOJSON__;

const COLORS = { river: "#1565c0", tributary: "#16a34a", landmark: "#fbbf24" };
const WEIGHTS = { river: 5, tributary: 3 };
const SELECTED_COLOR = "#ff2d55";        // vivid red-pink for the selected waterway
const SELECTED_POINT_FILL = "#ff2d55";   // selected landmark marker fill

// ---- Map setup ----
const map = L.map("map", { zoomControl: true, preferCanvas: true }).setView([14.44, 120.97], 13);

const satellite = L.tileLayer(
  "https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}",
  { maxZoom: 19, attribution: "Imagery &copy; Esri, Maxar, Earthstar Geographics" }
);
const labels = L.tileLayer(
  "https://server.arcgisonline.com/ArcGIS/rest/services/Reference/World_Boundaries_and_Places/MapServer/tile/{z}/{y}/{x}",
  { maxZoom: 19, opacity: 0.9 }
);
const streets = L.tileLayer("https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png",
  { maxZoom: 19, attribution: "&copy; OpenStreetMap contributors" });

const satGroup = L.layerGroup([satellite, labels]).addTo(map);
L.control.layers({ "Satellite": satGroup, "Street map": streets }, {}, { position: "topright" }).addTo(map);

// ---- Feature rendering ----
const layersById = {};
const itemsById = {};
let activeId = null;

function styleFor(props, highlight) {
  const baseColor = COLORS[props.category] || COLORS.tributary;
  return {
    color: highlight ? SELECTED_COLOR : baseColor,
    weight: (WEIGHTS[props.category] || 3) + (highlight ? 4 : 0),
    opacity: 1,
  };
}

function pointStyleFor(highlight) {
  return highlight
    ? { radius: 10, color: "#7a0022", weight: 3, fillColor: SELECTED_POINT_FILL, fillOpacity: 1 }
    : { radius: 7, color: "#7c5a00", weight: 2, fillColor: COLORS.landmark, fillOpacity: 0.95 };
}

function resetStyle(id) {
  if (id === null || id === undefined) return;
  const props = DATA.features[id].properties;
  const layer = layersById[id];
  if (!layer) return;
  if (props.geom === "line") layer.setStyle(styleFor(props, false));
  else layer.setStyle(pointStyleFor(false));
}

const geoLayer = L.geoJSON(DATA, {
  style: (f) => styleFor(f.properties, false),
  pointToLayer: (f, latlng) => L.circleMarker(latlng, pointStyleFor(false)),
  onEachFeature: (f, layer) => {
    const id = f.properties.id;
    layersById[id] = layer;
    layer.on("click", () => selectFeature(id, true));
    layer.bindTooltip(f.properties.name, { sticky: true, direction: "top" });
  },
}).addTo(map);

// Fit to all data initially
try { map.fitBounds(geoLayer.getBounds().pad(0.05)); } catch (e) {}

// ---- Helpers ----
function fmtLat(c) {
  if (!c) return "&mdash;";
  const v = c[1];
  return Math.abs(v).toFixed(6) + "\u00b0 " + (v >= 0 ? "N" : "S");
}
function fmtLng(c) {
  if (!c) return "&mdash;";
  const v = c[0];
  return Math.abs(v).toFixed(6) + "\u00b0 " + (v >= 0 ? "E" : "W");
}
function coordPair(label, c) {
  if (label) {
    return `<div class="coord-sub">${label}</div>`
      + `<div class="coord-grid"><span class="ck">Latitude</span><span class="cv">${fmtLat(c)}</span>`
      + `<span class="ck">Longitude</span><span class="cv">${fmtLng(c)}</span></div>`;
  }
  return `<div class="coord-grid"><span class="ck">Latitude</span><span class="cv">${fmtLat(c)}</span>`
    + `<span class="ck">Longitude</span><span class="cv">${fmtLng(c)}</span></div>`;
}

function boundsOf(id) {
  const layer = layersById[id];
  if (layer.getBounds) return layer.getBounds();
  const ll = layer.getLatLng();
  return L.latLngBounds(ll, ll);
}

function focusFeature(id) {
  const props = DATA.features[id].properties;
  const layer = layersById[id];
  if (props.geom === "line") {
    map.flyToBounds(layer.getBounds().pad(0.25), { duration: 0.9, maxZoom: 17 });
  } else {
    map.flyTo(layer.getLatLng(), 16, { duration: 0.9 });
  }
}

function buildProfileSVG(profile) {
  const d = profile.d, e = profile.e;
  const W = 300, H = 150, padL = 40, padR = 8, padT = 10, padB = 22;
  const plotW = W - padL - padR, plotH = H - padT - padB;
  const maxD = d[d.length - 1] || 1;
  let eMin = Math.min(...e), eMax = Math.max(...e);
  if (eMax - eMin < 1) { eMax = eMin + 1; }        // avoid flat/zero range
  const pad = (eMax - eMin) * 0.12;
  const yLo = eMin - pad, yHi = eMax + pad;

  const X = (v) => padL + (v / maxD) * plotW;
  const Y = (v) => padT + (1 - (v - yLo) / (yHi - yLo)) * plotH;

  let line = "", area = "";
  for (let i = 0; i < d.length; i++) {
    const x = X(d[i]).toFixed(1), y = Y(e[i]).toFixed(1);
    line += (i === 0 ? "M" : "L") + x + " " + y + " ";
  }
  area = line + "L" + X(maxD).toFixed(1) + " " + (padT + plotH).toFixed(1)
       + " L" + padL.toFixed(1) + " " + (padT + plotH).toFixed(1) + " Z";

  // gridlines + labels (elevation: lo/mid/hi ; distance: 0/mid/max)
  const yTicks = [yLo + pad, (yLo + yHi) / 2, yHi - pad];
  let grid = "";
  yTicks.forEach(t => {
    const y = Y(t).toFixed(1);
    grid += `<line class="profile-grid" x1="${padL}" y1="${y}" x2="${W - padR}" y2="${y}"/>`;
    grid += `<text class="profile-axis" x="${padL - 5}" y="${(+y + 3).toFixed(1)}" text-anchor="end">${t.toFixed(0)} m</text>`;
  });
  const xTicks = [0, maxD / 2, maxD];
  xTicks.forEach((t, i) => {
    const x = X(t).toFixed(1);
    const anchor = i === 0 ? "start" : (i === 2 ? "end" : "middle");
    grid += `<text class="profile-axis" x="${x}" y="${H - 6}" text-anchor="${anchor}">${t.toFixed(1)} km</text>`;
  });

  return `<svg viewBox="0 0 ${W} ${H}" preserveAspectRatio="xMidYMid meet" role="img" aria-label="Elevation profile">`
       + `<defs><linearGradient id="elevFill" x1="0" y1="0" x2="0" y2="1">`
       + `<stop offset="0%" stop-color="#2b8fe0" stop-opacity="0.55"/>`
       + `<stop offset="100%" stop-color="#2b8fe0" stop-opacity="0.05"/></linearGradient></defs>`
       + grid
       + `<path d="${area}" fill="url(#elevFill)" stroke="none"/>`
       + `<path d="${line}" fill="none" stroke="#3aa0ec" stroke-width="1.6" stroke-linejoin="round"/>`
       + `</svg>`;
}

function buildInfo(props) {
  let html = "";

  if (props.geom === "line") {
    // Length section (accurate WGS84 geodesic length, matches Google Earth)
    html += `<div class="ip-section">`
          + `<div class="sec-label">Length</div>`
          + `<div class="sec-value">${props.length_km.toFixed(2)} km</div>`
          + `</div>`;

    // Elevation profile (terrain sampled along the path, like Google Earth)
    if (props.profile && props.profile.e && props.profile.e.length > 1) {
      const p = props.profile;
      html += `<div class="ip-section">`
            + `<div class="sec-label">Elevation profile</div>`
            + `<div class="profile-wrap">${buildProfileSVG(p)}</div>`
            + `<div class="profile-stats">`
            + `<span>Min <b>${p.min.toFixed(1)} m</b></span>`
            + `<span>Median <b>${p.median.toFixed(1)} m</b></span>`
            + `<span>Max <b>${p.max.toFixed(1)} m</b></span>`
            + `</div></div>`;
    }
    // Coordinates section: start / end / midpoint
    html += `<div class="ip-section">`
          + `<div class="sec-label">Coordinates</div>`
          + coordPair("Start point", props.start)
          + coordPair("End point", props.end)
          + coordPair("Midpoint", props.center)
          + `</div>`;
  } else {
    // Point (dam / bay / landmark): latitude & longitude are the priority
    html += `<div class="ip-section">`
          + `<div class="sec-label">Coordinates</div>`
          + coordPair("", props.center)
          + `</div>`;
  }

  const c = props.center;
  const gmaps = `https://www.google.com/maps/search/?api=1&query=${c[1]},${c[0]}`;
  html += `<div class="ip-section ip-actions">`
        + `<a href="${gmaps}" target="_blank">Open location in Google Maps &rarr;</a>`
        + `<div class="ip-note" style="margin-top:8px">Data derived directly from the source KMZ geometry.</div>`
        + `</div>`;
  return html;
}

// ---- Selection ----
function populateInfoPanel(id) {
  const props = DATA.features[id].properties;
  const badge = document.getElementById("ip-badge");
  badge.textContent = props.category_label;
  badge.style.background = COLORS[props.category] || COLORS.tributary;
  document.getElementById("ip-title").textContent = props.name;
  document.getElementById("ip-body").innerHTML = buildInfo(props);
}

function openInfoPanel() {
  document.getElementById("info-panel").classList.add("open");
  document.getElementById("app").classList.add("panel-open");
  syncDetailReopenButton();
}

function closeInfoPanel() {
  document.getElementById("info-panel").classList.remove("open");
  document.getElementById("app").classList.remove("panel-open");
  syncDetailReopenButton();
}

function syncDetailReopenButton() {
  const btn = document.getElementById("detail-reopen");
  const mobile = window.matchMedia("(max-width: 760px)").matches;
  const panelOpen = document.getElementById("info-panel").classList.contains("open");
  if (!mobile || activeId === null || panelOpen) {
    btn.classList.remove("visible");
    return;
  }
  document.getElementById("detail-reopen-label").textContent = DATA.features[activeId].properties.name;
  btn.classList.add("visible");
}

function highlightFeature(id) {
  const props = DATA.features[id].properties;
  const layer = layersById[id];
  if (props.geom === "line") layer.setStyle(styleFor(props, true)).bringToFront();
  else layer.setStyle(pointStyleFor(true)).bringToFront();
}

function selectFeature(id, fromMap) {
  const panelOpen = document.getElementById("info-panel").classList.contains("open");

  // Same feature, panel closed: reopen without clearing highlight or re-flying the map
  if (activeId === id && !panelOpen) {
    populateInfoPanel(id);
    openInfoPanel();
    if (!fromMap) document.getElementById("app").classList.remove("nav-open");
    return;
  }

  if (activeId !== null && activeId !== id) resetStyle(activeId);

  activeId = id;
  highlightFeature(id);
  populateInfoPanel(id);
  openInfoPanel();

  document.querySelectorAll(".feature-item").forEach(el => el.classList.remove("active"));
  const item = itemsById[id];
  if (item) {
    item.classList.add("active");
    item.scrollIntoView({ block: "nearest", behavior: "smooth" });
  }

  focusFeature(id);

  if (!fromMap) document.getElementById("app").classList.remove("nav-open");
}

document.getElementById("ip-close").addEventListener("click", closeInfoPanel);
document.getElementById("detail-reopen").addEventListener("click", () => {
  if (activeId !== null) openInfoPanel();
});
window.addEventListener("resize", syncDetailReopenButton);

// ---- Sidebar list ----
const GROUPS = [
  { key: "river", label: "Major Rivers", color: COLORS.river },
  { key: "tributary", label: "Tributaries & Waterways", color: COLORS.tributary },
  { key: "landmark", label: "Landmarks, Dams & Bays", color: COLORS.landmark },
];

function buildList(filter) {
  const list = document.getElementById("list");
  list.innerHTML = "";
  const f = (filter || "").trim().toLowerCase();

  GROUPS.forEach(group => {
    const feats = DATA.features
      .map((ft) => ft.properties)
      .filter(p => p.category === group.key)
      .filter(p => !f || p.name.toLowerCase().includes(f))
      .sort((a, b) => a.name.localeCompare(b.name));

    if (!feats.length) return;

    const title = document.createElement("div");
    title.className = "group-title";
    title.innerHTML = `<span class="swatch" style="background:${group.color}"></span>${group.label}`
      + `<span class="count">${feats.length}</span>`;
    list.appendChild(title);

    feats.forEach(p => {
      const item = document.createElement("div");
      item.className = "feature-item";
      const meta = p.geom === "line" ? `${p.length_km.toFixed(1)} km` : p.category_label;
      const shape = p.geom === "line"
        ? `<span class="swatch" style="background:${group.color}"></span>`
        : `<span class="swatch" style="background:${group.color};border-radius:50%"></span>`;
      item.innerHTML = `${shape}<span class="fname">${p.name}</span><span class="fmeta">${meta}</span>`;
      item.addEventListener("click", () => selectFeature(p.id, false));
      itemsById[p.id] = item;
      list.appendChild(item);
    });
  });

  if (!list.children.length) {
    list.innerHTML = `<div style="padding:20px;color:var(--muted);font-size:13px">No matches found.</div>`;
  }
}

buildList("");

document.getElementById("search").addEventListener("input", (e) => buildList(e.target.value));
document.getElementById("menu-toggle").addEventListener("click", () =>
  document.getElementById("app").classList.toggle("nav-open"));

// Collapsible legend (mobile). Start collapsed on small screens to save space.
const legend = document.getElementById("legend");
if (window.matchMedia("(max-width: 760px)").matches) legend.classList.add("collapsed");
document.getElementById("legend-title").addEventListener("click", () =>
  legend.classList.toggle("collapsed"));

// Summary in header subtitle
const counts = DATA.features.reduce((a, ft) => (a[ft.properties.category] = (a[ft.properties.category]||0)+1, a), {});
document.querySelector("#sidebar header p").textContent =
  `${counts.river||0} rivers \u00b7 ${counts.tributary||0} tributaries \u00b7 ${counts.landmark||0} landmarks \u2014 click to explore`;
</script>
</body>
</html>
"""


if __name__ == "__main__":
    main()
