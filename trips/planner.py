"""Combine the route from the map service with the HOS simulator."""

from __future__ import annotations

from datetime import datetime

from . import hos, maps

# Routing data is for cars; loaded trucks average less on the open road.
MAX_TRUCK_MPH = 65

STOP_PRIORITY = ["pickup", "dropoff", "restart", "rest", "fuel", "break", "pre_trip"]
STOP_TITLES = {
    "pickup": "Pickup",
    "dropoff": "Drop-off",
    "restart": "34-hour restart",
    "rest": "10-hour rest",
    "fuel": "Fuel stop",
    "break": "30-minute break",
    "pre_trip": "Start",
}


def plan(current: dict, pickup: dict, dropoff: dict, cycle_used: float, start: datetime) -> dict:
    routed = maps.route([current, pickup, dropoff])
    total_miles = sum(leg["miles"] for leg in routed["legs"])
    line = maps.RouteLine(routed["geometry"], total_miles)

    legs = []
    for leg, activity, place in zip(routed["legs"], ("pickup", "dropoff"), (pickup, dropoff)):
        minutes = max(leg["minutes"], leg["miles"] / MAX_TRUCK_MPH * 60)
        legs.append(hos.Leg(leg["miles"], minutes, activity, place["label"]))

    segments = hos.simulate(legs, cycle_used, start)
    stops = _build_stops(segments, line, current, pickup, dropoff, legs[0].miles, total_miles)
    logs = hos.build_daily_logs(segments, cycle_used)

    for log in logs:
        log["from"] = current["label"]
        log["to"] = dropoff["label"]

    driving_min = sum(s.minutes for s in segments if s.status == hos.DRIVING)
    on_duty_min = sum(s.minutes for s in segments if s.status == hos.ON_DUTY)
    return {
        "locations": {"current": current, "pickup": pickup, "dropoff": dropoff},
        "summary": {
            "total_miles": round(total_miles, 1),
            "driving_hours": round(driving_min / 60, 2),
            "on_duty_hours": round((driving_min + on_duty_min) / 60, 2),
            "trip_hours": round((segments[-1].end - segments[0].start).total_seconds() / 3600, 2),
            "start": segments[0].start.isoformat(),
            "end": segments[-1].end.isoformat(),
            "days": len(logs),
            "cycle_used_start": cycle_used,
            "cycle_used_end": logs[-1]["recap"]["cycle_used"],
            "counts": {kind: sum(1 for s in segments if s.kind == kind)
                       for kind in ("rest", "fuel", "break", "restart")},
        },
        "route": {
            "geometry": line.simplified(),
            "legs": [
                {
                    "from": a["label"],
                    "to": b["label"],
                    "miles": round(leg.miles, 1),
                    "drive_hours": round(leg.minutes / 60, 2),
                    "steps": maps.describe_steps(raw["steps"]),
                }
                for leg, raw, a, b in zip(legs, routed["legs"], (current, pickup),
                                          (pickup, dropoff))
            ],
        },
        "stops": stops,
        "timeline": [
            {
                "status": s.status,
                "kind": s.kind,
                "note": s.note,
                "start": s.start.isoformat(),
                "end": s.end.isoformat(),
                "minutes": s.minutes,
                "miles": round(s.end_mile - s.start_mile, 1),
                "location": s.location,
            }
            for s in segments
        ],
        "daily_logs": logs,
    }


def _build_stops(segments, line, current, pickup, dropoff, pickup_mile, total_miles) -> list[dict]:
    """Group consecutive non-driving segments into map stops and name every location."""
    groups: list[list[hos.Segment]] = []
    previous_was_stop = False
    for seg in segments:
        is_stop = seg.kind != "drive"
        if is_stop and previous_was_stop:
            groups[-1].append(seg)
        elif is_stop:
            groups.append([seg])
        previous_was_stop = is_stop

    stops = []
    for group in groups:
        kinds = [s.kind for s in group]
        kind = next(k for k in STOP_PRIORITY if k in kinds)
        mile = group[0].start_mile
        lat, lng = line.at_mile(mile)
        if mile <= 0.5:
            place = current
        elif abs(mile - pickup_mile) <= 0.5:
            place = pickup
        elif abs(mile - total_miles) <= 0.5:
            place = dropoff
        else:
            label = maps.reverse_geocode(lat, lng) or f"Mile {round(mile)} en route"
            place = {"lat": lat, "lng": lng, "label": label}
        for seg in group:
            seg.location = place["label"]
        stops.append({
            "type": "start" if kind == "pre_trip" else kind,
            "title": STOP_TITLES[kind],
            "activities": [s.note for s in group],
            "location": place["label"],
            "lat": round(place["lat"], 5),
            "lng": round(place["lng"], 5),
            "mile": round(mile, 1),
            "arrive": group[0].start.isoformat(),
            "depart": group[-1].end.isoformat(),
            "minutes": sum(s.minutes for s in group),
        })

    # A driving segment's remark is where the driving began.
    location = current["label"]
    for seg in segments:
        if seg.kind == "drive":
            seg.location = location
        else:
            location = seg.location
    return stops
