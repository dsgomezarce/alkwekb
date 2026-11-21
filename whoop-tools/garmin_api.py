#!/usr/bin/env python3
"""Garmin Connect Personal API → the same SQLite tables the dashboard already reads.

The partner Health API needs a Garmin business key. This client uses the published
Personal API instead: you create a key in Garmin Connect and put it in .env.
Login exchanges that key for a bearer token. Sync copies heart rate, sleep, and
activity into garmin.db with source='garmin', so WHOOP rows stay distinct.

Nothing here talks to a strap. Tokens stay in .garmin_token.json (gitignored).

    python garmin_api.py login
    python garmin_api.py sync                 # last 365 days
    python garmin_api.py sync --days 30      # a shorter window
"""
import os, sys, json, time, hashlib, base64, calendar
import requests
import config, db

SOURCE = "garmin"
BASE = "https://personal.connectapi.garmin.com"
TOKEN_PATH = "/personalapitoken-service/personal-api/personal-api-key/token"
KEY_ENV = "GARMIN_PERSONAL_API_KEY"
BASE_ENV = "GARMIN_PERSONAL_API_BASE_URL"
def home():
    """Folder that holds .env. WHOOP_HOME overrides the directory of these scripts."""
    return os.environ.get("WHOOP_HOME") or config.HERE


def token_file():
    return os.path.join(home(), ".garmin_token.json")
KEY_URL = "https://connect.garmin.com/app/settings/personalApiToken"

# Paths published by Garmin's Personal API client. Dates are YYYY-MM-DD.
HEART_PATH = "/wellness-service/wellness/dailyHeartRate"
SLEEP_PATH = "/sleep-service/sleep/dailySleep"
SLEEP_SUMMARIES_PATH = "/sleep-service/sleep/dailySleepSummariesByDate"
ACTIVITIES_PATH = "/activitylist-service/activities/search/activities"
DAILIES_PATH = "/usersummary-service/v2/dailysummaries"
HRV_PATH = "/hrv-service/hrv"  # + /{date}
PROFILE_PATH = "/userprofile-service/userprofile/settings"
# Wellness activities carry deviceMetaData.deviceName, the watch that recorded the day.
# The published spec has no separate "my devices" list.
WELLNESS_ACTIVITY_PATH = "/wellnessactivity-service/activity/summary/range"
WELLNESS_SPAN = 28

DEFAULT_DAYS = 365
OVERLAP_DAYS = 2
MAX_DAYS = 366
# A year-long daily summary is one call. If that call 500s, walk the year in
# smaller spans so the days that succeed are kept.
DAILY_SPAN = 28
ACTIVITY_PAGE = 100
KCAL_TO_KJ = 4.184
# Honor Retry-After when Garmin sends one; otherwise wait 1, 2, 4, 8, 16 seconds.
# 429 and 500 use the same waits. A 500 is often a transient on a long range.
RETRY_WAITS = (1, 2, 4, 8, 16)
RETRY_STATUSES = (429, 500)
MAX_RETRY_AFTER = 60
RETRY_KEY = "garmin_retry"
# A short pause between days so a 90-day pull does not immediately trip the limit.
DAY_PACE = 0.2


class GarminError(RuntimeError):
    pass


def api_key(env=None):
    """The Personal API key from the environment or .env. Never printed."""
    e = env if env is not None else config.env(os.path.join(home(), ".env"))
    key = (os.environ.get(KEY_ENV) or e.get(KEY_ENV) or "").strip()
    if not key or key == "your_personal_api_key_here":
        raise GarminError(
            f"Set {KEY_ENV} in whoop-tools/.env. Create the key while logged in "
            f"at {KEY_URL} — it is shown once. The partner Health API is not used.")
    return key


def base_url(env=None):
    e = env if env is not None else config.env(os.path.join(home(), ".env"))
    return (os.environ.get(BASE_ENV) or e.get(BASE_ENV) or BASE).rstrip("/")


def _b64url(segment):
    pad = "=" * (-len(segment) % 4)
    return base64.urlsafe_b64decode(segment + pad)


