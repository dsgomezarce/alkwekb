#!/usr/bin/env python3
"""Garmin Personal API mapping, without the network and without a strap.

Run:  python -m unittest test_garmin -v
"""
import base64, json, os, stat, sys, tempfile, unittest

_TMP = tempfile.mkdtemp(prefix="garmin-feed-test-")
os.environ["WHOOP_DB"] = os.path.join(_TMP, "test.db")
os.environ.pop("GARMIN_PERSONAL_API_KEY", None)

import db, garmin_api, dashboard  # noqa: E402

DAY = "2026-11-19"
TS = 1763510400  # sample instant used in the fixture (seconds)


def _jwt(exp=4_000_000_000):
    def seg(obj):
        raw = base64.urlsafe_b64encode(json.dumps(obj).encode()).rstrip(b"=")
        return raw.decode()
    return seg({"alg": "none"}) + "." + seg({"exp": exp}) + ".sig"


class Resp:
    def __init__(self, status, body, headers=None):
        self.status_code = status
        self._body = body
        self.text = body if isinstance(body, str) else json.dumps(body)
        self.ok = 200 <= status < 300
        self.headers = headers or {}

    def json(self):
        if isinstance(self._body, str):
            return json.loads(self._body)
        return self._body


class FakeSession:
    def __init__(self, routes):
        self.routes = routes
        self.calls = []

    def post(self, url, headers=None, timeout=None):
        self.calls.append(("POST", url, dict(headers or {})))
        return Resp(200, _jwt())

    def get(self, url, headers=None, params=None, timeout=None):
        self.calls.append(("GET", url, dict(params or {}), dict(headers or {})))
        path = url.split("personal.connectapi.garmin.com", 1)[-1]
        if path not in self.routes:
            return Resp(404, "")
        return Resp(200, self.routes[path])


class RateSession(FakeSession):
    """Heart-rate GETs fail with 429 until `fails` runs out."""

    def __init__(self, routes, fails):
        super().__init__(routes)
        self.fails = dict(fails)

    def get(self, url, headers=None, params=None, timeout=None):
        path = url.split("personal.connectapi.garmin.com", 1)[-1]
        left = self.fails.get(path, 0)
        if left:
            self.fails[path] = left - 1
            self.calls.append(("GET", url, dict(params or {}), dict(headers or {})))
            return Resp(429, "")
        return super().get(url, headers=headers, params=params, timeout=timeout)


def fixture_routes():
    heart = {
        "calendarDate": DAY,
        "restingHeartRate": 48,
        "maxHeartRate": 140,
        "heartRateValueDescriptors": [
            {"index": 0, "key": "timestamp"},
            {"index": 1, "key": "heartrate"},
        ],
        "heartRateValues": [[TS * 1000, 62], [(TS + 60) * 1000, 70]],
    }
    sleep = {"dailySleepDTO": {
        "calendarDate": DAY,
        "sleepTimeSeconds": 27000,
        "deepSleepSeconds": 5400,
        "lightSleepSeconds": 15000,
        "remSleepSeconds": 6600,
        "awakeSleepSeconds": 1800,
        "sleepStartTimestampGMT": TS * 1000 - 8 * 3600 * 1000,
        "sleepEndTimestampGMT": TS * 1000 - 3600 * 1000,
        "averageRespirationValue": 14.2,
        "avgSpO2": 96.5,
        "awakeCount": 2,
        "sleepScores": {"overall": {"value": 82, "qualifierKey": "GOOD"}},
    }}
    return {
        garmin_api.PROFILE_PATH: {"displayName": "Diego G", "userData": {"firstName": "Diego"}},
        garmin_api.DAILIES_PATH: [{
            "calendarDate": DAY, "activeKilocalories": 450, "maxHeartRate": 140,
            "restingHeartRate": 48,
        }],
        garmin_api.HEART_PATH: heart,
        garmin_api.SLEEP_PATH: sleep,
        f"{garmin_api.HRV_PATH}/{DAY}": {"hrvSummary": {"calendarDate": DAY, "lastNightAvg": 58}},
        garmin_api.ACTIVITIES_PATH: [{
            "activityId": 999001, "activityName": "Morning Run",
            "startTimeGMT": "2026-11-19 07:30:00", "duration": 1800,
            "activityType": {"typeKey": "running"},
            "averageHR": 145, "maxHR": 168, "calories": 320, "distance": 5000,
        }],
    }


