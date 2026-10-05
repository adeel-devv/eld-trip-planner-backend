from datetime import datetime, timedelta

from django.test import SimpleTestCase

from . import hos
from .hos import DRIVING, OFF_DUTY, ON_DUTY, SLEEPER, Leg, build_daily_logs, simulate

START = datetime(2026, 1, 5, 8, 0)


def plan(miles_1, miles_2, cycle=0, mph=60):
    legs = [
        Leg(miles_1, miles_1 / mph * 60, "pickup"),
        Leg(miles_2, miles_2 / mph * 60, "dropoff"),
    ]
    return simulate(legs, cycle, START)


def shifts(segments):
    """Split a timeline into shifts separated by 10+ hour rests."""
    out, current = [], []
    for seg in segments:
        if seg.status in (OFF_DUTY, SLEEPER) and seg.minutes >= hos.DAILY_REST_MIN:
            if current:
                out.append(current)
            current = []
        else:
            current.append(seg)
    if current:
        out.append(current)
    return out


class HosRulesTests(SimpleTestCase):
    def assert_compliant(self, segments, cycle=0):
        for a, b in zip(segments, segments[1:]):
            self.assertEqual(a.end, b.start)

        for shift in shifts(segments):
            driving = sum(s.minutes for s in shift if s.status == DRIVING)
            self.assertLessEqual(driving, hos.MAX_DRIVING_MIN)
            last_drive_end = max((s.end for s in shift if s.status == DRIVING), default=None)
            if last_drive_end:
                window = (last_drive_end - shift[0].start).total_seconds() / 60
                self.assertLessEqual(window, hos.MAX_WINDOW_MIN)

        since_break = 0
        cycle_min = cycle * 60
        fuel_miles = 0
        for seg in segments:
            if seg.status == DRIVING:
                self.assertLess(cycle_min, hos.CYCLE_LIMIT_MIN, "drove with no cycle hours left")
                since_break += seg.minutes
                fuel_miles += seg.end_mile - seg.start_mile
                self.assertLessEqual(since_break, hos.BREAK_AFTER_DRIVING_MIN)
                self.assertLessEqual(fuel_miles, hos.FUEL_INTERVAL_MILES + 2)
            elif seg.minutes >= hos.BREAK_MIN:
                since_break = 0
            if seg.kind == "fuel":
                fuel_miles = 0
            if seg.status in (DRIVING, ON_DUTY):
                cycle_min += seg.minutes
            if seg.kind == "restart":
                cycle_min = 0

    def test_short_trip_needs_no_rest(self):
        segments = plan(60, 120)
        self.assertEqual([s.kind for s in segments],
                         ["pre_trip", "drive", "pickup", "drive", "dropoff"])
        self.assert_compliant(segments)

    def test_break_after_eight_hours_driving(self):
        segments = plan(30, 540)  # 9.5 h of driving
        kinds = [s.kind for s in segments]
        self.assertIn("break", kinds)
        self.assertNotIn("rest", kinds)
        self.assert_compliant(segments)

    def test_long_trip_takes_daily_rests_and_fuel(self):
        segments = plan(300, 2400)
        kinds = [s.kind for s in segments]
        self.assertGreaterEqual(kinds.count("rest"), 3)
        self.assertGreaterEqual(kinds.count("fuel"), 2)
        self.assertAlmostEqual(segments[-1].end_mile, 2700, delta=1)
        self.assert_compliant(segments)

    def test_cycle_limit_forces_restart(self):
        segments = plan(200, 1500, cycle=55)
        self.assertIn("restart", [s.kind for s in segments])
        self.assert_compliant(segments, cycle=55)

    def test_exhausted_cycle_starts_with_restart(self):
        segments = plan(50, 50, cycle=70)
        self.assertEqual(segments[0].kind, "restart")
        self.assert_compliant(segments, cycle=0)

    def test_pickup_and_dropoff_take_one_hour(self):
        segments = plan(100, 100)
        for kind in ("pickup", "dropoff"):
            seg = next(s for s in segments if s.kind == kind)
            self.assertEqual(seg.minutes, 60)
            self.assertEqual(seg.status, ON_DUTY)

    def test_many_trips_are_compliant(self):
        for miles_1 in (0, 15, 250, 700):
            for miles_2 in (40, 480, 1100, 2900):
                for cycle in (0, 20, 45, 62, 69.5):
                    for mph in (48, 63):
                        with self.subTest(m1=miles_1, m2=miles_2, cycle=cycle, mph=mph):
                            segments = plan(miles_1, miles_2, cycle, mph)
                            self.assert_compliant(segments, cycle)
                            self.assertAlmostEqual(segments[-1].end_mile, miles_1 + miles_2,
                                                   delta=1)


class DailyLogTests(SimpleTestCase):
    def test_each_log_totals_24_hours(self):
        segments = plan(300, 2400, cycle=30)
        logs = build_daily_logs(segments, 30)
        self.assertGreater(len(logs), 3)
        for log in logs:
            self.assertAlmostEqual(sum(log["totals"].values()), 24, places=1)
            self.assertEqual(log["entries"][0]["start"], 0)
            self.assertEqual(log["entries"][-1]["end"], 1440)
            for a, b in zip(log["entries"], log["entries"][1:]):
                self.assertEqual(a["end"], b["start"])
                self.assertNotEqual(a["status"], b["status"])
        self.assertAlmostEqual(sum(log["total_miles"] for log in logs), 2700, delta=len(logs))

    def test_recap_tracks_cycle(self):
        segments = plan(60, 120, cycle=10)
        logs = build_daily_logs(segments, 10)
        self.assertEqual(len(logs), 1)
        recap = logs[0]["recap"]
        self.assertAlmostEqual(recap["on_duty_today"], 5.25)
        self.assertAlmostEqual(recap["cycle_used"], 15.25)
        self.assertAlmostEqual(recap["available_tomorrow"], 54.75)

    def test_trip_ending_at_midnight_has_no_empty_log(self):
        segments = simulate([Leg(60, 60, "pickup"), Leg(60, 60, "dropoff")], 0,
                            START.replace(hour=19, minute=45))
        self.assertEqual(segments[-1].end, START.replace(hour=0) + timedelta(days=1))
        self.assertEqual(len(build_daily_logs(segments, 0)), 1)


class GazetteerTests(SimpleTestCase):
    def test_suggest_ranks_big_cities_first(self):
        from . import gazetteer

        self.assertEqual(gazetteer.suggest("chic")[0]["label"], "Chicago, IL")
        self.assertEqual(gazetteer.suggest("springfield, mo")[0]["label"], "Springfield, MO")
        self.assertEqual(gazetteer.suggest("saint lou")[0]["label"], "St. Louis, MO")
        self.assertEqual(gazetteer.suggest("x"), [])

    def test_lookup_and_nearest(self):
        from . import gazetteer

        self.assertEqual(gazetteer.lookup("Dallas, Texas")["label"], "Dallas, TX")
        self.assertIsNone(gazetteer.lookup("123 Main St, Dallas"))
        self.assertEqual(gazetteer.nearest(35.2, -101.8), "Amarillo, TX")
        self.assertIsNone(gazetteer.nearest(50, -30))