def jwt_exp(token):
    """Read the exp claim. The signature is not checked; Garmin issued the token."""
    try:
        payload = json.loads(_b64url(token.split(".")[1]))
        return float(payload["exp"])
    except (IndexError, KeyError, TypeError, ValueError) as ex:
        raise GarminError("The token endpoint did not return a JWT with an exp claim.") from ex


def _fingerprint(base, key):
    return hashlib.sha256(f"{base}\0{key}".encode()).hexdigest()


def _retry_after(response):
    """Seconds to wait, from a Retry-After header. None when Garmin did not send one."""
    headers = getattr(response, "headers", None) or {}
    raw = headers.get("Retry-After") if hasattr(headers, "get") else None
    if raw is None or raw == "":
        return None
    try:
        return min(MAX_RETRY_AFTER, max(0.0, float(raw)))
    except (TypeError, ValueError):
        return None


def _shift_day(day, delta):
    t = calendar.timegm(time.strptime(day, "%Y-%m-%d")) + delta * 86400
    return time.strftime("%Y-%m-%d", time.gmtime(t))


def _load_retry(c):
    raw = db.get_state(c, RETRY_KEY)
    if not raw:
        return []
    try:
        days = json.loads(raw)
    except ValueError:
        return []
    return sorted({d for d in days if isinstance(d, str) and len(d) == 10})


class GarminClient:
    """Bearer-token client for https://personal.connectapi.garmin.com.

    `session` is a requests-like object (post/get) so tests can stand in for the network.
    """

    def __init__(self, api_key, base=BASE, session=None, token_path=None, now=None, sleep=None):
        self.api_key = api_key
        self.base = base.rstrip("/")
        self.session = session or requests.Session()
        self.token_path = token_path or token_file()
        self.now = now or time.time
        self.sleep = sleep or time.sleep
        self._token = None
        self._exp = 0

    def obtain_token(self, force=False):
        fp = _fingerprint(self.base, self.api_key)
        if not force:
            cached = self._read_cache(fp)
            if cached and cached[1] - self.now() > 3600:
                self._token, self._exp = cached
                return self._token
        url = self.base + TOKEN_PATH
        response = self.session.post(
            url, headers={"Accept": "*/*", "Authorization": f"ApiKey {self.api_key}"}, timeout=30)
        if response.status_code >= 400:
            raise GarminError(f"Garmin token request failed ({response.status_code}). "
                              "Check the key in .env. The key itself is not logged.")
        token = (response.text or "").strip().strip('"')
        if not token:
            raise GarminError("Garmin token request returned an empty body.")
        exp = jwt_exp(token)
        self._token, self._exp = token, exp
        self._write_cache(fp, token, exp)
        return token

    def get(self, path, params=None):
        """GET a published path. 404 and an empty body mean that day has no data."""
        token = self._token or self.obtain_token()
        url = path if path.startswith("http") else self.base + path
        response = self._get(url, token, params)
        if response.status_code == 401:
            token = self.obtain_token(force=True)
            response = self._get(url, token, params)
        if response.status_code == 404 or response.status_code == 204:
            return None
        attempt = 0
        while response.status_code in RETRY_STATUSES and attempt < len(RETRY_WAITS):
            wait = _retry_after(response)
            if wait is None:
                wait = RETRY_WAITS[attempt]
            self.sleep(wait)
            response = self._get(url, token, params)
            attempt += 1
        if response.status_code >= 400:
            raise GarminError(f"GET {path} failed ({response.status_code}).")
        text = (response.text or "").strip()
        if not text:
            return None
        try:
            return response.json()
        except ValueError as ex:
            raise GarminError(f"GET {path} did not return JSON.") from ex

    def _get(self, url, token, params):
        return self.session.get(
            url, headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
            params=params, timeout=30)

    def _read_cache(self, fp):
        try:
            with open(self.token_path) as fh:
                cache = json.load(fh)
        except (OSError, ValueError):
            return None
        if cache.get("fingerprint") != fp:
            return None
        try:
            return cache["access_token"], float(cache["expires_at"])
        except (KeyError, TypeError, ValueError):
            return None

    def _write_cache(self, fp, token, exp):
        parent = os.path.dirname(self.token_path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        tmp = self.token_path + ".tmp"
        body = json.dumps({"fingerprint": fp, "access_token": token, "expires_at": exp})
        with os.fdopen(os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w") as f:
            f.write(body)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, self.token_path)


def gmt_iso(value):
    """Garmin GMT timestamps (ms, seconds, or 'YYYY-MM-DD HH:MM:SS') → ISO-8601 Z."""
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        sec = float(value) / 1000.0 if float(value) > 10_000_000_000 else float(value)
        return time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime(sec))
    text = str(value).strip().replace(" ", "T")
    if text.endswith("Z"):
        return text if "." in text else text[:-1] + ".000Z"
    for fmt in ("%Y-%m-%dT%H:%M:%S.%f", "%Y-%m-%dT%H:%M:%S"):
        try:
            parsed = time.strptime(text[:26], fmt)
        except ValueError:
            continue
        return time.strftime("%Y-%m-%dT%H:%M:%S.000Z", parsed)
    return None


