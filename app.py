"""
RainRoute – Smart Route Intelligence
Run:  streamlit run app.py
Needs: pip install streamlit streamlit-folium folium requests pandas numpy plotly
"""
import html
import math
import os
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone

import folium
import numpy as np
import pandas as pd
import plotly.graph_objects as go
import requests
import streamlit as st
from folium.plugins import Fullscreen, MarkerCluster
from streamlit_folium import st_folium

st.set_page_config(page_title="RainRoute | Smart Route Intelligence", page_icon="🌦️", layout="wide")

# ----------------------------------------------------------------------------
# 🔐 API key is loaded securely from Streamlit Secrets or environment variables.
# Get one in a minute, no credit card: https://myprojects.geoapify.com
# Keep this empty. Add GEOAPIFY_API_KEY through Streamlit Cloud Secrets.
# ----------------------------------------------------------------------------
GEOAPIFY_API_KEY = ""

# ⚙️ Average speed used for "Bike" trips (km/h). Change this number to whatever you like.
BIKE_SPEED_KMH = 60


# ⛽ Fuel estimator
FUEL_DEFAULT_MILEAGE = {"Car": 15.0, "Bike": 40.0}  # km per litre

# Latest verified reference data available: 13 November 2026
FUEL_REF_DATE = "13 Nov 2026"

# Reference retail prices in ₹/litre:
# (city, latitude, longitude, petrol, diesel)
# Prices are estimates for reference; local rates may vary.
FUEL_RATES = [
    ("Agra",       27.1767, 78.0081, 101.67, 95.14),
    ("Delhi",      28.6139, 77.2090, 102.12, 95.20),
    ("Mumbai",     19.0760, 72.8777, 111.21, 97.83),
    ("Bengaluru",  12.9716, 77.5946, 111.68, 99.56),
    ("Chennai",    13.0827, 80.2707, 107.76, 99.55),
    ("Hyderabad",  17.3850, 78.4867, 116.15, 104.23),
    ("Kolkata",    22.5726, 88.3639, 113.51, 99.82),
    ("Patna",      25.5941, 85.1376, 113.37, 99.36),
    ("Bhubaneswar",20.2961, 85.8245, 108.97, 100.68),
]



