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
import xml.etree.ElementTree as ET

HERE = os.path.dirname(os.path.abspath(__file__))
KML_PATH = os.path.join(HERE, "doc.kml")
OUT_PATH = os.path.join(HERE, "index.html")

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


def haversine_km(a, b):
    """Distance in km between two [lng, lat] points."""
    r = 6371.0
    lng1, lat1 = math.radians(a[0]), math.radians(a[1])
    lng2, lat2 = math.radians(b[0]), math.radians(b[1])
    dlat = lat2 - lat1
    dlng = lng2 - lng1
    h = math.sin(dlat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlng / 2) ** 2
    return 2 * r * math.asin(math.sqrt(h))


def line_length_km(coords):
    return sum(haversine_km(coords[i], coords[i + 1]) for i in range(len(coords) - 1))


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
<meta name="viewport" content="width=device-width, initial-scale=1" />
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
  #app { display: flex; height: 100vh; overflow: hidden; background: var(--bg); color: var(--text); }

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
  #info-panel .ip-body { padding: 12px 16px 16px; }
  .stat-row { display: flex; justify-content: space-between; gap: 12px; padding: 7px 0;
    border-bottom: 1px dashed rgba(255,255,255,0.08); font-size: 13px; }
  .stat-row:last-child { border-bottom: none; }
  .stat-row .k { color: var(--muted); }
  .stat-row .v { text-align: right; font-variant-numeric: tabular-nums; }
  .ip-note { margin-top: 10px; font-size: 11.5px; color: var(--muted); line-height: 1.5; }

  /* Legend */
  #legend {
    position: absolute; bottom: 16px; left: 16px; z-index: 900;
    background: rgba(16,35,58,0.92); border: 1px solid rgba(255,255,255,0.12);
    border-radius: 10px; padding: 10px 12px; font-size: 12px; color: var(--text);
  }
  #legend .row { display: flex; align-items: center; gap: 8px; margin: 3px 0; }
  #legend .swatch { width: 14px; height: 8px; border-radius: 2px; }

  .leaflet-popup-content-wrapper { background: var(--panel); color: var(--text); }
  .leaflet-popup-tip { background: var(--panel); }
  .leaflet-popup-content { font-size: 13px; }

  /* Mobile */
  #menu-toggle { display: none; }
  @media (max-width: 760px) {
    #sidebar { position: absolute; left: 0; top: 0; bottom: 0; transform: translateX(-100%);
      transition: transform .25s ease; box-shadow: 0 0 40px rgba(0,0,0,.5); }
    #app.nav-open #sidebar { transform: translateX(0); }
    #menu-toggle { display: flex; position: absolute; top: 12px; left: 12px; z-index: 1300;
      background: var(--panel); color: var(--text); border: 1px solid rgba(255,255,255,.15);
      border-radius: 8px; width: 42px; height: 42px; align-items: center; justify-content: center;
      font-size: 20px; cursor: pointer; }
    #info-panel { width: calc(100% - 24px); }
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

    <div id="legend">
      <div class="row"><span class="swatch" style="background:#1565c0"></span> Major River</div>
      <div class="row"><span class="swatch" style="background:#16a34a"></span> Tributary / Waterway</div>
      <div class="row"><span class="swatch" style="background:#fbbf24;border-radius:50%;width:10px;height:10px"></span> Landmark / Dam / Bay</div>
      <div class="row"><span class="swatch" style="background:#ff2d55"></span> Selected</div>
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
function fmtCoord(c) {
  if (!c) return "&mdash;";
  return c[1].toFixed(5) + ", " + c[0].toFixed(5); // lat, lng
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

function buildInfo(props) {
  const rows = [];
  rows.push(["Type", props.category_label]);
  if (props.geom === "line") {
    rows.push(["Approx. length", props.length_km.toFixed(2) + " km"]);
    rows.push(["Path points", props.num_points.toLocaleString()]);
    rows.push(["Start (lat, lng)", fmtCoord(props.start)]);
    rows.push(["End (lat, lng)", fmtCoord(props.end)]);
    rows.push(["Center (lat, lng)", fmtCoord(props.center)]);
  } else {
    rows.push(["Location (lat, lng)", fmtCoord(props.center)]);
  }
  let html = rows.map(r =>
    `<div class="stat-row"><span class="k">${r[0]}</span><span class="v">${r[1]}</span></div>`
  ).join("");

  const gmapC = props.geom === "line" ? props.center : props.center;
  const gmaps = `https://www.google.com/maps/search/?api=1&query=${gmapC[1]},${gmapC[0]}`;
  html += `<div class="ip-note">Part of the Bacoor City waterways network. `
        + `Metadata is derived from the source KMZ geometry.<br><br>`
        + `<a href="${gmaps}" target="_blank" style="color:var(--accent)">Open location in Google Maps &rarr;</a></div>`;
  return html;
}

// ---- Selection ----
function selectFeature(id, fromMap) {
  // Restore the previously selected feature to its normal color
  resetStyle(activeId);

  activeId = id;
  const props = DATA.features[id].properties;
  const layer = layersById[id];

  // Highlight the newly selected feature
  if (props.geom === "line") layer.setStyle(styleFor(props, true)).bringToFront();
  else layer.setStyle(pointStyleFor(true)).bringToFront();

  // Info panel
  const badge = document.getElementById("ip-badge");
  badge.textContent = props.category_label;
  badge.style.background = COLORS[props.category] || COLORS.tributary;
  document.getElementById("ip-title").textContent = props.name;
  document.getElementById("ip-body").innerHTML = buildInfo(props);
  document.getElementById("info-panel").classList.add("open");

  // Sidebar active state
  document.querySelectorAll(".feature-item").forEach(el => el.classList.remove("active"));
  const item = itemsById[id];
  if (item) {
    item.classList.add("active");
    item.scrollIntoView({ block: "nearest", behavior: "smooth" });
  }

  focusFeature(id);

  // On mobile, close nav when selecting from list
  if (!fromMap) document.getElementById("app").classList.remove("nav-open");
}

document.getElementById("ip-close").addEventListener("click", () => {
  document.getElementById("info-panel").classList.remove("open");
  resetStyle(activeId);
  document.querySelectorAll(".feature-item").forEach(el => el.classList.remove("active"));
  activeId = null;
});

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