def _add_seconds(iso, seconds):
    if not iso or seconds is None:
        return None
    try:
        t = calendar.timegm(time.strptime(iso[:19], "%Y-%m-%dT%H:%M:%S"))
    except ValueError:
        return None
    return time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime(t + float(seconds)))


def day_id(day):
    """Negative YYYYMMDD so a Garmin day cannot collide with a WHOOP cycle id."""
    return -int(day.replace("-", ""))


def sleep_dto(payload):
    """The night summary. dailySleep wraps it; some calls return the DTO itself."""
    if not isinstance(payload, dict):
        return None
    dto = payload.get("dailySleepDTO")
    if isinstance(dto, dict):
        return dto
    if payload.get("calendarDate") or payload.get("sleepTimeSeconds") is not None:
        return payload
    return None


def iter_days(start, end):
    t = calendar.timegm(time.strptime(start, "%Y-%m-%d"))
    last = calendar.timegm(time.strptime(end, "%Y-%m-%d"))
    while t <= last:
        yield time.strftime("%Y-%m-%d", time.gmtime(t))
        t += 86400


def _num(value):
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _ms(seconds):
    n = _num(seconds)
    return None if n is None else int(round(n * 1000))


def display_name(settings):
    if not isinstance(settings, dict):
        return None
    for key in ("displayName", "fullName", "userName", "firstName"):
        val = settings.get(key)
        if isinstance(val, str) and val.strip():
            return val.strip()
    user = settings.get("userData")
    if isinstance(user, dict):
        for key in ("firstName", "displayName", "fullName"):
            val = user.get(key)
            if isinstance(val, str) and val.strip():
                return val.strip()
    return None


def hr_samples(payload):
    """dailyHeartRate heartRateValues, using the descriptor order when Garmin sends one."""
    if not isinstance(payload, dict):
        return []
    values = payload.get("heartRateValues") or []
    ts_i, hr_i = 0, 1
    for desc in payload.get("heartRateValueDescriptors") or []:
        if not isinstance(desc, dict) or not isinstance(desc.get("index"), int):
            continue
        key = str(desc.get("key") or "").lower().replace("_", "")
        if key in ("timestamp", "time"):
            ts_i = desc["index"]
        elif key in ("heartrate", "hr"):
            hr_i = desc["index"]
    out = []
    for row in values:
        if isinstance(row, dict):
            ts, hr = row.get("timestamp"), row.get("heartRate", row.get("heartrate"))
        elif isinstance(row, (list, tuple)) and len(row) > max(ts_i, hr_i):
            ts, hr = row[ts_i], row[hr_i]
        else:
            continue
        ts_n, hr_n = _num(ts), _num(hr)
        if ts_n is None or hr_n is None or hr_n <= 0:
            continue
        if ts_n > 10_000_000_000:
            ts_n /= 1000.0
        # Milliseconds, so the same sample hits the same REAL primary key next sync.
        out.append((round(ts_n, 3), int(hr_n)))
    return out