def _km_between(lat1, lon1, lat2, lon2):
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dl = math.radians(lon2 - lon1)
    h = math.sin((p2 - p1) / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * 6371.0 * math.asin(math.sqrt(h))


def _nearest_fuel_rate(lat, lon, fuel):
    best = None
    for city, la, lo, petrol, diesel in FUEL_RATES:
        price = petrol if fuel == "Petrol" else diesel
        if price is None:
            continue
        d = _km_between(lat, lon, la, lo)
        if best is None or d < best[0]:
            best = (d, city, price)
    return best


def auto_fuel_rate(start, end, fuel):
    """Automatic ₹/L: average of the nearest reference cities to the start and the destination."""
    s_, e_ = _nearest_fuel_rate(*start, fuel), _nearest_fuel_rate(*end, fuel)
    rate = (s_[2] + e_[2]) / 2
    if s_[1] == e_[1]:
        text = f"Automatic rate: {s_[1]} reference price"
    else:
        text = (f"Automatic rate: average of {s_[1]} (₹{s_[2]:.2f}) and {e_[1]} (₹{e_[2]:.2f}) reference prices")
    text += f" · built-in table dated {FUEL_REF_DATE}."
    if max(s_[0], e_[0]) > 500:
        text += " ⚠️ Your route is far from the reference cities – please check the local price and edit it."
    else:
        text += " State taxes differ, so switch the toggle off and edit if you know the local price."
    return rate, text

# ----------------------------------------------------------------------------
# Constants & helpers
# ----------------------------------------------------------------------------
HTTP = requests.Session()
HTTP.headers.update({"User-Agent": "RainRouteStudentProject/2.0 (educational route planner)",
                     "Accept": "application/json"})

ROUTERS = {
    "Car": ["https://routing.openstreetmap.de/routed-car/route/v1/driving/",
            "https://router.project-osrm.org/route/v1/driving/"],
    "Bike": ["https://routing.openstreetmap.de/routed-car/route/v1/driving/",
             "https://router.project-osrm.org/route/v1/driving/"],
    "Walking": ["https://routing.openstreetmap.de/routed-foot/route/v1/driving/"],
}
OVERPASS = [
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
    "https://overpass.private.coffee/api/interpreter",
]
ROUTE_COLORS = ["#2563EB", "#16A34A", "#9333EA", "#F59E0B"]

# name: (emoji, folium colour, legend hex, glyphicon)
CATS = {
    "Restaurants": ("🍽️", "red", "#d33d29", "cutlery"),
    "Cafes": ("☕", "orange", "#f69730", "glass"),
    "Petrol Pumps": ("⛽", "green", "#72b026", "tint"),
    "Hospitals": ("🏥", "blue", "#38aadd", "plus-sign"),
    "Hotels": ("🏨", "purple", "#d252b9", "home"),
    "Tourist Attractions": ("🎡", "cadetblue", "#436978", "camera"),
    "Famous Places": ("🏛️", "darkpurple", "#5b396b", "star"),
    "Pharmacies": ("💊", "lightred", "#ff8e7f", "plus"),
    "ATMs & Banks": ("🏧", "darkgreen", "#728224", "usd"),
    "EV Charging": ("🔌", "lightgreen", "#bbf970", "flash"),
    "Parking": ("🅿️", "gray", "#575757", "th-large"),
    "Rest Areas": ("🛣️", "beige", "#ffcb92", "road"),
    "Toll Plazas": ("🧾", "black", "#303030", "credit-card"),
    "Police Stations": ("👮", "darkblue", "#00669a", "exclamation-sign"),
    "Supermarkets & Shops": ("🛒", "pink", "#ff91ea", "shopping-cart"),
    "Parks & Nature": ("🌳", "lightblue", "#8adaff", "leaf"),
}
DEFAULT_CATS = ["Restaurants", "Cafes", "Petrol Pumps", "Hospitals", "Hotels", "Tourist Attractions",
                "Famous Places", "Pharmacies", "ATMs & Banks"]
FALLBACK_NAME = {"ATMs & Banks": "ATM", "EV Charging": "EV charging station", "Rest Areas": "Rest area",
                 "Toll Plazas": "Toll plaza"}

WMO = {0: ("☀️", "Clear sky"), 1: ("🌤️", "Mostly clear"), 2: ("⛅", "Partly cloudy"), 3: ("☁️", "Overcast"),
       45: ("🌫️", "Fog"), 48: ("🌫️", "Rime fog"), 51: ("🌦️", "Light drizzle"), 53: ("🌦️", "Drizzle"),
       55: ("🌧️", "Dense drizzle"), 56: ("🌧️", "Freezing drizzle"), 57: ("🌧️", "Freezing drizzle"),
       61: ("🌧️", "Light rain"), 63: ("🌧️", "Rain"), 65: ("🌧️", "Heavy rain"), 66: ("🌧️", "Freezing rain"),
       67: ("🌧️", "Freezing rain"), 71: ("🌨️", "Light snow"), 73: ("🌨️", "Snow"), 75: ("❄️", "Heavy snow"),
       77: ("🌨️", "Snow grains"), 80: ("🌦️", "Rain showers"), 81: ("🌧️", "Rain showers"),
       82: ("⛈️", "Violent showers"), 85: ("🌨️", "Snow showers"), 86: ("🌨️", "Snow showers"),
       95: ("⛈️", "Thunderstorm"), 96: ("⛈️", "Thunderstorm, hail"), 99: ("⛈️", "Severe thunderstorm")}


def _ver():
    try:
        return tuple(int(x) for x in st.__version__.split(".")[:2])
    except Exception:
        return (1, 0)


def stretch():
    """Full-width kwargs that work on both old and new Streamlit versions."""
    return {"width": "stretch"} if _ver() >= (1, 50) else {"use_container_width": True}


def race(tasks):
    """Run callables in parallel and return the first successful result."""
    errors = []
    ex = ThreadPoolExecutor(max_workers=len(tasks))
    futures = [ex.submit(t) for t in tasks]
    try:
        for f in as_completed(futures):
            try:
                return f.result()
            except Exception as exc:  # noqa: BLE001
                errors.append(str(exc)[:120])
    finally:
        ex.shutdown(wait=False, cancel_futures=True)
    raise RuntimeError(" | ".join(errors) or "All servers failed")


def fmt_duration(minutes):
    m = int(round(minutes))
    h, mm = divmod(m, 60)
    return f"{h} h {mm:02d} min" if h else f"{mm} min"


def risk(p):
    if p >= 70:
        return "High", "#dc2626", "🔴"
    if p >= 40:
        return "Moderate", "#f59e0b", "🟠"
    return "Low", "#16a34a", "🟢"


# ----------------------------------------------------------------------------
# Geometry (numpy – fast even for thousands of points)
# ----------------------------------------------------------------------------
def cumulative_km(arr):
    lat, lon = np.radians(arr[:, 0]), np.radians(arr[:, 1])
    dlat, dlon = np.diff(lat), np.diff(lon)
    a = np.sin(dlat / 2) ** 2 + np.cos(lat[:-1]) * np.cos(lat[1:]) * np.sin(dlon / 2) ** 2
    d = 2 * 6371.0 * np.arcsin(np.sqrt(np.clip(a, 0, 1)))
    return np.concatenate([[0.0], np.cumsum(d)])


def points_along(coords, n):
    """n evenly spaced points (by distance) along a [(lat, lon), ...] route."""
    arr = np.asarray(coords, dtype=float)
    cum = cumulative_km(arr)
    total = cum[-1] if cum[-1] > 0 else 1e-9
    idx = np.clip(np.searchsorted(cum, np.linspace(0, total, n)), 0, len(arr) - 1)
    return [{"lat": float(arr[i, 0]), "lon": float(arr[i, 1]), "km": float(cum[i]),
             "frac": float(cum[i] / total)} for i in idx]


def distances_to_route_km(points_latlon, route_coords, max_route_pts=500):
    """Shortest distance (km) from every point to the route polyline (vectorised)."""
    pts = np.asarray(points_latlon, dtype=float)
    route = np.asarray(route_coords, dtype=float)
    if len(route) > max_route_pts:
        route = route[np.linspace(0, len(route) - 1, max_route_pts).astype(int)]
    if len(route) < 2:
        route = np.vstack([route, route])
    r = 6371.0
    lat0 = np.radians(route[:, 0].mean())

    def proj(a):
        return np.column_stack([r * np.radians(a[:, 1]) * np.cos(lat0), r * np.radians(a[:, 0])])

    p, q = proj(pts), proj(route)
    a_, b_ = q[:-1], q[1:]
    ab = b_ - a_
    denom = (ab ** 2).sum(axis=1)
    ap = p[:, None, :] - a_[None, :, :]
    t = np.where(denom == 0, 0, np.clip((ap * ab[None]).sum(axis=2) / np.where(denom == 0, 1, denom), 0, 1))
    nearest = a_[None] + t[..., None] * ab[None]
    return np.linalg.norm(p[:, None, :] - nearest, axis=2).min(axis=1)


# ----------------------------------------------------------------------------
# API layer (all cached, all with timeouts / fallbacks)
# ----------------------------------------------------------------------------
@st.cache_data(ttl=86400, show_spinner=False)
def geocode(query):
    """Nominatim first, Open-Meteo geocoder as fallback. Returns None only if truly not found."""
    q = query.strip()
    errors = 0
    try:
        r = HTTP.get("https://nominatim.openstreetmap.org/search",
                     params={"q": q, "format": "jsonv2", "limit": 1}, timeout=8)
        r.raise_for_status()
        res = r.json()
        if res:
            label = ", ".join(res[0].get("display_name", q).split(",")[:2])
            return {"lat": float(res[0]["lat"]), "lon": float(res[0]["lon"]), "name": label}
    except (requests.RequestException, ValueError, KeyError):
        errors += 1
    try:
        r = HTTP.get("https://geocoding-api.open-meteo.com/v1/search",
                     params={"name": q, "count": 1, "language": "en", "format": "json"}, timeout=8)
        r.raise_for_status()
        res = r.json().get("results")
        if res:
            x = res[0]
            label = ", ".join(v for v in (x.get("name"), x.get("admin1")) if v)
            return {"lat": float(x["latitude"]), "lon": float(x["longitude"]), "name": label}
    except (requests.RequestException, ValueError, KeyError):
        errors += 1
    if errors:
        raise RuntimeError("Geocoding services are not reachable right now. Please try again.")
    return None


@st.cache_data(ttl=900, show_spinner=False)
def fetch_routes(start, end, mode):
    """Returns (routes, note). Routes are sorted by duration with [(lat, lon)] geometry."""
    coords = f"{start[1]:.6f},{start[0]:.6f};{end[1]:.6f},{end[0]:.6f}"
    params = {"overview": "full", "geometries": "geojson", "alternatives": "true", "steps": "false"}

    def call(base):
        r = HTTP.get(base + coords, params=params, timeout=(4, 15))
        r.raise_for_status()
        d = r.json()
        if d.get("code") != "Ok" or not d.get("routes"):
            raise RuntimeError(d.get("message") or d.get("code") or "No route")
        return d["routes"]

    note = None
    try:
        raw = race([lambda b=b: call(b) for b in ROUTERS[mode]])
    except RuntimeError:
        if mode in ("Car", "Bike"):
            raise
        raw = race([lambda b=b: call(b) for b in ROUTERS["Car"]])
        note = f"No {mode.lower()} route service responded – showing the driving route instead."

    if mode == "Bike":
        for r in raw:
            r["duration"] = r["distance"] / (BIKE_SPEED_KMH / 3.6)  # metres / (m/s) = seconds
    routes, seen = [], set()
    for r in sorted(raw, key=lambda x: x["duration"]):
        c = [(lat, lon) for lon, lat in r["geometry"]["coordinates"]]
        sig = (round(r["distance"]), round(r["duration"]))
        if sig in seen:
            continue
        seen.add(sig)
        routes.append({"distance_km": r["distance"] / 1000, "duration_min": r["duration"] / 60, "coords": c})
    shortest = min(range(len(routes)), key=lambda i: routes[i]["distance_km"])
    for i, rt in enumerate(routes):
        name = "Fastest" if i == 0 else f"Alternative {i}"
        if len(routes) > 1 and i == shortest:
            name += " & Shortest" if i == 0 else " (shortest)"
        rt["name"], rt["color"] = name, ROUTE_COLORS[i % len(ROUTE_COLORS)]
    return routes, note


@st.cache_data(ttl=900, show_spinner=False)
def fetch_weather(points):
    """One request for all points. points = ((lat, lon), ...). Always returns a list."""
    params = {
        "latitude": ",".join(f"{p[0]:.3f}" for p in points),
        "longitude": ",".join(f"{p[1]:.3f}" for p in points),
        "current": "temperature_2m,relative_humidity_2m,precipitation,wind_speed_10m,weather_code",
        "hourly": "temperature_2m,precipitation_probability,precipitation,weather_code,wind_speed_10m",
        "forecast_days": 3,
        "timezone": "auto",
    }
    last = None
    for _ in range(2):
        try:
            r = HTTP.get("https://api.open-meteo.com/v1/forecast", params=params, timeout=(4, 15))
            r.raise_for_status()
            data = r.json()
            if isinstance(data, dict):
                if data.get("error"):
                    raise RuntimeError(data.get("reason", "Weather API error"))
                data = [data]
            return data
        except (requests.RequestException, ValueError) as exc:
            last = exc
            time.sleep(0.4)
    raise RuntimeError(f"Weather service unavailable: {last}")


CAT_QUERY = {
    "Restaurants": ['["name"]["amenity"~"^(restaurant|fast_food)$"]'],
    "Cafes": ['["name"]["amenity"="cafe"]'],
    "Petrol Pumps": ['["name"]["amenity"="fuel"]'],
    "Hospitals": ['["name"]["amenity"~"^(hospital|clinic)$"]', '["name"]["healthcare"~"^(hospital|clinic)$"]'],
    "Hotels": ['["name"]["tourism"~"^(hotel|guest_house|hostel|motel)$"]'],
    "Tourist Attractions": ['["name"]["tourism"~"^(attraction|museum|viewpoint|theme_park|gallery|zoo|aquarium)$"]'],
    "Famous Places": ['["name"]["historic"]', '["name"]["heritage"]',
                      '["name"]["amenity"="place_of_worship"]["wikidata"]'],
    "Pharmacies": ['["name"]["amenity"="pharmacy"]'],
    "ATMs & Banks": ['["amenity"~"^(atm|bank)$"]'],
    "EV Charging": ['["amenity"="charging_station"]'],
    "Parking": ['["name"]["amenity"="parking"]'],
    "Rest Areas": ['["highway"~"^(rest_area|services)$"]'],
    "Toll Plazas": ['["barrier"="toll_booth"]'],
    "Police Stations": ['["name"]["amenity"="police"]'],
    "Supermarkets & Shops": ['["name"]["shop"~"^(supermarket|mall|department_store)$"]'],
    "Parks & Nature": ['["name"]["leisure"~"^(park|nature_reserve|garden)$"]'],
}


def split_route(coords, n):
    """Split a [(lat, lon), ...] route into n pieces of roughly equal length."""
    arr = np.asarray(coords, dtype=float)
    cum = cumulative_km(arr)
    total = cum[-1] if cum[-1] > 0 else 1e-9
    edges = np.clip(np.searchsorted(cum, np.linspace(0, total, n + 1)), 0, len(arr) - 1)
    parts = []
    for i in range(n):
        seg = arr[int(edges[i]):int(edges[i + 1]) + 1]
        if len(seg):
            parts.append(seg)
    return parts


def chunk_bbox(seg, radius_m):
    lat0 = float(seg[:, 0].mean())
    dlat = radius_m / 111000.0
    dlon = radius_m / (111000.0 * max(0.2, math.cos(math.radians(lat0))))
    return (round(float(seg[:, 0].min() - dlat), 4), round(float(seg[:, 1].min() - dlon), 4),
            round(float(seg[:, 0].max() + dlat), 4), round(float(seg[:, 1].max() + dlon), 4))


@st.cache_resource
def _chunk_cache():
    return {}


def fetch_chunk(cache, bbox, cats, start_ep):
    """Small bounding-box Overpass query (much cheaper than a route-long 'around' query).
    Tries the servers one after another, starting from a different one per segment."""
    key = (bbox, cats)
    hit = cache.get(key)
    if hit and time.time() - hit[0] < 600:
        return hit[1]
    box = "({},{},{},{})".format(*bbox)
    lines = [f"  nwr{sel}{box};" for c in cats for sel in CAT_QUERY[c]]
    query = "[out:json][timeout:18];\n(\n" + "\n".join(lines) + "\n);\nout center tags 1200;"
    errors = []
    for k in range(len(OVERPASS)):
        url = OVERPASS[(start_ep + k) % len(OVERPASS)]
        try:
            r = HTTP.post(url, data={"data": query}, timeout=(4, 20))
            r.raise_for_status()
            elements = r.json().get("elements", [])
            if len(cache) > 400:
                cache.clear()
            cache[key] = (time.time(), elements)
            return elements
        except (requests.RequestException, ValueError) as exc:
            errors.append(f"{url.split('/')[2]}: {str(exc)[:60]}")
    raise RuntimeError(" | ".join(errors))


def classify(tags):
    amenity, tourism = tags.get("amenity", ""), tags.get("tourism", "")
    healthcare = tags.get("healthcare", "")
    if amenity in ("restaurant", "fast_food"):
        return "Restaurants"
    if amenity == "cafe":
        return "Cafes"
    if amenity == "fuel":
        return "Petrol Pumps"
    if amenity in ("hospital", "clinic") or healthcare in ("hospital", "clinic"):
        return "Hospitals"
    if amenity == "pharmacy" or healthcare == "pharmacy":
        return "Pharmacies"
    if amenity in ("atm", "bank"):
        return "ATMs & Banks"
    if amenity == "charging_station":
        return "EV Charging"
    if amenity == "police":
        return "Police Stations"
    if amenity == "parking":
        return "Parking"
    if tags.get("highway") in ("rest_area", "services"):
        return "Rest Areas"
    if tags.get("barrier") == "toll_booth":
        return "Toll Plazas"
    if tourism in ("hotel", "guest_house", "hostel", "motel"):
        return "Hotels"
    if tags.get("historic") or tags.get("heritage") or amenity == "place_of_worship":
        return "Famous Places"
    if tourism in ("attraction", "museum", "viewpoint", "theme_park", "gallery", "zoo", "aquarium"):
        return "Tourist Attractions"
    if tags.get("shop") in ("supermarket", "mall", "department_store"):
        return "Supermarkets & Shops"
    if tags.get("leisure") in ("park", "nature_reserve", "garden"):
        return "Parks & Nature"
    return None


def find_places(route, cats, radius=3500, per_category=60):
    """Returns (items, warning). Route is searched in parallel segments; failed segments are reported."""
    cats = tuple(sorted(cats))
    n = int(min(14, max(1, math.ceil(route["distance_km"] / 20))))
    parts = split_route(route["coords"], n)
    cache = _chunk_cache()
    elements, failed, last_err = [], 0, ""
    with ThreadPoolExecutor(max_workers=5) as ex:
        futs = [ex.submit(fetch_chunk, cache, chunk_bbox(p, radius), cats, i % len(OVERPASS))
                for i, p in enumerate(parts)]
        for f in futs:
            try:
                elements.extend(f.result())
            except RuntimeError as exc:
                failed += 1
                last_err = str(exc)
    if failed == len(parts):
        raise RuntimeError("OpenStreetMap place servers are busy right now. Please try again in a few seconds. "
                           f"({last_err})")
    warn = None
    if failed:
        warn = (f"{failed} of {len(parts)} route segments timed out, so results may be incomplete. "
                "Press Search again – only the missing segments will be retried.")

    rows, seen = [], set()
    for el in elements:
        tags = el.get("tags", {})
        cat = classify(tags)
        if not cat or cat not in cats:
            continue
        name = (tags.get("name") or tags.get("name:en") or tags.get("operator") or tags.get("brand")
                or FALLBACK_NAME.get(cat))
        if not name:
            continue
        if el.get("type") == "node":
            lat, lon = el.get("lat"), el.get("lon")
        else:
            c = el.get("center", {})
            lat, lon = c.get("lat"), c.get("lon")
        key = f"{el.get('type')}:{el.get('id')}"
        if lat is None or lon is None or key in seen:
            continue
        seen.add(key)
        rows.append({"id": key, "name": name, "category": cat, "lat": float(lat), "lon": float(lon),
                     "hours": tags.get("opening_hours", "—"), "description": tags.get("description", "")})
    if not rows:
        return [], warn

    dist = distances_to_route_km([(r["lat"], r["lon"]) for r in rows], route["coords"])
    for r, d in zip(rows, dist):
        r["distance_km"] = round(float(d), 2)
    rows = [r for r in rows if r["distance_km"] <= radius / 1000.0]
    out = []
    for cat in CATS:
        items = sorted((r for r in rows if r["category"] == cat), key=lambda r: (r["distance_km"], r["name"].lower()))
        out.extend(items[:per_category])
    return out, warn


# ----------------------------------------------------------------------------
# Geoapify Places API – search along the route (OpenStreetMap data, free key, no credit card)
# ----------------------------------------------------------------------------
GEO_CATS = {
    "Restaurants": ["catering.restaurant", "catering.fast_food", "catering.food_court"],
    "Cafes": ["catering.cafe"],
    "Petrol Pumps": ["service.vehicle.fuel"],
    "Hospitals": ["healthcare.hospital", "healthcare.clinic_or_praxis"],
    "Hotels": ["accommodation.hotel", "accommodation.guest_house", "accommodation.hostel", "accommodation.motel"],
    "Tourist Attractions": ["tourism.attraction", "entertainment.museum", "entertainment.theme_park",
                            "entertainment.zoo", "entertainment.aquarium", "entertainment.culture.gallery"],
    "Famous Places": ["heritage", "tourism.sights"],
    "Pharmacies": ["healthcare.pharmacy"],
    "ATMs & Banks": ["service.financial.atm", "service.financial.bank"],
    "EV Charging": ["service.vehicle.charging_station"],
    "Parking": ["parking.cars"],
    "Police Stations": ["service.police"],
    "Supermarkets & Shops": ["commercial.supermarket", "commercial.shopping_mall", "commercial.department_store"],
    "Parks & Nature": ["leisure.park", "national_park"],
}  # Rest Areas and Toll Plazas have no Geoapify category – they are fetched from OpenStreetMap directly.


def geo_key():
    """Load the key from Streamlit Secrets or an environment variable."""
    if GEOAPIFY_API_KEY.strip():
        return GEOAPIFY_API_KEY.strip()
    try:
        k = st.secrets.get("GEOAPIFY_API_KEY", "")
    except Exception:  # no secrets file
        k = ""
    return str(k or os.environ.get("GEOAPIFY_API_KEY", "")).strip()


def encode_polyline(coords):
    """Encoded polyline (precision 5) for [(lat, lon), ...]."""
    out, plat, plon = [], 0, 0
    for lat, lon in coords:
        la, lo = int(round(lat * 1e5)), int(round(lon * 1e5))
        for v in (la - plat, lo - plon):
            v = ~(v << 1) if v < 0 else (v << 1)
            while v >= 0x20:
                out.append(chr((0x20 | (v & 0x1F)) + 63))
                v >>= 5
            out.append(chr(v + 63))
        plat, plon = la, lo
    return "".join(out)


def _geo_post(key, categories, encoded, buffer_m, offset):
    body = {"categories": categories, "lang": "en", "limit": 500, "offset": offset,
            "filter": {"type": "polyline", "geometry": encoded, "encoding": "polyline5", "buffer": buffer_m}}
    last = ""
    for _ in range(3):
        try:
            r = requests.post("https://api.geoapify.com/v2/places", params={"apiKey": key}, json=body,
                              timeout=(4, 25))
        except requests.RequestException as exc:
            last = str(exc)[:120]
            time.sleep(0.4)
            continue
        if r.status_code == 200:
            return r.json().get("features", [])
        try:
            msg = r.json().get("message") or r.json().get("error") or r.text[:120]
        except ValueError:
            msg = r.text[:120]
        last = f"HTTP {r.status_code}: {msg}"
        if r.status_code == 429 or r.status_code >= 500:
            try:
                wait = min(3.0, float(r.headers.get("Retry-After", "1")))
            except ValueError:
                wait = 1.0
            time.sleep(wait)
            continue
        break  # 4xx (bad key / bad request) will not fix itself
    raise RuntimeError(last)


def _geo_segment(key, categories, encoded, buffer_m, max_pages=3):
    feats = []
    for page in range(max_pages):
        got = _geo_post(key, categories, encoded, buffer_m, page * 500)
        feats.extend(got)
        if len(got) < 500:
            break
    return feats


def geo_match(category_list, selected):
    for ours in CATS:
        if ours not in selected or ours not in GEO_CATS:
            continue
        for k in GEO_CATS[ours]:
            if any(c == k or c.startswith(k + ".") for c in category_list):
                return ours
    return None


def find_places_geoapify(route, cats, key, buffer_m=4000, per_category=60):
    """Returns (items, warning). One request per ~50 km route segment, all categories at once."""
    geo_sel = [c for c in cats if c in GEO_CATS]
    extra = [c for c in cats if c not in GEO_CATS]
    warns, items_geo = [], []

    if geo_sel:
        flat = sorted({k for c in geo_sel for k in GEO_CATS[c]})
        n = int(min(8, max(1, math.ceil(route["distance_km"] / 50))))
        encs = []
        for seg in split_route(route["coords"], n):
            if len(seg) > 250:
                seg = seg[np.linspace(0, len(seg) - 1, 250).astype(int)]
            if len(seg) < 2:
                seg = np.vstack([seg, seg])
            encs.append(encode_polyline([(float(x), float(y)) for x, y in seg]))

        feats, errors = [], []
        with ThreadPoolExecutor(max_workers=4) as ex:
            futs = [ex.submit(_geo_segment, key, flat, enc, buffer_m) for enc in encs]
            for f in futs:
                try:
                    feats.extend(f.result())
                except RuntimeError as exc:
                    errors.append(str(exc))
        if errors and len(errors) == len(encs):
            hint = " Check your Geoapify API key." if ("401" in errors[0] or "403" in errors[0]) else ""
            raise RuntimeError(f"Geoapify error – {errors[0]}.{hint}")
        if errors:
            warns.append(f"{len(errors)} of {len(encs)} route segments failed, so results may be incomplete. "
                         "Press Search again to retry.")

        rows, seen = [], set()
        for f in feats:
            p = f.get("properties", {})
            raw = (p.get("datasource") or {}).get("raw") or {}
            cat = geo_match(p.get("categories", []), geo_sel)
            if not cat:
                continue
            lat, lon = p.get("lat"), p.get("lon")
            if lat is None or lon is None:
                try:
                    lon, lat = f["geometry"]["coordinates"][:2]
                except (KeyError, TypeError, ValueError):
                    continue
            name = (p.get("name") or raw.get("name") or raw.get("operator") or raw.get("brand")
                    or FALLBACK_NAME.get(cat))
            pid = p.get("place_id") or f"{lat},{lon}"
            if not name or (pid, cat) in seen:
                continue
            seen.add((pid, cat))
            rows.append({"id": f"{pid}:{cat}", "name": name, "category": cat, "lat": float(lat), "lon": float(lon),
                         "hours": raw.get("opening_hours") or p.get("opening_hours") or "—", "description": "",
                         "address": p.get("formatted") or p.get("address_line2") or ""})
        if rows:
            dist = distances_to_route_km([(r["lat"], r["lon"]) for r in rows], route["coords"])
            for r, d in zip(rows, dist):
                r["distance_km"] = round(float(d), 2)
            for cat in CATS:
                its = sorted((r for r in rows if r["category"] == cat),
                             key=lambda r: (r["distance_km"], r["name"].lower()))
                items_geo.extend(its[:per_category])

    items_extra = []
    if extra:  # categories Geoapify does not know (rest areas, toll plazas) come straight from OpenStreetMap
        try:
            items_extra, w = find_places(route, tuple(extra))
            if w:
                warns.append(w)
        except RuntimeError as exc:
            warns.append("Rest areas / toll plazas could not be loaded from OpenStreetMap right now "
                         f"({str(exc)[:90]}…).")
    return items_geo + items_extra, (" ".join(warns) or None)


# ----------------------------------------------------------------------------
# Weather timeline: forecast at the time you will actually pass each point
# ----------------------------------------------------------------------------
def build_timeline(route, wx_list, pts, depart_utc):
    out = []
    for i, (p, wx) in enumerate(zip(pts, wx_list)):
        offset = timedelta(seconds=wx.get("utc_offset_seconds", 0))
        local = depart_utc + timedelta(minutes=p["frac"] * route["duration_min"]) + offset
        h = wx["hourly"]
        first = datetime.fromisoformat(h["time"][0])
        idx = max(0, min(len(h["time"]) - 1, int(round((local - first).total_seconds() / 3600))))

        def g(key, default=0.0):
            v = h.get(key, [None] * (idx + 1))[idx]
            return default if v is None else v

        code = int(g("weather_code", 0))
        emoji, desc = WMO.get(code, ("🌡️", "Unknown"))
        label = "Start" if i == 0 else "Destination" if i == len(pts) - 1 else f"{p['km']:.0f} km"
        out.append({"label": label, "km": p["km"], "lat": p["lat"], "lon": p["lon"], "local": local,
                    "rain": float(g("precipitation_probability")), "temp": float(g("temperature_2m")),
                    "precip": float(g("precipitation")), "wind": float(g("wind_speed_10m")),
                    "emoji": emoji, "desc": desc, "idx": idx, "wx": wx, "offset": offset})
    return out


def advice(tl, mode):
    max_rain = max(t["rain"] for t in tl)
    max_temp = max(t["temp"] for t in tl)
    min_temp = min(t["temp"] for t in tl)
    max_wind = max(t["wind"] for t in tl)
    tips = []
    if max_rain >= 70:
        tips.append("Heavy rain is likely – consider shifting your departure or carrying full rain protection.")
        tips.append("Add 20–30% extra travel time and avoid waterlogged roads.")
    elif max_rain >= 40:
        tips.append("Showers are possible – keep an umbrella or rain jacket handy.")
        tips.append("Add about 10% extra travel time.")
    else:
        tips.append("Rain risk is low for this trip – enjoy the journey!")
    if mode == "Bike" and max_rain >= 40:
        tips.append("Wet roads reduce grip: brake early and keep a larger gap.")
    if mode == "Car" and max_rain >= 40:
        tips.append("Use headlights and keep your wipers in good condition.")
    if mode == "Walking" and max_rain >= 40:
        tips.append("Wear waterproof footwear and carry a poncho.")
    if max_temp >= 38:
        tips.append(f"Very hot ({max_temp:.0f} °C): carry water and avoid the midday sun.")
    if min_temp <= 5:
        tips.append(f"Cold ({min_temp:.0f} °C): dress in layers.")
    if max_wind >= 40:
        tips.append(f"Strong winds up to {max_wind:.0f} km/h – take extra care, especially on two wheels.")
    return tips


# ----------------------------------------------------------------------------
# Map
# ----------------------------------------------------------------------------
def _div_icon(markup):
    try:
        return folium.DivIcon(html=markup, icon_size=(0, 0), icon_anchor=(0, 0), class_name="eta-badge")
    except TypeError:  # older folium
        return folium.DivIcon(html=markup, icon_size=(0, 0), icon_anchor=(0, 0))


def build_map(a, route_idx, timeline=None, places=None, eta=None, sel_color=None):
    routes = a["routes"]
    sel = routes[route_idx]
    m = folium.Map(location=a["start"], zoom_start=7, tiles=None, control_scale=True)
    folium.TileLayer("OpenStreetMap", name="Street map", show=True).add_to(m)
    folium.TileLayer(
        tiles="https://server.arcgisonline.com/ArcGIS/rest/services/World_Street_Map/MapServer/tile/{z}/{y}/{x}",
        attr="Esri, HERE, Garmin, OpenStreetMap contributors", name="Esri streets", show=False).add_to(m)
    folium.TileLayer(
        tiles="https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}",
        attr="Esri, Maxar, Earthstar Geographics", name="Satellite", show=False).add_to(m)
    folium.TileLayer(
        tiles="https://tile.opentopomap.org/{z}/{x}/{y}.png",
        attr="© OpenTopoMap (CC-BY-SA), © OpenStreetMap contributors", name="Terrain", show=False).add_to(m)
    Fullscreen().add_to(m)

    fg_routes = folium.FeatureGroup(name="Routes", show=True)
    for i, r in enumerate(routes):
        if i == route_idx:
            continue
        folium.PolyLine(r["coords"], color=r["color"], weight=4, opacity=0.55, dash_array="8 8",
                        tooltip=f"{r['name']} | {r['distance_km']:.1f} km | {fmt_duration(r['duration_min'])}"
                        ).add_to(fg_routes)
    folium.PolyLine(sel["coords"], color=sel_color or sel["color"], weight=7, opacity=0.9,
                    tooltip=f"{sel['name']} | {sel['distance_km']:.1f} km | {fmt_duration(sel['duration_min'])}"
                    ).add_to(fg_routes)
    fg_routes.add_to(m)

    folium.Marker(a["start"], tooltip=f"Start: {a['start_name']}",
                  icon=folium.Icon(color="green", icon="play")).add_to(m)
    folium.Marker(a["end"], tooltip=f"Destination: {a['end_name']}",
                  icon=folium.Icon(color="red", icon="flag")).add_to(m)

    if timeline:
        fg_w = folium.FeatureGroup(name="Weather points", show=True)
        for t in timeline:
            lvl, color, _ = risk(t["rain"])
            folium.CircleMarker(
                [t["lat"], t["lon"]], radius=11, color="white", weight=2, fill=True, fill_color=color,
                fill_opacity=0.9,
                tooltip=f"{t['emoji']} {t['label']}: {t['rain']:.0f}% rain, {t['temp']:.0f} °C",
                popup=folium.Popup(
                    f"<b>{html.escape(t['label'])}</b><br>{t['emoji']} {t['desc']}<br>"
                    f"Reach ≈ {t['local'].strftime('%I:%M %p')}<br>Rain: {t['rain']:.0f}% ({lvl})<br>"
                    f"Temp: {t['temp']:.0f} °C · Wind: {t['wind']:.0f} km/h", max_width=240),
            ).add_to(fg_w)
        fg_w.add_to(m)

    if eta:
        fg_e = folium.FeatureGroup(name="Arrival time along route", show=True)
        for e in eta:
            tm = e["time"].strftime("%I:%M %p")
            badge = ('<div style="background:#0f172a;color:#fff;border-radius:12px;padding:2px 9px;'
                     'font-size:12px;font-weight:600;white-space:nowrap;box-shadow:0 1px 4px rgba(0,0,0,.45);'
                     f'transform:translate(-50%,-50%);display:inline-block">🕒 {tm}</div>')
            folium.Marker([e["lat"], e["lon"]], tooltip=f"{e['label']} · reach ≈ {tm}",
                          icon=_div_icon(badge)).add_to(fg_e)
        fg_e.add_to(m)

    if places:
        fg_p = folium.FeatureGroup(name="Places along route", show=True)
        cluster = MarkerCluster().add_to(fg_p)
        for p in places[:400]:
            _, color, _, glyph = CATS[p["category"]]
            folium.Marker(
                [p["lat"], p["lon"]], tooltip=f"{CATS[p['category']][0]} {p['name']}",
                popup=folium.Popup(f"<b>{html.escape(p['name'])}</b><br>{html.escape(p['category'])}"
                                   + (f"<br>⭐ {p['rating']} ({p.get('reviews') or 0})" if p.get("rating") else "")
                                   + f"<br>{p['distance_km']:.1f} km from route", max_width=260),
                icon=folium.Icon(color=color, icon=glyph),
            ).add_to(cluster)
        fg_p.add_to(m)

    lats, lons = [c[0] for c in sel["coords"]], [c[1] for c in sel["coords"]]
    m.fit_bounds([[min(lats), min(lons)], [max(lats), max(lons)]], padding=(30, 30))
    folium.LayerControl(collapsed=True).add_to(m)

    rows = []
    if timeline:
        rows.append('<span style="color:#16a34a;">●</span> Low rain · <span style="color:#f59e0b;">●</span> '
                    'Moderate · <span style="color:#dc2626;">●</span> High')
    if eta:
        rows.append("🕒 Expected arrival at each point")
    if places:
        present = {p["category"] for p in places}
        rows += [f'<span style="color:{c[2]};font-size:16px;">●</span> {n}' for n, c in CATS.items() if n in present]
    if len(routes) > 1:
        rows.append("╌╌ Other route options")
    if rows:
        legend = ('<div style="position:fixed;bottom:25px;left:25px;z-index:9999;background:rgba(255,255,255,.95);'
                  'color:#111;border:1px solid #bbb;border-radius:10px;padding:10px 14px;font-size:13px;'
                  'max-height:260px;overflow:auto;box-shadow:0 2px 8px rgba(0,0,0,.2)"><b>Legend</b><br>'
                  + "<br>".join(rows) + "</div>")
        m.get_root().html.add_child(folium.Element(legend))
    return m


# ----------------------------------------------------------------------------
# Styling
# ----------------------------------------------------------------------------
st.markdown("""
<style>
#MainMenu, footer {visibility: hidden;}
.block-container {padding-top: 1.6rem; max-width: 1300px;}
.hero {background: linear-gradient(120deg,#0f172a 0%,#1d4ed8 55%,#06b6d4 100%);
       padding: 26px 32px; border-radius: 20px; color: #fff; margin-bottom: 1.1rem;
       box-shadow: 0 10px 30px rgba(29,78,216,.28);}
.hero h1 {margin: 0; font-size: 2.2rem; color: #fff; padding: 0;}
.hero p {margin: 6px 0 0 0; opacity: .88; font-size: 1.02rem;}
.kpi {border-radius: 16px; padding: 14px 18px; background: rgba(120,130,160,.10);
      border: 1px solid rgba(120,130,160,.22); border-left: 6px solid var(--accent); height: 100%;}
.kpi .l {font-size: .74rem; text-transform: uppercase; letter-spacing: .07em; opacity: .7;}
.kpi .v {font-size: 1.65rem; font-weight: 700; line-height: 1.25;}
.kpi .s {font-size: .8rem; opacity: .7;}
.chip {display: inline-block; padding: 3px 12px; border-radius: 999px; margin: 3px 5px 3px 0;
       background: rgba(37,99,235,.14); font-size: .82rem;}
.alert {border-radius: 14px; padding: 14px 18px; margin: 8px 0 14px 0; border-left: 6px solid var(--c);
        background: rgba(120,130,160,.10);}
.alert b {font-size: 1.05rem;}
.feature {border-radius: 16px; padding: 18px; background: rgba(120,130,160,.10);
          border: 1px solid rgba(120,130,160,.22); height: 100%;}
.feature h4 {margin: 0 0 6px 0;}
div.stButton > button[kind="primary"], div[data-testid="stFormSubmitButton"] > button {
    background: linear-gradient(90deg,#2563eb,#06b6d4); border: 0; color: #fff; font-weight: 600;
    border-radius: 12px; padding: .55rem 1.2rem;}
div[data-testid="stTabs"] button {font-size: 1rem; font-weight: 600;}
</style>
""", unsafe_allow_html=True)


def kpi(label, value, sub="", accent="#2563EB"):
    return (f'<div class="kpi" style="--accent:{accent}"><div class="l">{label}</div>'
            f'<div class="v">{value}</div><div class="s">{sub}</div></div>')


st.markdown('<div class="hero"><h1>🌦️ RainRoute</h1>'
            '<p>Plan smarter journeys: routes, rain forecast at the time you pass each stretch, '
            'traffic insight and useful stops – all in one place.</p></div>', unsafe_allow_html=True)

# ----------------------------------------------------------------------------
# Input form
# ----------------------------------------------------------------------------
with st.form("route_form"):
    f1, f2, f3, f4 = st.columns([2, 2, 1.2, 1.6])
    source = f1.text_input("📍 Starting point", placeholder="e.g. Agra")
    destination = f2.text_input("🏁 Destination", placeholder="e.g. Delhi")
    travel_mode = f3.selectbox("Travel mode", ["Car", "Bike", "Walking"])
    depart_in = f4.slider("Leave in (hours)", 0, 24, 0, help="0 = leave now. Used to match the forecast to your trip.")
    submitted = st.form_submit_button("🔍 Analyse Route", **stretch())

if submitted:
    if not source.strip() or not destination.strip():
        st.warning("Please enter both a starting point and a destination.")
    else:
        try:
            with st.spinner("Locating places…"):
                s, e = geocode(source), geocode(destination)
            if not s or not e:
                missing = source if not s else destination
                st.error(f"Could not find “{missing}”. Try adding the state or country.")
            else:
                with st.spinner("Calculating routes…"):
                    start = (round(s["lat"], 5), round(s["lon"], 5))
                    end = (round(e["lat"], 5), round(e["lon"], 5))
                    routes, note = fetch_routes(start, end, travel_mode)
                st.session_state["analysis"] = {
                    "id": time.time(), "start": start, "end": end, "start_name": s["name"],
                    "end_name": e["name"], "mode": travel_mode, "routes": routes, "note": note,
                    "depart_utc": datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(hours=depart_in),
                    "depart_in": depart_in,
                }
                st.session_state["route_choice"] = 0
                st.session_state.pop("places", None)
                st.session_state.pop("places_error", None)
        except (requests.RequestException, RuntimeError) as exc:
            st.error(f"Could not calculate the route: {exc}")

a = st.session_state.get("analysis")

# ----------------------------------------------------------------------------
# Sidebar – places controls (defined before the map so filters apply instantly)
# ----------------------------------------------------------------------------
def _select_all():
    st.session_state["sel_cats"] = list(CATS)


def _select_default():
    st.session_state["sel_cats"] = list(DEFAULT_CATS)


def _select_none():
    st.session_state["sel_cats"] = []


with st.sidebar:
    st.markdown("## 📍 Places along route")
    if not a:
        st.info("Analyse a route first, then search for food, fuel, hospitals, hotels, ATMs, EV charging and more.")
        sel_cats, max_km, show_on_map = list(CATS), 2.0, True
    else:
        route_idx = min(st.session_state.get("route_choice", 0), len(a["routes"]) - 1)
        sig = (a["id"], route_idx)
        provider = st.radio("Search with", ["Geoapify (recommended)", "OpenStreetMap (no key)"],
                            key="places_provider")
        want_geo = provider.startswith("Geoapify")
        if want_geo:
            if geo_key():
                st.caption("🔑 Geoapify key loaded securely – ready to search.")
            else:
                st.caption("⚠️ No Geoapify key found in Streamlit Secrets. OpenStreetMap will be used instead.")
        if "sel_cats" not in st.session_state:
            st.session_state["sel_cats"] = list(DEFAULT_CATS)
        sel_cats = st.multiselect("Categories to search", list(CATS), key="sel_cats",
                                  format_func=lambda c: f"{CATS[c][0]} {c}")
        b1, b2, b3 = st.columns(3)
        b1.button("All", on_click=_select_all, **stretch())
        b2.button("Core", on_click=_select_default, **stretch())
        b3.button("None", on_click=_select_none, **stretch())
        max_km = st.slider("Max distance from route (km)", 0.5, 4.0, 2.0, 0.5)
        show_on_map = st.toggle("Show places on main map", value=True)
        if st.button("🔎 Search / refresh places", type="primary", **stretch()):
            if not sel_cats:
                st.session_state["places_error"] = "Please select at least one category to search."
            else:
                use_geo = want_geo and bool(geo_key())
                try:
                    with st.spinner(f"Searching {'Geoapify' if use_geo else 'OpenStreetMap'} along your route…"):
                        if use_geo:
                            items, warn = find_places_geoapify(a["routes"][route_idx], tuple(sel_cats), geo_key())
                        else:
                            items, warn = find_places(a["routes"][route_idx], tuple(sel_cats))
                    if want_geo and not use_geo:
                        warn = ((warn + " ") if warn else "") + "No Geoapify key found, so OpenStreetMap was used."
                    st.session_state["places"] = {"sig": sig, "items": items, "cats": list(sel_cats), "warn": warn,
                                                  "provider": "Geoapify" if use_geo else "OpenStreetMap",
                                                  "time": datetime.now().strftime("%d %b, %I:%M %p")}
                    st.session_state.pop("places_error", None)
                except (requests.RequestException, RuntimeError, ValueError) as exc:
                    st.session_state["places_error"] = str(exc)
        st.caption("Fewer categories = faster search. Category and distance filters apply instantly.")
    st.divider()
    st.caption("Data: Geoapify / OpenStreetMap · OSRM · Open-Meteo")

# ----------------------------------------------------------------------------
# Landing page
# ----------------------------------------------------------------------------
if not a:
    c1, c2, c3 = st.columns(3)
    c1.markdown('<div class="feature"><h4>🛣️ Smart routes</h4>Fastest, shortest and alternative roads for car, '
                'bike or walking, compared side by side.</div>', unsafe_allow_html=True)
    c2.markdown('<div class="feature"><h4>🌧️ Rain along the way</h4>Forecast matched to the time you will reach '
                'each stretch of the route – not just the start.</div>', unsafe_allow_html=True)
    c3.markdown('<div class="feature"><h4>📍 Useful stops</h4>Restaurants, fuel, hospitals, hotels and famous '
                'places near your path, plotted on the map.</div>', unsafe_allow_html=True)
    st.info("Enter a starting point and destination above, then press **Analyse Route**.")
    st.stop()

# ----------------------------------------------------------------------------
# Route selection
# ----------------------------------------------------------------------------
if a.get("note"):
    st.warning(a["note"])

routes = a["routes"]
st.markdown(f"#### {html.escape(a['start_name'])} → {html.escape(a['end_name'])} · {a['mode']}")
route_idx = st.radio(
    "Choose a route", range(len(routes)), key="route_choice", horizontal=True,
    format_func=lambda i: f"{routes[i]['name']} — {routes[i]['distance_km']:.0f} km · "
                          f"{fmt_duration(routes[i]['duration_min'])}",
)
route = routes[route_idx]

# ----------------------------------------------------------------------------
# Weather along route (single cached request)
# ----------------------------------------------------------------------------
timeline, wx_error = None, None
n_pts = int(min(8, max(3, route["distance_km"] // 60 + 2)))
w_pts = points_along(route["coords"], n_pts)
try:
    with st.spinner("Checking weather along your route…"):
        wx_list = fetch_weather(tuple((round(p["lat"], 3), round(p["lon"], 3)) for p in w_pts))
        timeline = build_timeline(route, wx_list, w_pts, a["depart_utc"])
except (requests.RequestException, RuntimeError, KeyError, ValueError, IndexError) as exc:
    wx_error = str(exc)

sys_offset = timedelta(seconds=datetime.now().astimezone().utcoffset().total_seconds())
offset = timeline[0]["offset"] if timeline else sys_offset
depart_local = a["depart_utc"] + offset
arrival_local = depart_local + timedelta(minutes=route["duration_min"])
max_rain = max((t["rain"] for t in timeline), default=None) if timeline else None

# KPI cards
k1, k2, k3, k4 = st.columns(4)
k1.markdown(kpi("Road distance", f"{route['distance_km']:.1f} km", route["name"], "#2563EB"), unsafe_allow_html=True)
k2.markdown(kpi("Estimated time", fmt_duration(route["duration_min"]),
                f"Avg {route['distance_km'] / (route['duration_min'] / 60):.0f} km/h", "#06b6d4"),
            unsafe_allow_html=True)
k3.markdown(kpi("Arrival (est.)", arrival_local.strftime("%I:%M %p"),
                f"{arrival_local.strftime('%a, %d %b')} · leave {depart_local.strftime('%I:%M %p')}", "#9333EA"),
            unsafe_allow_html=True)
if max_rain is not None:
    lvl, col, em = risk(max_rain)
    k4.markdown(kpi("Max rain chance", f"{max_rain:.0f}%", f"{em} {lvl} risk on route", col), unsafe_allow_html=True)
else:
    k4.markdown(kpi("Max rain chance", "—", "Weather unavailable", "#9ca3af"), unsafe_allow_html=True)

st.write("")

# Places for the selected route
pl = st.session_state.get("places")
places_valid = bool(pl) and pl["sig"] == (a["id"], route_idx)
all_places = pl["items"] if places_valid else []
visible = [p for p in all_places if p["category"] in sel_cats and p["distance_km"] <= max_km]
map_sig = f"{a['id']}-{route_idx}-{len(visible)}-{abs(hash(tuple(sorted(sel_cats))))}-{max_km}"

tab_map, tab_weather, tab_traffic, tab_places, tab_fuel = st.tabs(
    ["🗺️ Map", "🌧️ Weather", "🚦 Traffic & ETA", "📍 Places", "⛽ Fuel cost"])

# ---- Map ----------------------------------------------------------------
with tab_map:
    fmap = build_map(a, route_idx, timeline, visible if show_on_map else [])
    st_folium(fmap, height=560, use_container_width=True,
              key=f"map-{map_sig}-{show_on_map}", returned_objects=[])
    st.caption("Use the layer control (top-right of the map) to switch map style or hide layers. "
               "Dashed lines are the other route options.")

# ---- Weather --------------------------------------------------------------
with tab_weather:
    if not timeline:
        st.warning(f"Weather could not be loaded. {wx_error or ''} Please press Analyse Route again in a moment.")
    else:
        worst = max(timeline, key=lambda t: t["rain"])
        lvl, col, em = risk(max_rain)
        tips = advice(timeline, a["mode"])
        st.markdown(
            f'<div class="alert" style="--c:{col}"><b>{em} {lvl} rain risk</b> – up to {max_rain:.0f}% near '
            f'<b>{html.escape(worst["label"])}</b> around {worst["local"].strftime("%I:%M %p")}.<br>'
            + "<br>".join(f"• {html.escape(t)}" for t in tips) + "</div>", unsafe_allow_html=True)

        labels = [t["label"] for t in timeline]
        fig = go.Figure()
        fig.add_bar(x=labels, y=[t["rain"] for t in timeline], name="Rain probability (%)",
                    marker_color=[risk(t["rain"])[1] for t in timeline],
                    customdata=[t["local"].strftime("%I:%M %p") for t in timeline],
                    hovertemplate="%{x}<br>Reach ≈ %{customdata}<br>Rain: %{y:.0f}%<extra></extra>")
        fig.add_trace(go.Scatter(x=labels, y=[t["temp"] for t in timeline], mode="lines+markers",
                                 name="Temperature (°C)", yaxis="y2", line=dict(color="#f97316", width=3)))
        fig.update_layout(
            title="Forecast at the time you reach each point", height=380, hovermode="x unified",
            yaxis=dict(title="Rain probability (%)", range=[0, 100]),
            yaxis2=dict(title="Temperature (°C)", overlaying="y", side="right", showgrid=False),
            legend=dict(orientation="h", y=1.15), margin=dict(l=10, r=10, t=70, b=10),
            paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(0,0,0,0)")
        st.plotly_chart(fig, **stretch())

        df = pd.DataFrame({
            "Location": [t["label"] for t in timeline],
            "Reach at": [t["local"].strftime("%I:%M %p") for t in timeline],
            "Weather": [f"{t['emoji']} {t['desc']}" for t in timeline],
            "Rain %": [t["rain"] for t in timeline],
            "Temp °C": [round(t["temp"], 1) for t in timeline],
            "Wind km/h": [round(t["wind"]) for t in timeline],
        })
        st.dataframe(df, hide_index=True, **stretch(),
                     column_config={"Rain %": st.column_config.ProgressColumn("Rain %", min_value=0, max_value=100,
                                                                              format="%d%%")})

        with st.expander("⏱️ Hourly forecast at a point on your route"):
            pi = st.selectbox("Point", range(len(timeline)), format_func=lambda i: timeline[i]["label"],
                              key="hourly_point")
            t = timeline[pi]
            h = t["wx"]["hourly"]
            sl = slice(t["idx"], t["idx"] + 24)
            times = pd.to_datetime(h["time"][sl]).strftime("%a %I %p")
            rain = [v or 0 for v in h["precipitation_probability"][sl]]
            temps = h["temperature_2m"][sl]
            f2 = go.Figure()
            f2.add_bar(x=times, y=rain, name="Rain probability (%)", marker_color="#38bdf8")
            f2.add_trace(go.Scatter(x=times, y=temps, mode="lines+markers", name="Temperature (°C)",
                                    yaxis="y2", line=dict(color="#f97316", width=3)))
            f2.update_layout(height=340, hovermode="x unified", yaxis=dict(title="Rain %", range=[0, 100]),
                             yaxis2=dict(title="°C", overlaying="y", side="right", showgrid=False),
                             legend=dict(orientation="h", y=1.15), margin=dict(l=10, r=10, t=40, b=10),
                             paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(0,0,0,0)")
            st.plotly_chart(f2, **stretch())

# ---- Traffic & ETA ---------------------------------------------------------
with tab_traffic:
    st.info("Free routing services do not provide live traffic. The smart ETA below is a transparent estimate "
            "based on time of day and rain – use Google Maps for real-time congestion.")
    hr = depart_local.hour
    rush = a["mode"] == "Car" and (8 <= hr < 11 or 17 <= hr < 21)
    night = hr >= 22 or hr < 5
    factor = 1 + (0.15 if rush else 0) + (0.20 if (max_rain or 0) >= 70 else 0.10 if (max_rain or 0) >= 40 else 0)
    smart_min = route["duration_min"] * factor
    status = ("🔴 Peak hours – expect slowdowns near cities" if rush
              else "🟢 Light traffic expected (night)" if night else "🟡 Moderate traffic expected")
    t1, t2, t3 = st.columns(3)
    t1.metric("Base ETA (free-flow)", fmt_duration(route["duration_min"]))
    t2.metric("Smart ETA", fmt_duration(smart_min),
              f"+{fmt_duration(smart_min - route['duration_min'])}" if factor > 1 else "No delay expected",
              delta_color="inverse" if factor > 1 else "off")
    t3.metric("Typical traffic", status.split(" ", 1)[0], status.split(" ", 1)[1])
    gmode = {"Car": "driving", "Bike": "bicycling", "Walking": "walking"}[a["mode"]]
    gurl = (f"https://www.google.com/maps/dir/?api=1&origin={a['start'][0]},{a['start'][1]}"
            f"&destination={a['end'][0]},{a['end'][1]}&travelmode={gmode}")
    st.markdown(f"[🚦 Open live traffic in Google Maps ↗]({gurl})")

    status_color = "#dc2626" if rush else "#16a34a" if night else "#f59e0b"
    eta_pts = points_along(route["coords"], 6)
    eta = [{"lat": p["lat"], "lon": p["lon"], "label": f"{p['km']:.0f} km",
            "time": depart_local + timedelta(minutes=p["frac"] * smart_min)} for p in eta_pts[1:]]
    tmap = build_map(a, route_idx, None, None, eta=eta, sel_color=status_color)
    st_folium(tmap, height=500, use_container_width=True,
              key=f"traffic-{a['id']}-{route_idx}-{round(smart_min)}", returned_objects=[])
    st.caption("Route colour shows the expected traffic level (green = light, amber = moderate, red = peak). "
               "Time badges show when you should reach each point using the smart ETA.")

    cmp_df = pd.DataFrame({
        "Route": [r["name"] for r in routes],
        "Distance (km)": [round(r["distance_km"], 1) for r in routes],
        "Time": [fmt_duration(r["duration_min"]) for r in routes],
        "Avg speed (km/h)": [round(r["distance_km"] / (r["duration_min"] / 60)) for r in routes],
    })
    st.markdown("##### Route comparison")
    st.dataframe(cmp_df, hide_index=True, **stretch())

# ---- Places -------------------------------------------------------------------
with tab_places:
    if st.session_state.get("places_error"):
        st.warning(st.session_state["places_error"])
    if places_valid and pl.get("warn"):
        st.warning(pl["warn"])
    if not places_valid:
        st.info("Pick categories in the sidebar and press **🔎 Search / refresh places** to find stops along "
                "this route.")
    else:
        missing = [c for c in sel_cats if c not in pl.get("cats", [])]
        if missing:
            st.info("Not searched yet: " + ", ".join(f"{CATS[c][0]} {c}" for c in missing)
                    + ". Press **Search / refresh places** to include them.")
        in_range = [p for p in all_places if p["distance_km"] <= max_km]
        counts = {c: sum(1 for p in in_range if p["category"] == c) for c in pl.get("cats", [])}
        chips = "".join(f'<span class="chip">{CATS[c][0]} {c}: <b>{n}</b></span>' for c, n in counts.items())
        st.markdown(chips, unsafe_allow_html=True)
        st.caption(f"Source: {pl.get('provider', 'OpenStreetMap')} · last search {pl['time']} · "
                   f"showing places within {max_km:g} km of the route")

    pmap = build_map(a, route_idx, None, visible)
    st_folium(pmap, height=520, use_container_width=True, key=f"places-{map_sig}", returned_objects=[])

    if places_valid:
        if visible:
            cols = {
                "Name": [p["name"] for p in visible],
                "Category": [f"{CATS[p['category']][0]} {p['category']}" for p in visible],
                "Km from route": [p["distance_km"] for p in visible],
            }
            if any(p.get("rating") for p in visible):
                cols["Rating ⭐"] = [p.get("rating") for p in visible]
                cols["Reviews"] = [p.get("reviews") for p in visible]
            if any(p.get("address") for p in visible):
                cols["Address"] = [p.get("address", "") for p in visible]
            cols["Opening hours"] = [p["hours"] for p in visible]
            cols["Navigate"] = [p.get("uri") or f"https://www.google.com/maps/search/?api=1&query={p['lat']},{p['lon']}"
                                for p in visible]
            pdf = pd.DataFrame(cols)
            st.dataframe(pdf, hide_index=True, height=420, **stretch(),
                         column_config={"Navigate": st.column_config.LinkColumn("Navigate", display_text="Open ↗")})
        else:
            st.info("No places match the selected categories and distance. Try more categories or a larger distance.")


# ---- Fuel cost ------------------------------------------------------------------
with tab_fuel:
    mode = a["mode"]
    if mode == "Walking":
        st.info("🚶 The fuel estimator is disabled for walking trips. Switch the travel mode to Car or Bike "
                "and press Analyse Route to use it.")
    else:
        st.markdown("##### ⛽ Estimated fuel cost")
        f1, f2, f3 = st.columns(3)
        if mode == "Car":
            fuel_type = f1.selectbox("Fuel type", ["Petrol", "Diesel"], key="fuel_type")
        else:
            fuel_type = "Petrol"
            f1.selectbox("Fuel type", ["Petrol"], disabled=True, key="fuel_type_bike")
        mileage = f2.number_input("Mileage (km per litre)", min_value=1.0, max_value=150.0,
                                  value=FUEL_DEFAULT_MILEAGE[mode], step=1.0, key=f"fuel_mileage_{mode}",
                                  help=f"Default for {mode}: {FUEL_DEFAULT_MILEAGE[mode]:.0f} km/L. Change it to match your vehicle.")
        round_trip = f3.checkbox("Round trip (×2)", key="fuel_round_trip")

        auto_rate, auto_text = auto_fuel_rate(a["start"], a["end"], fuel_type)
        use_auto = st.toggle("Use automatic fuel rate", value=True, key="fuel_use_auto")
        if use_auto:
            rate = auto_rate
            st.metric(f"{fuel_type} rate (automatic)", f"₹{rate:.2f} / L")
        else:
            rate = st.number_input(f"{fuel_type} rate (₹ per litre) – edit manually", min_value=1.0, max_value=500.0,
                                   value=float(round(auto_rate, 2)), step=0.5, key=f"fuel_rate_manual_{fuel_type}")
        st.caption(auto_text if use_auto else "Manual rate in use – your value is used for all calculations.")

        trips = 2 if round_trip else 1
        dist = route["distance_km"] * trips
        litres = dist / mileage
        cost = litres * rate
        m1, m2, m3, m4 = st.columns(4)
        m1.metric("Distance", f"{dist:.1f} km")
        m2.metric("Fuel needed", f"{litres:.1f} L")
        m3.metric("Estimated cost", f"₹{cost:,.0f}")
        m4.metric("Cost per km", f"₹{rate / mileage:.2f}")

        st.markdown("##### Route comparison")
        fdf = pd.DataFrame({
            "Route": [r["name"] for r in routes],
            "Distance (km)": [round(r["distance_km"] * trips, 1) for r in routes],
            "Fuel (L)": [round(r["distance_km"] * trips / mileage, 1) for r in routes],
            "Cost (₹)": [round(r["distance_km"] * trips / mileage * rate) for r in routes],
        })
        st.dataframe(fdf, hide_index=True, **stretch())
        st.caption("Estimate only: real mileage changes with traffic, load, speed and driving style. "
                   "Tolls and parking are not included.")
