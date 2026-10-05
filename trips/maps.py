"""Geocoding and routing.

* Bundled gazetteer - type-ahead suggestions, city lookups and stop naming (offline)
* OSRM              - routing
* Nominatim         - fallback geocoder for street addresses and places abroad
"""

from __future__ import annotations

import hashlib
import math
import re

import requests
from django.conf import settings
from django.core.cache import cache

from . import gazetteer

METERS_PER_MILE = 1609.344
TIMEOUT = 12
DAY = 60 * 60 * 24

_session = requests.Session()


class MapServiceError(Exception):
    """A map provider failed or returned nothing usable."""


def _get(url: str, params: dict, timeout: float = TIMEOUT) -> dict | list:
    try:
        response = _session.get(url, params=params, timeout=timeout,
                                headers={"User-Agent": settings.MAPS_USER_AGENT})
        response.raise_for_status()
        return response.json()
    except (requests.RequestException, ValueError) as exc:
        raise MapServiceError(f"Map service unavailable ({url.split('/')[2]})") from exc


def _cache_key(prefix: str, text: str) -> str:
    return f"{prefix}:{hashlib.md5(text.strip().lower().encode()).hexdigest()}"


def _short_label(address: dict, fallback: str) -> str:
    """'Dallas, TX' style label, as written in a log book's remarks."""
    place = next((address[k] for k in ("city", "town", "village", "hamlet", "municipality",
                                       "suburb", "county") if address.get(k)), None)
    state = address.get("ISO3166-2-lvl4", "")
    state = state.split("-")[-1] if state else address.get("state", "")
    parts = [p for p in (place, state) if p]
    return ", ".join(parts) if parts else fallback


_COORDS = re.compile(r"^\s*(-?\d+(?:\.\d+)?)\s*,\s*(-?\d+(?:\.\d+)?)\s*$")


def geocode(query: str) -> dict:
    """Resolve free text (or a 'lat,lng' pair) to a point."""
    match = _COORDS.match(query)
    if match:
        lat, lng = float(match[1]), float(match[2])
        if not (-90 <= lat <= 90 and -180 <= lng <= 180):
            raise MapServiceError(f"Invalid coordinates: {query}")
        label = reverse_geocode(lat, lng) or f"{lat:.4f}, {lng:.4f}"
        return {"lat": lat, "lng": lng, "label": label, "name": label}

    known = gazetteer.lookup(query)
    if known:
        return {**known, "name": known["label"]}

    key = _cache_key("geocode", query)
    hit = cache.get(key)
    if hit:
        return hit
    results = _get(f"{settings.NOMINATIM_URL}/search",
                   {"q": query, "format": "jsonv2", "limit": 1, "addressdetails": 1})
    if not results:
        raise LookupError(query)
    top = results[0]
    point = {
        "lat": float(top["lat"]),
        "lng": float(top["lon"]),
        "label": _short_label(top.get("address", {}), top["display_name"]),
        "name": top["display_name"],
    }
    cache.set(key, point, DAY)
    return point


def reverse_geocode(lat: float, lng: float) -> str | None:
    """Nearest town for a point, or None if the lookup fails."""
    local = gazetteer.nearest(lat, lng)
    if local:
        return local
    key = f"reverse:{lat:.2f},{lng:.2f}"
    hit = cache.get(key)
    if hit:
        return hit
    try:
        data = _get(f"{settings.NOMINATIM_URL}/reverse",
                    {"lat": lat, "lon": lng, "format": "jsonv2", "zoom": 12,
                     "addressdetails": 1}, timeout=6)
    except MapServiceError:
        return None
    label = _short_label(data.get("address", {}), "") if isinstance(data, dict) else ""
    if label:
        cache.set(key, label, DAY)
    return label or None


def route(points: list[dict]) -> dict:
    """Driving route through the given points, with one leg between each pair."""
    coords = ";".join(f"{p['lng']:.6f},{p['lat']:.6f}" for p in points)
    data = _get(f"{settings.OSRM_URL}/route/v1/driving/{coords}",
                {"overview": "full", "geometries": "geojson", "steps": "true"}, timeout=25)
    if data.get("code") != "Ok" or not data.get("routes"):
        raise LookupError("route")
    best = data["routes"][0]
    return {
        "geometry": [(lat, lng) for lng, lat in best["geometry"]["coordinates"]],
        "legs": [
            {
                "miles": leg["distance"] / METERS_PER_MILE,
                "minutes": leg["duration"] / 60,
                "steps": leg["steps"],
            }
            for leg in best["legs"]
        ],
    }


