"""Offline place lookup for North America.

Backed by a bundled extract of the GeoNames "cities1000" dataset (every town
of 1,000+ people in the US, Canada and Mexico), so type-ahead suggestions and
stop naming need no network call and can never be rate limited.
"""

from __future__ import annotations

import csv
import math
import re
import unicodedata
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

DATA_FILE = Path(__file__).parent / "data" / "places.csv"

STATE_NAMES = {
    "alabama": "AL", "alaska": "AK", "arizona": "AZ", "arkansas": "AR", "california": "CA",
    "colorado": "CO", "connecticut": "CT", "delaware": "DE", "district of columbia": "DC",
    "florida": "FL", "georgia": "GA", "hawaii": "HI", "idaho": "ID", "illinois": "IL",
    "indiana": "IN", "iowa": "IA", "kansas": "KS", "kentucky": "KY", "louisiana": "LA",
    "maine": "ME", "maryland": "MD", "massachusetts": "MA", "michigan": "MI", "minnesota": "MN",
    "mississippi": "MS", "missouri": "MO", "montana": "MT", "nebraska": "NE", "nevada": "NV",
    "new hampshire": "NH", "new jersey": "NJ", "new mexico": "NM", "new york": "NY",
    "north carolina": "NC", "north dakota": "ND", "ohio": "OH", "oklahoma": "OK", "oregon": "OR",
    "pennsylvania": "PA", "rhode island": "RI", "south carolina": "SC", "south dakota": "SD",
    "tennessee": "TN", "texas": "TX", "utah": "UT", "vermont": "VT", "virginia": "VA",
    "washington": "WA", "west virginia": "WV", "wisconsin": "WI", "wyoming": "WY",
    "alberta": "AB", "british columbia": "BC", "manitoba": "MB", "new brunswick": "NB",
    "newfoundland and labrador": "NL", "nova scotia": "NS", "ontario": "ON",
    "prince edward island": "PE", "quebec": "QC", "saskatchewan": "SK", "mexico": "MX",
}

# Beyond this distance the nearest town is not a meaningful name for a point.
MAX_NEAREST_MILES = 60


@dataclass(frozen=True)
class Place:
    name: str
    state: str
    lat: float
    lng: float
    key: str  # normalised name used for matching

    @property
    def label(self) -> str:
        return f"{self.name}, {self.state}"

    def as_dict(self) -> dict:
        return {"lat": self.lat, "lng": self.lng, "label": self.label}


def _normalise(text: str) -> str:
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    text = re.sub(r"[^a-z0-9 ]", "", text.lower().replace("-", " "))
    text = re.sub(r"\b(saint|sainte)\b", "st", text)
    return " ".join(text.split())


@lru_cache(maxsize=1)
def _places() -> tuple[Place, ...]:
    """All places, largest population first."""
    with DATA_FILE.open(encoding="utf-8", newline="") as handle:
        return tuple(
            Place(row["name"], row["state"], float(row["lat"]), float(row["lng"]),
                  _normalise(row["name"]))
            for row in csv.DictReader(handle)
        )


def _split(query: str) -> tuple[str, str]:
    """'Springfield, Illinois' -> ('springfield', 'IL'); the region may be partial."""
    name, _, region = query.partition(",")
    region = _normalise(region.replace("USA", "").replace("United States", ""))
    return _normalise(name), STATE_NAMES.get(region, region.upper())


def suggest(query: str, limit: int = 6) -> list[dict]:
    """Places whose name starts with the query, most populous first."""
    name, region = _split(query)
    if len(name) < 2:
        return []
    starts, contains = [], []
    for place in _places():
        if region and not place.state.startswith(region):
            continue
        if place.key.startswith(name):
            starts.append(place)
            if len(starts) >= limit:
                break
        elif len(contains) < limit and f" {name}" in place.key:
            contains.append(place)
    return [place.as_dict() for place in (starts + contains)[:limit]]


def lookup(query: str) -> dict | None:
    """Exact 'City' or 'City, ST' match; the largest such place wins."""
    name, region = _split(query)
    if not name:
        return None
    for place in _places():
        if place.key == name and (not region or place.state == region):
            return place.as_dict()
    return None


def nearest(lat: float, lng: float) -> str | None:
    """Label of the closest town, or None if nothing is reasonably near."""
    cos_lat = math.cos(math.radians(lat))
    best, best_d2 = None, float("inf")
    for place in _places():
        dy = place.lat - lat
        dx = (place.lng - lng) * cos_lat
        d2 = dx * dx + dy * dy
        if d2 < best_d2:
            best, best_d2 = place, d2
    if best is None or math.sqrt(best_d2) * 69.0 > MAX_NEAREST_MILES:
        return None
    return best.label