def hrv_avg(payload):
    if not isinstance(payload, dict):
        return None
    summary = payload.get("hrvSummary")
    if not isinstance(summary, dict):
        summary = payload
    return _num(summary.get("lastNightAvg"))


def _as_list(payload, *keys):
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        for key in keys:
            if isinstance(payload.get(key), list):
                return payload[key]
        if payload.get("calendarDate"):
            return [payload]
    return []


def put_hr(c, ts, hr):
    """Store one Garmin sample.

    Returns "new" when the row is inserted, "kept" when that timestamp was already
    Garmin (the bpm is refreshed, the row is not duplicated), or "blocked" when a
    non-Garmin row already owns the timestamp.
    """
    ts = round(float(ts), 3)
    row = c.execute(
        "SELECT ts, source, hr FROM ble_hr WHERE ts >= ? AND ts < ? ORDER BY ABS(ts - ?) LIMIT 1",
        (ts - 0.0005, ts + 0.0005, ts)).fetchone()
    if row is not None and (row["source"] or "whoop") != SOURCE:
        return "blocked"
    if row is not None:
        if int(row["hr"]) != int(hr) or (row["source"] or "") != SOURCE:
            c.execute("UPDATE ble_hr SET hr=?, source=? WHERE ts=?", (int(hr), SOURCE, row["ts"]))
        return "kept"
    db.upsert(c, "ble_hr", {"ts": ts, "hr": int(hr), "rr_ms": "", "source": SOURCE})
    return "new"


def _slim(payload):
    """Drop the large per-minute arrays before storing raw JSON."""
    if not isinstance(payload, dict):
        return payload
    drop = {"heartRateValues", "sleepMovement", "sleepLevels", "wellnessEpochSPO2DataDTOList",
            "wellnessEpochRespirationDataDTOList", "sleepRestlessMoments", "sleepStress"}
    return {k: v for k, v in payload.items() if k not in drop}


def store_sleep(c, payload):
    dto = sleep_dto(payload)
    if not dto:
        return False
    day = dto.get("calendarDate")
    asleep = _num(dto.get("sleepTimeSeconds")) or 0
    if not day or asleep <= 0:
        return False
    awake = _num(dto.get("awakeSleepSeconds")) or 0
    start = gmt_iso(dto.get("sleepStartTimestampGMT"))
    end = gmt_iso(dto.get("sleepEndTimestampGMT"))
    if not start:
        start = f"{day}T12:00:00"
    if not end:
        end = _add_seconds(gmt_iso(dto.get("sleepStartTimestampGMT")), asleep + awake) or f"{day}T12:00:00"
    scores = dto.get("sleepScores") if isinstance(dto.get("sleepScores"), dict) else {}
    overall = scores.get("overall") if isinstance(scores.get("overall"), dict) else {}
    in_bed = asleep + awake
    db.upsert(c, "api_sleep", {
        "id": f"garmin:{day}",
        "start": start, "end": end,
        "nap": 1 if asleep <= 0 and (_num(dto.get("napTimeSeconds")) or 0) > 0 else 0,
        "resp_rate": _num(dto.get("averageRespirationValue")),
        "sleep_perf": _num(overall.get("value")),
        "sleep_consistency": None,
        "sleep_efficiency": round(100.0 * asleep / in_bed, 1) if in_bed else None,
        "total_in_bed_ms": _ms(in_bed),
        "total_awake_ms": _ms(awake),
        "total_light_ms": _ms(dto.get("lightSleepSeconds")),
        "total_sws_ms": _ms(dto.get("deepSleepSeconds")),
        "total_rem_ms": _ms(dto.get("remSleepSeconds")),
        "disturbances": int(dto["awakeCount"]) if isinstance(dto.get("awakeCount"), int) else None,
        "raw": json.dumps(_slim(dto)),
        "source": SOURCE,
    })
    return True


def store_heart(c, payload):
    """Returns (written, kept, samples).

    written is a new row. kept is a sample that was already stored, or a timestamp
    owned by another source. Already-stored Garmin samples stay in `samples` so the
    day's average still has them.
    """
    written, kept, samples = 0, 0, []
    for ts, hr in hr_samples(payload):
        status = put_hr(c, ts, hr)
        if status == "new":
            written += 1
            samples.append((ts, hr))
        else:
            kept += 1
            if status == "kept":
                samples.append((ts, hr))
    return written, kept, samples