# ---------------------------------------------------------------------------
# Geometry helpers
# ---------------------------------------------------------------------------

def _haversine_miles(a: tuple[float, float], b: tuple[float, float]) -> float:
    lat1, lng1, lat2, lng2 = map(math.radians, (*a, *b))
    h = (math.sin((lat2 - lat1) / 2) ** 2
         + math.cos(lat1) * math.cos(lat2) * math.sin((lng2 - lng1) / 2) ** 2)
    return 3958.7613 * 2 * math.asin(math.sqrt(h))


class RouteLine:
    """Route polyline that can be queried by distance travelled."""

    def __init__(self, geometry: list[tuple[float, float]], total_miles: float):
        self.points = geometry
        self.cumulative = [0.0]
        for a, b in zip(geometry, geometry[1:]):
            self.cumulative.append(self.cumulative[-1] + _haversine_miles(a, b))
        # Scale so the polyline length matches the router's reported distance.
        raw = self.cumulative[-1]
        self.scale = raw / total_miles if total_miles and raw else 1.0

    def at_mile(self, mile: float) -> tuple[float, float]:
        target = max(0.0, min(mile * self.scale, self.cumulative[-1]))
        lo, hi = 0, len(self.cumulative) - 1
        while lo < hi:
            mid = (lo + hi) // 2
            if self.cumulative[mid] < target:
                lo = mid + 1
            else:
                hi = mid
        if lo == 0:
            return self.points[0]
        a, b = self.points[lo - 1], self.points[lo]
        span = self.cumulative[lo] - self.cumulative[lo - 1]
        f = (target - self.cumulative[lo - 1]) / span if span else 0
        return (a[0] + (b[0] - a[0]) * f, a[1] + (b[1] - a[1]) * f)

    def simplified(self, max_points: int = 2500) -> list[list[float]]:
        step = max(1, math.ceil(len(self.points) / max_points))
        pts = self.points[::step]
        if pts[-1] != self.points[-1]:
            pts.append(self.points[-1])
        return [[round(lat, 5), round(lng, 5)] for lat, lng in pts]


def describe_steps(steps: list[dict]) -> list[dict]:
    """Turn OSRM maneuvers into readable turn-by-turn instructions."""
    out = []
    for step in steps:
        maneuver = step["maneuver"]
        kind = maneuver.get("type", "")
        modifier = maneuver.get("modifier", "")
        road = " / ".join(p for p in (step.get("ref"), step.get("name")) if p)
        onto = f" onto {road}" if road else ""
        if kind == "depart":
            text = f"Head out on {road}" if road else "Head out"
        elif kind == "arrive":
            text = "Arrive at destination"
        elif kind in ("on ramp", "off ramp"):
            side = f" on the {modifier.replace('slight ', '')}" if modifier else ""
            text = f"Take the ramp{side}{onto}"
        elif kind == "merge":
            text = f"Merge{onto}"
        elif kind == "fork":
            text = f"Keep {modifier.replace('slight ', '') or 'straight'} at the fork{onto}"
        elif kind in ("roundabout", "rotary"):
            exit_no = maneuver.get("exit")
            text = f"At the roundabout take exit {exit_no}{onto}" if exit_no else \
                f"Enter the roundabout{onto}"
        elif kind in ("new name", "continue") and modifier in ("straight", ""):
            text = f"Continue{onto}"
        elif modifier == "uturn":
            text = f"Make a U-turn{onto}"
        elif modifier == "straight":
            text = f"Continue straight{onto}"
        elif modifier:
            verb = "Keep" if "slight" in modifier else "Turn"
            text = f"{verb} {modifier.replace('slight ', '').replace('sharp ', 'sharp ')}{onto}"
        else:
            text = f"Continue{onto}"
        miles = step["distance"] / METERS_PER_MILE
        if kind not in ("depart", "arrive") and miles < 0.05:
            continue
        out.append({"text": text, "miles": round(miles, 1), "kind": kind, "modifier": modifier})
    return out
