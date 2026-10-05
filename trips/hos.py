"""
Hours-of-Service trip simulator.

Implements the FMCSA rules for a property-carrying driver on the
70-hour / 8-day cycle (49 CFR Part 395), with no adverse-driving or
short-haul exceptions:

* 11-hour driving limit per shift
* 14-hour driving window per shift
* 30-minute break after 8 cumulative hours of driving
* 10 consecutive hours off duty between shifts
* 70 on-duty hours in 8 days, cleared by a 34-hour restart

The simulator is pure Python and knows nothing about maps: it takes the legs
of a trip as (miles, minutes) and returns a minute-accurate duty timeline.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime, timedelta

OFF_DUTY = "off_duty"
SLEEPER = "sleeper_berth"
DRIVING = "driving"
ON_DUTY = "on_duty"

MAX_DRIVING_MIN = 11 * 60
MAX_WINDOW_MIN = 14 * 60
BREAK_AFTER_DRIVING_MIN = 8 * 60
BREAK_MIN = 30
DAILY_REST_MIN = 10 * 60
CYCLE_LIMIT_MIN = 70 * 60
RESTART_MIN = 34 * 60

FUEL_INTERVAL_MILES = 1000
FUEL_STOP_MIN = 30
PICKUP_MIN = 60
DROPOFF_MIN = 60
PRE_TRIP_MIN = 15

# Driving stints shorter than this are not worth starting before a mandatory stop.
MIN_DRIVE_MIN = 1


@dataclass
class Leg:
    """One driving leg of the trip, ending in an on-duty activity."""

    miles: float
    minutes: float
    end_activity: str  # "pickup" | "dropoff"
    end_location: str = ""


@dataclass
class Segment:
    status: str
    start: datetime
    end: datetime
    note: str = ""
    kind: str = ""  # drive | pickup | dropoff | fuel | break | rest | restart | pre_trip
    start_mile: float = 0.0
    end_mile: float = 0.0
    location: str = ""

    @property
    def minutes(self) -> int:
        return int(round((self.end - self.start).total_seconds() / 60))


@dataclass
class _State:
    now: datetime
    cycle_min: float
    mile: float = 0.0
    shift_start: datetime | None = None
    shift_driving_min: float = 0.0
    driving_since_break_min: float = 0.0
    miles_since_fuel: float = 0.0
    segments: list[Segment] = field(default_factory=list)

    def add(self, status: str, minutes: float, kind: str, note: str, miles: float = 0.0,
            location: str = "") -> None:
        minutes = int(round(minutes))
        if minutes <= 0:
            return
        end = self.now + timedelta(minutes=minutes)
        self.segments.append(
            Segment(status, self.now, end, note, kind, self.mile, self.mile + miles, location)
        )
        self.now = end
        self.mile += miles
        if status in (DRIVING, ON_DUTY):
            self.cycle_min += minutes
        if status == DRIVING:
            self.shift_driving_min += minutes
            self.driving_since_break_min += minutes
            self.miles_since_fuel += miles
        else:
            # Any non-driving period of 30+ consecutive minutes satisfies the break rule.
            if minutes >= BREAK_MIN:
                self.driving_since_break_min = 0

    def start_shift(self) -> None:
        self.shift_start = self.now
        self.shift_driving_min = 0
        self.driving_since_break_min = 0
        self.add(ON_DUTY, PRE_TRIP_MIN, "pre_trip", "Pre-trip inspection")

    def window_left(self) -> float:
        elapsed = (self.now - self.shift_start).total_seconds() / 60
        return MAX_WINDOW_MIN - elapsed

    def daily_rest(self) -> None:
        self.add(SLEEPER, DAILY_REST_MIN, "rest", "10-hour rest")
        self.start_shift()

    def restart(self) -> None:
        self.add(OFF_DUTY, RESTART_MIN, "restart", "34-hour restart")
        self.cycle_min = 0
        self.start_shift()


def simulate(legs: list[Leg], cycle_used_hours: float, start: datetime) -> list[Segment]:
    """Return the duty-status timeline for the whole trip."""
    state = _State(now=start, cycle_min=cycle_used_hours * 60)

    if state.cycle_min >= CYCLE_LIMIT_MIN:
        # No hours left to even begin: the trip starts with a restart.
        state.add(OFF_DUTY, RESTART_MIN, "restart", "34-hour restart")
        state.cycle_min = 0
    state.start_shift()

    for leg in legs:
        _drive_leg(state, leg)
        if leg.end_activity == "pickup":
            state.add(ON_DUTY, PICKUP_MIN, "pickup", "Pickup - loading", location=leg.end_location)
        else:
            state.add(ON_DUTY, DROPOFF_MIN, "dropoff", "Drop-off - unloading",
                      location=leg.end_location)

    return state.segments


def _drive_leg(state: _State, leg: Leg) -> None:
    remaining_miles = leg.miles
    if remaining_miles <= 0.05 or leg.minutes <= 0:
        return
    mph = leg.miles / (leg.minutes / 60)

    while remaining_miles > 0.05:
        remaining_min = remaining_miles / mph * 60

        limits = {
            "cycle": CYCLE_LIMIT_MIN - state.cycle_min,
            "driving": MAX_DRIVING_MIN - state.shift_driving_min,
            "window": state.window_left(),
            "break": BREAK_AFTER_DRIVING_MIN - state.driving_since_break_min,
            "fuel": (FUEL_INTERVAL_MILES - state.miles_since_fuel) / mph * 60,
        }
        reason = min(limits, key=limits.get)
        available = limits[reason]

        if remaining_min <= available:
            # The HOS limits are whole minutes, so rounding up never exceeds one.
            state.add(DRIVING, max(1, math.ceil(remaining_min)), "drive", "Driving",
                      miles=remaining_miles)
            return
        if available >= MIN_DRIVE_MIN:
            minutes = int(available)
            miles = mph * minutes / 60
            state.add(DRIVING, minutes, "drive", "Driving", miles=miles)
            remaining_miles -= miles
            continue

        if reason == "cycle":
            state.restart()
        elif reason in ("driving", "window"):
            state.daily_rest()
        elif reason == "break":
            state.add(OFF_DUTY, BREAK_MIN, "break", "30-minute break")
        else:
            state.add(ON_DUTY, FUEL_STOP_MIN, "fuel", "Fuel stop")
            state.miles_since_fuel = 0


# ---------------------------------------------------------------------------
# Daily logs
# ---------------------------------------------------------------------------

STATUS_ORDER = [OFF_DUTY, SLEEPER, DRIVING, ON_DUTY]


def build_daily_logs(segments: list[Segment], cycle_used_hours: float) -> list[dict]:
    """Split the timeline at midnight into one 24-hour log per calendar day.

    Time before the trip starts and after it ends is logged as off duty, so
    every sheet totals exactly 24 hours.
    """
    if not segments:
        return []

    first_day = segments[0].start.replace(hour=0, minute=0, second=0, microsecond=0)
    last_end = segments[-1].end
    last_day = last_end.replace(hour=0, minute=0, second=0, microsecond=0)
    if last_end == last_day and last_day > first_day:
        last_day -= timedelta(days=1)

    padded = list(segments)
    if segments[0].start > first_day:
        padded.insert(0, Segment(OFF_DUTY, first_day, segments[0].start, "", "off"))
    end_of_last = last_day + timedelta(days=1)
    if last_end < end_of_last:
        padded.append(Segment(OFF_DUTY, last_end, end_of_last, "", "off",
                              segments[-1].end_mile, segments[-1].end_mile))

    logs = []
    cycle_min = cycle_used_hours * 60
    day = first_day
    while day <= last_day:
        next_day = day + timedelta(days=1)
        entries: list[dict] = []
        remarks: list[dict] = []
        totals = dict.fromkeys(STATUS_ORDER, 0)
        miles = 0.0

        for seg in padded:
            if seg.end <= day or seg.start >= next_day:
                continue
            start = max(seg.start, day)
            end = min(seg.end, next_day)
            start_min = int((start - day).total_seconds() // 60)
            end_min = int((end - day).total_seconds() // 60)
            if end_min <= start_min:
                continue
            totals[seg.status] += end_min - start_min
            if seg.status == DRIVING and seg.minutes:
                miles += (seg.end_mile - seg.start_mile) * (end_min - start_min) / seg.minutes

            # A restart clears the cycle once it completes.
            if seg.kind == "restart" and seg.end <= next_day and seg.end > day:
                cycle_min = -(totals[DRIVING] + totals[ON_DUTY])

            if entries and entries[-1]["status"] == seg.status:
                entries[-1]["end"] = end_min
            else:
                entries.append({"status": seg.status, "start": start_min, "end": end_min})
            if seg.start >= day and seg.kind != "off":
                remarks.append({"minute": start_min, "status": seg.status,
                                "location": seg.location, "note": seg.note})

        on_duty_today = totals[DRIVING] + totals[ON_DUTY]
        cycle_min += on_duty_today
        logs.append({
            "date": day.date().isoformat(),
            "entries": entries,
            "remarks": remarks,
            "totals": {k: round(v / 60, 2) for k, v in totals.items()},
            "total_miles": round(miles),
            "recap": {
                "on_duty_today": round(on_duty_today / 60, 2),
                "cycle_used": round(cycle_min / 60, 2),
                "available_tomorrow": round(max(0, CYCLE_LIMIT_MIN - cycle_min) / 60, 2),
            },
        })
        day = next_day
    return logs