def store_day(c, day, summary, hrv, spo2, hrs):
    """One Garmin day in api_cycle (activity) and api_recovery (resting HR + overnight HRV)."""
    summary = summary if isinstance(summary, dict) else {}
    if summary.get("calendarDate") and summary.get("calendarDate") != day:
        return False
    kcal = _num(summary.get("activeKilocalories"))
    if kcal is None:
        kcal = _num(summary.get("totalKilocalories"))
    max_hr = _num(summary.get("maxHeartRate"))
    if max_hr is None and hrs:
        max_hr = max(hrs)
    avg_hr = round(sum(hrs) / len(hrs), 1) if hrs else None
    resting = _num(summary.get("restingHeartRate"))
    if not summary and hrv is None and not hrs and resting is None:
        return False
    ident = day_id(day)
    noon = f"{day}T12:00:00"
    db.upsert(c, "api_cycle", {
        "id": ident, "start": noon, "end": f"{day}T23:00:00",
        "score_state": "GARMIN", "strain": None, "avg_hr": avg_hr, "max_hr": max_hr,
        "kilojoule": None if kcal is None else round(kcal * KCAL_TO_KJ, 1),
        "raw": json.dumps(_slim(summary)) if summary else None,
        "source": SOURCE,
    })
    if resting is not None or hrv is not None or spo2 is not None:
        db.upsert(c, "api_recovery", {
            "cycle_id": ident, "sleep_id": f"garmin:{day}",
            "created_at": noon, "score_state": "GARMIN",
            "recovery_score": None, "resting_hr": resting, "hrv_rmssd_ms": hrv,
            "spo2": spo2, "skin_temp_c": None,
            "raw": json.dumps({"calendarDate": day, "restingHeartRate": resting,
                               "lastNightAvg": hrv, "source": SOURCE}),
            "source": SOURCE,
        })
    return True


def store_activity(c, activity):
    if not isinstance(activity, dict) or activity.get("activityId") is None:
        return False
    start = gmt_iso(activity.get("startTimeGMT") or activity.get("startTimeLocal"))
    if not start:
        return False
    duration = _num(activity.get("duration"))
    if duration is None:
        duration = _num(activity.get("elapsedDuration"))
    kind = activity.get("activityType") if isinstance(activity.get("activityType"), dict) else {}
    sport = kind.get("typeKey") or activity.get("activityName") or "activity"
    kcal = _num(activity.get("calories"))
    db.upsert(c, "api_workout", {
        "id": f"garmin:{activity['activityId']}",
        "start": start, "end": _add_seconds(start, duration),
        "sport_name": str(sport), "score_state": "GARMIN",
        "strain": None,
        "avg_hr": _num(activity.get("averageHR")),
        "max_hr": _num(activity.get("maxHR")),
        "kilojoule": None if kcal is None else round(kcal * KCAL_TO_KJ, 1),
        "distance_m": _num(activity.get("distance")),
        "zone_raw": None,
        "raw": json.dumps(activity),
        "source": SOURCE,
    })
    return True


def device_name(summaries):
    """Display name of the device that recorded the latest wellness activity.

    Published on WellnessActivitySummaryDTO.deviceMetaData.deviceName
    (GET /wellnessactivity-service/activity/summary/range/{from}/{until}).
    """
    best_name, best_day = None, ""
    rows = summaries if isinstance(summaries, list) else []
    for row in rows:
        if not isinstance(row, dict):
            continue
        meta = row.get("deviceMetaData")
        if not isinstance(meta, dict):
            continue
        name = meta.get("deviceName")
        if not isinstance(name, str) or not name.strip():
            continue
        day = str(row.get("calendarDate") or "")
        if best_name is None or day >= best_day:
            best_name, best_day = name.strip(), day
    return best_name


def store_device(c, name):
    if name:
        db.upsert(c, "api_profile", {"key": "garmin_device", "value": name})