class GarminMapTest(unittest.TestCase):
    def setUp(self):
        self.c = db.connect()
        for table in ("api_sleep", "api_recovery", "api_cycle", "api_workout", "ble_hr",
                      "api_profile", "sync_state"):
            self.c.execute(f"DELETE FROM {table}")
        self.c.commit()
        self.token = os.path.join(_TMP, ".garmin_token.json")
        if os.path.exists(self.token):
            os.remove(self.token)
        self.session = FakeSession(fixture_routes())
        self.client = garmin_api.GarminClient(
            "test-key-not-a-secret", base=garmin_api.BASE, session=self.session,
            token_path=self.token, now=lambda: 1_700_000_000)

    def test_login_uses_the_published_token_endpoint(self):
        self.client.obtain_token()
        method, url, headers = self.session.calls[0]
        self.assertEqual(method, "POST")
        self.assertEqual(url, garmin_api.BASE + garmin_api.TOKEN_PATH)
        self.assertEqual(headers["Authorization"], "ApiKey test-key-not-a-secret")
        self.assertTrue(os.path.exists(self.token))
        self.assertEqual(stat.S_IMODE(os.stat(self.token).st_mode) & 0o077, 0)
        with open(self.token) as fh:
            cached = json.load(fh)
        self.assertNotIn("test-key-not-a-secret", json.dumps(cached))
        self.assertIn("access_token", cached)

    def test_sync_labels_garmin_and_leaves_whoop_rows(self):
        self.c.execute("INSERT INTO ble_hr(ts, hr, rr_ms) VALUES(?, 55, '800')", (float(TS),))
        self.c.execute(
            "INSERT INTO api_sleep(id, start, end, nap, total_in_bed_ms) VALUES(?,?,?,?,?)",
            ("whoop-sleep-1", "2026-11-18T23:00:00.000Z", "2026-11-19T07:00:00.000Z", 0, 8 * 3600 * 1000))
        self.c.commit()

        counts, start, end = garmin_api.Sync(self.client, self.c, today=DAY).run(days=1)
        self.assertEqual((start, end), (DAY, DAY))
        self.assertEqual(counts["sleep"], 1)
        self.assertEqual(counts["workout"], 1)
        self.assertEqual(counts["heart"], 1)          # the other sample belonged to WHOOP
        self.assertEqual(counts["heart_kept"], 1)

        whoop_hr = self.c.execute("SELECT hr, rr_ms, source FROM ble_hr WHERE ts=?", (float(TS),)).fetchone()
        self.assertEqual(whoop_hr["hr"], 55)
        self.assertEqual(whoop_hr["rr_ms"], "800")
        self.assertIsNone(whoop_hr["source"])
        garmin_hr = self.c.execute(
            "SELECT hr, source FROM ble_hr WHERE ts=?", (float(TS + 60),)).fetchone()
        self.assertEqual((garmin_hr["hr"], garmin_hr["source"]), (70, "garmin"))

        whoop_sleep = self.c.execute("SELECT source FROM api_sleep WHERE id='whoop-sleep-1'").fetchone()
        self.assertIsNone(whoop_sleep["source"])
        night = self.c.execute("SELECT * FROM api_sleep WHERE id=?", (f"garmin:{DAY}",)).fetchone()
        self.assertEqual(night["source"], "garmin")
        self.assertEqual(night["sleep_perf"], 82)
        self.assertEqual(night["total_sws_ms"], 5400 * 1000)
        self.assertEqual(night["total_rem_ms"], 6600 * 1000)
        self.assertEqual(night["resp_rate"], 14.2)
        self.assertNotIn("test-key-not-a-secret", night["raw"])

        cycle = self.c.execute("SELECT * FROM api_cycle WHERE source='garmin'").fetchone()
        self.assertEqual(cycle["id"], -20261119)
        self.assertIsNone(cycle["strain"])
        self.assertAlmostEqual(cycle["kilojoule"], 450 * 4.184, places=1)
        self.assertEqual(cycle["max_hr"], 140)

        rec = self.c.execute("SELECT * FROM api_recovery WHERE source='garmin'").fetchone()
        self.assertEqual(rec["resting_hr"], 48)
        self.assertEqual(rec["hrv_rmssd_ms"], 58)
        self.assertEqual(rec["spo2"], 96.5)
        self.assertIsNone(rec["recovery_score"])

        workout = self.c.execute("SELECT * FROM api_workout WHERE source='garmin'").fetchone()
        self.assertEqual(workout["id"], "garmin:999001")
        self.assertEqual(workout["sport_name"], "running")
        self.assertEqual(workout["distance_m"], 5000)
        self.assertAlmostEqual(workout["kilojoule"], 320 * 4.184, places=1)
        self.assertTrue(workout["start"].endswith("Z"))

        paths = [url.split("personal.connectapi.garmin.com", 1)[-1] for _, url, _, _ in
                 [c for c in self.session.calls if c[0] == "GET"]]
        self.assertIn(garmin_api.HEART_PATH, paths)
        self.assertIn(garmin_api.SLEEP_PATH, paths)
        self.assertIn(garmin_api.ACTIVITIES_PATH, paths)
        self.assertNotIn("collector.py", " ".join(paths))

    def test_missing_key_names_the_variable_only(self):
        with self.assertRaises(garmin_api.GarminError) as caught:
            garmin_api.api_key({})
        self.assertIn("GARMIN_PERSONAL_API_KEY", str(caught.exception))
        self.assertNotIn("ApiKey", str(caught.exception))

    def test_dashboard_shows_garmin_and_does_not_drain(self):
        garmin_api.Sync(self.client, self.c, today=DAY).run(days=1)
        argv = dashboard.sync_argv()
        joined = " ".join(argv)
        self.assertTrue(joined.endswith("garmin_api.py sync") or "garmin_api.py" in joined)
        self.assertNotIn("collector.py", joined)
        self.assertNotIn("whoop.py", joined)
        body = dashboard.app.test_client().get("/api/summary").get_json()
        self.assertEqual(body["feed"], "garmin")
        sources = {row["source"] for row in body["sleep"]}
        self.assertIn("garmin", sources)
        self.assertGreaterEqual(body["whoop_rows"], 0)
        garmin_nights = [s for s in body["sleep"] if s["source"] == "garmin"]
        self.assertEqual(garmin_nights[0]["total_rem_ms"], 6600 * 1000)
        refused = dashboard.app.test_client().post("/api/sync")
        self.assertEqual(refused.status_code, 403)
        self.assertNotIn("bleak", sys.modules)
        self.assertNotIn("whoop", sys.modules)
        alarm = dashboard.app.test_client().get("/api/alarm")
        buzz = dashboard.app.test_client().post("/api/buzz", headers={"X-Vitals": "1"})
        self.assertEqual(alarm.status_code, 404)
        self.assertEqual(buzz.status_code, 404)
        self.assertNotIn("bleak", sys.modules)
        page = dashboard.app.test_client().get("/").get_data(as_text=True)
        self.assertIn('title="dsgomezarce"', page)
        self.assertIn(">A</span>", page)
        self.assertNotIn("WHOOP", page)
        self.assertNotIn("smiley.png", page)
        self.assertNotIn("alarm-card", page)
        self.assertNotIn("alkwekb", page.lower())

    def test_second_sync_keeps_heart_samples(self):
        first, _, _ = garmin_api.Sync(self.client, self.c, today=DAY).run(days=1)
        self.assertEqual(first["heart"], 2)
        self.assertEqual(first["heart_kept"], 0)
        stored = self.c.execute("SELECT COUNT(*) n FROM ble_hr").fetchone()["n"]
        self.assertEqual(stored, 2)
        # A new connection sees the same rows: the first sync committed them.
        other = db.connect()
        self.assertEqual(other.execute("SELECT COUNT(*) n FROM ble_hr").fetchone()["n"], 2)
        other.close()
        second, _, _ = garmin_api.Sync(self.client, self.c, today=DAY).run(days=1)
        self.assertEqual(second["heart"], 0)
        self.assertEqual(second["heart_kept"], 2)
        self.assertEqual(second["skipped"], 0)
        again = self.c.execute("SELECT COUNT(*) n FROM ble_hr").fetchone()["n"]
        self.assertEqual(again, stored)
        row = self.c.execute(
            "SELECT hr, source FROM ble_hr WHERE ABS(ts - ?) < 0.001", (float(TS + 60),)).fetchone()
        self.assertEqual((row["hr"], row["source"]), (70, "garmin"))

    def test_rate_limit_retries_and_refills_the_skipped_day(self):
        sleeps = []
        session = RateSession(fixture_routes(), {garmin_api.HEART_PATH: 99})
        client = garmin_api.GarminClient(
            "test-key-not-a-secret", base=garmin_api.BASE, session=session,
            token_path=self.token, now=lambda: 1_700_000_000, sleep=lambda s: sleeps.append(s))
        counts, _, _ = garmin_api.Sync(client, self.c, today=DAY).run(days=1)
        self.assertEqual(counts["heart"], 0)
        self.assertEqual(counts["skipped"], 1)
        self.assertEqual(sleeps, list(garmin_api.RETRY_WAITS))
        self.assertEqual(json.loads(db.get_state(self.c, garmin_api.RETRY_KEY)), [DAY])
        self.assertEqual(db.get_state(self.c, "garmin_since"), "2026-11-18")
        self.assertEqual(self.c.execute("SELECT COUNT(*) n FROM ble_hr").fetchone()["n"], 0)

        session.fails[garmin_api.HEART_PATH] = 0
        sleeps.clear()
        again, _, _ = garmin_api.Sync(client, self.c, today=DAY).run(days=1)
        self.assertEqual(again["heart"], 2)
        self.assertEqual(again["heart_kept"], 0)
        self.assertEqual(again["skipped"], 0)
        self.assertEqual(json.loads(db.get_state(self.c, garmin_api.RETRY_KEY)), [])
        self.assertEqual(db.get_state(self.c, "garmin_since"), DAY)
        self.assertEqual(self.c.execute("SELECT COUNT(*) n FROM ble_hr WHERE source='garmin'").fetchone()["n"], 2)
        self.assertEqual(sleeps, [])


if __name__ == "__main__":
    unittest.main()