def store_profile(c, settings):
    name = display_name(settings)
    if not name:
        return
    db.upsert(c, "api_profile", {"key": "garmin_name", "value": name})
    have = c.execute("SELECT value FROM api_profile WHERE key='first_name'").fetchone()
    if have is None:
        db.upsert(c, "api_profile", {"key": "first_name", "value": name.split()[0]})


def _summary_by_day(payload):
    out = {}
    for row in _as_list(payload):
        if isinstance(row, dict) and row.get("calendarDate"):
            out[row["calendarDate"]] = row
    return out


def _spo2(dto):
    if not isinstance(dto, dict):
        return None
    return _num(dto.get("avgSpO2") if dto.get("avgSpO2") is not None else dto.get("averageSpO2"))


class Sync:
    def __init__(self, client, conn, today=None, pace=0):
        self.client = client
        self.c = conn
        self.today = today or time.strftime("%Y-%m-%d", time.localtime())
        self.pace = pace

    def window(self, days):
        """Inclusive start and end. No --days means at least the last 365 days."""
        explicit = days is not None
        if not explicit:
            days = DEFAULT_DAYS
        days = max(1, min(int(days), MAX_DAYS))
        start = _shift_day(self.today, -(days - 1))
        if not explicit:
            since = db.get_state(self.c, "garmin_since")
            if since:
                earlier = _shift_day(since, -OVERLAP_DAYS)
                if earlier < start:
                    start = earlier
        if start > self.today:
            start = self.today
        retry = [d for d in _load_retry(self.c) if d <= self.today]
        if retry:
            start = min(start, retry[0])
        return start, self.today

    def run(self, days=None):
        start, end = self.window(days)
        self.client.obtain_token()
        try:
            store_profile(self.c, self.client.get(PROFILE_PATH))
        except GarminError as ex:
            print("profile:", ex)
        dailies, summary_holes = self._dailies(start, end)
        activities = self._activities(start, end)
        counts = {"heart": 0, "heart_kept": 0, "sleep": 0, "days": 0, "workout": 0, "skipped": 0}
        latest = None
        failed = []
        hole = set(summary_holes)
        for day in iter_days(start, end):
            heart, heart_ok = self._get(HEART_PATH, {"date": day}, "heart")
            sleep, sleep_ok = self._get(SLEEP_PATH, {"date": day}, "sleep")
            hrv, hrv_ok = self._get(f"{HRV_PATH}/{day}", None, "hrv")
            written, kept, samples = store_heart(self.c, heart or {})
            counts["heart"] += written
            counts["heart_kept"] += kept
            if store_sleep(self.c, sleep or {}):
                counts["sleep"] += 1
            if samples:
                last = max(samples)
                if latest is None or last[0] > latest[0]:
                    latest = last
            hrs = [hr for _, hr in samples]
            if store_day(self.c, day, dailies.get(day), hrv_avg(hrv), _spo2(sleep_dto(sleep or {})), hrs):
                counts["days"] += 1
            if day in hole or not (heart_ok and sleep_ok and hrv_ok):
                failed.append(day)
            # A later 429 must not throw away the days that already landed.
            self.c.commit()
            if self.pace:
                self.client.sleep(self.pace)
        for activity in activities:
            if store_activity(self.c, activity):
                counts["workout"] += 1
        worn = self._device(start, end)
        if worn:
            store_device(self.c, worn)
        counts["device"] = worn
        if latest:
            db.set_state(self.c, "garmin_hr_now", json.dumps(
                {"hr": latest[1], "ts": latest[0], "source": SOURCE}))
        if failed:
            # Leave the watermark on the hole so the next sync asks for it again.
            db.set_state(self.c, "garmin_since", _shift_day(min(failed), -1))
            db.set_state(self.c, RETRY_KEY, json.dumps(failed))
            print("will retry:", ", ".join(failed))
        else:
            db.set_state(self.c, "garmin_since", end)
            db.set_state(self.c, RETRY_KEY, "[]")
        db.set_state(self.c, "garmin_synced_at", str(time.time()))
        counts["skipped"] = len(failed)
        self.c.commit()
        return counts, start, end

    def _get(self, path, params, label):
        """Returns (payload, ok). ok is False when the request failed, including a 429."""
        try:
            return self.client.get(path, params), True
        except GarminError as ex:
            day = ""
            if isinstance(params, dict) and params.get("date"):
                day = " " + str(params["date"])
            elif isinstance(path, str) and path.startswith(HRV_PATH + "/"):
                day = " " + path.rsplit("/", 1)[-1]
            print(f"{label}{day}:", ex)
            return None, False

    def _dailies(self, start, end):
        """Daily summaries for the window.

        A 500 on the whole range is retried like a 429. If it still fails, the
        year is walked in smaller spans. Days that never come back are holes
        for the next sync. Heart rate, sleep, and activities still run.
        """
        payload, ok = self._get(
            DAILIES_PATH, {"startDate": start, "endDate": end}, "daily summaries")
        if ok:
            return _summary_by_day(payload), []
        if len(list(iter_days(start, end))) <= DAILY_SPAN:
            return {}, list(iter_days(start, end))
        merged, holes = {}, []
        span_start = start
        while span_start <= end:
            span_end = min(end, _shift_day(span_start, DAILY_SPAN - 1))
            payload, ok = self._get(
                DAILIES_PATH, {"startDate": span_start, "endDate": span_end}, "daily summaries")
            if ok:
                merged.update(_summary_by_day(payload))
            else:
                holes.extend(iter_days(span_start, span_end))
            span_start = _shift_day(span_end, 1)
        return merged, holes

    def _device(self, start, end):
        """Latest deviceMetaData.deviceName across the window. A miss does not fail the day."""
        rows = []
        span_start = start
        while span_start <= end:
            span_end = min(end, _shift_day(span_start, WELLNESS_SPAN - 1))
            payload, ok = self._get(
                f"{WELLNESS_ACTIVITY_PATH}/{span_start}/{span_end}", None, "device")
            if ok and isinstance(payload, list):
                rows.extend(payload)
            span_start = _shift_day(span_end, 1)
        return device_name(rows)

    def _activities(self, start, end):
        out = []
        start_at = 0
        while start_at < ACTIVITY_PAGE * 20:
            try:
                page = self.client.get(ACTIVITIES_PATH, {
                    "start": start_at, "limit": ACTIVITY_PAGE,
                    "startDate": start, "endDate": end})
            except GarminError as ex:
                print("activities:", ex)
                break
            rows = _as_list(page, "activities", "activityList")
            out.extend(rows)
            if len(rows) < ACTIVITY_PAGE:
                break
            start_at += ACTIVITY_PAGE
        return out


def login():
    client = GarminClient(api_key(), base_url())
    client.obtain_token(force=True)
    settings = client.get(PROFILE_PATH)
    name = display_name(settings) or "Garmin Connect"
    c = db.connect()
    store_profile(c, settings)
    c.commit()
    print(f"Logged in ({name}). Bearer token stored in {client.token_path}.")
    print("Next: python garmin_api.py sync")


def sync(days=None, client=None, conn=None, today=None):
    own_client = client is None
    if own_client:
        client = GarminClient(api_key(), base_url())
    c = conn or db.connect()
    counts, start, end = Sync(client, c, today=today, pace=DAY_PACE).run(days)
    print(f"Garmin sync {start} → {end}: {counts}")
    return counts


def main(argv):
    cmd = argv[1] if len(argv) > 1 else "sync"
    days = None
    if "--days" in argv:
        i = argv.index("--days")
        if i + 1 >= len(argv):
            print("usage: python garmin_api.py sync --days N")
            return 2
        try:
            days = int(argv[i + 1])
        except ValueError:
            print("--days needs an integer")
            return 2
        if days < 1:
            print("--days must be at least 1")
            return 2
    try:
        if cmd == "login":
            login()
        elif cmd == "sync":
            sync(days=days)
        else:
            print("usage: python garmin_api.py login | sync [--days N]")
            return 2
    except GarminError as ex:
        print(ex)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
