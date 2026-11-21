#!/usr/bin/env python3
"""Localhost dashboard for Garmin Connect. Run: python dashboard.py -> http://localhost:8787

The front end lives in static/ (index.html, app.css, app.js, vendored Chart.js) and
works fully offline. This file only serves it plus two JSON endpoints.
"""
import contextlib, json, re, time, os, sys, threading
from flask import Flask, jsonify, request, send_from_directory
import alerts, db, garmin_api, learn, metrics, scores, sleep, stress

HERE = os.path.dirname(os.path.abspath(__file__))
STATIC = os.path.join(HERE, "static")
app = Flask(__name__, static_folder=STATIC, static_url_path="/static")

HISTORY = 400  # ~13 months; the client filters by range
# Folder holding collector.py + .env + .tokens.json (defaults to this one).
WHOOP_HOME = os.environ.get("WHOOP_HOME") or HERE
DEMO = "demo" in os.path.basename(db.DB_PATH)


def rows(c, q, *a):
    return [dict(r) for r in c.execute(q, a).fetchall()]


def _has(c, t):
    return c.execute("SELECT name FROM sqlite_master WHERE type='table' AND name=?", (t,)).fetchone() is not None


def _safe(fn, c):
    """A new section failing must not take the whole summary down."""
    try:
        return fn(c)
    except Exception as ex:          # noqa: BLE001
        print(f"{fn.__module__}.{fn.__name__} error:", ex)
        return None


def _clean(s):
    return (s or "").replace("\x00", "").strip()


def _feed(c):
    """Garmin wins the board when any Garmin row is stored. WHOOP rows stay in the tables."""
    for table in ("api_sleep", "api_cycle", "api_workout", "ble_hr", "api_recovery"):
        if c.execute(f"SELECT 1 FROM {table} WHERE source = 'garmin' LIMIT 1").fetchone():
            return "garmin"
    return "whoop"


def _whoop_rows(c):
    n = 0
    for table in ("api_sleep", "api_recovery", "api_cycle", "api_workout", "ble_hr"):
        n += c.execute(
            f"SELECT COUNT(*) n FROM {table} WHERE source IS NULL OR source != 'garmin'"
        ).fetchone()["n"]
    return n


def _archive_span(c, feed):
    clause = "source = 'garmin'" if feed == "garmin" else "(source IS NULL OR source != 'garmin')"
    times = []
    for table, col in (("api_recovery", "created_at"), ("api_sleep", "end"), ("api_cycle", "start")):
        r = c.execute(f"SELECT MIN({col}) a, MAX({col}) b FROM {table} WHERE {clause}").fetchone()
        if r["a"]:
            times.append(r["a"])
        if r["b"]:
            times.append(r["b"])
    if not times:
        return None, None
    return min(times), max(times)


def _zones(raw):
    if not raw:
        return []
    try:
        rows = json.loads(raw)
    except ValueError:
        return []
    return rows if isinstance(rows, list) else []


def _garmin_sync(c):
    raw = db.get_state(c, "garmin_synced_at")
    try:
        return float(raw) if raw else None
    except ValueError:
        return None


def sync_argv():
    """Garmin pull only. This does not start a strap drain."""
    return [sys.executable, os.path.join(HERE, "garmin_api.py"), "sync"]


@app.route("/api/summary")
def summary():
    c = db.connect()
    workouts = rows(c, "SELECT start,end,sport_name,strain,avg_hr,max_hr,kilojoule,distance_m,zone_raw,source "
                       "FROM api_workout ORDER BY start DESC LIMIT ?", HISTORY)
    for w in workouts:
        try:
            w["zones"] = json.loads(w.pop("zone_raw") or "null")
        except ValueError:
            w["zones"] = None
    profile = {r["key"]: r["value"] for r in c.execute("SELECT key,value FROM api_profile")}
    # Newest first by arrival, not by rtc: rtc is the firmware's tick counter and restarts
    # when the strap reboots, so after a reboot the newest lines carry the smallest numbers.
    logs = []
    if _has(c, "device_logs"):
        logs = rows(c, "SELECT rtc,subsystem,message FROM device_logs ORDER BY rowid DESC LIMIT 60")
        for l in logs:
            l["message"] = _clean(l["message"])
    soc = []
    if _has(c, "device_soc"):
        db.add_columns(c, [("device_soc", "seen", "REAL")])      # a database decode.py hasn't touched since
        soc = rows(c, "SELECT rtc,soc_pct FROM device_soc ORDER BY COALESCE(seen, 0), rtc")
    last_ble = db.get_state(c, "last_ble_sync")
    # "Today" = heart-rate samples since local midnight (strap or Garmin). WHOOP and Garmin
    # rows share the tables and are labeled with source so the board can show one feed.
    midnight, tomorrow = metrics.day_bounds(0)
    today_hr = rows(c, "SELECT ts,hr,rr_ms,source FROM ble_hr WHERE ts >= ? AND hr > 0 ORDER BY ts", midnight)
    stats = metrics.day_stats(c, midnight, tomorrow, learn.high_hr(c)) or {}
    feed = _feed(c)
    arch_a, arch_b = _archive_span(c, feed)
    feel = c.execute("SELECT feel FROM daily_night WHERE date=?", (time.strftime("%Y-%m-%d"),)).fetchone()
    return jsonify({
        "today_hr": today_hr,
        # same numbers the Telegram message uses (metrics.py), so the two never disagree
        "today": {k: stats.get(k) for k in ("rmssd", "n_rr", "resting", "high_s", "recorded_s")},
        "battery": metrics.battery(c),
        "pack_battery": metrics.pack_battery(c),
        "personal": dict(learn.summary(c), text=learn.learned_text(c).replace("• ", "").split("\n"),
                         feel=learn.FEEL_WORD.get(feel["feel"]) if feel and feel["feel"] else None),
        "archive": {"start": arch_a, "end": arch_b},
        "feed": feed,
        "whoop_rows": _whoop_rows(c),
        "nights": sleep.recent(c, 30),               # how you slept (sleep.py), newest first
        "profile": {"first_name": profile.get("first_name"),
                    "garmin_name": profile.get("garmin_name"),
                    "garmin_device": profile.get("garmin_device"),
                    "garmin_zones": _zones(profile.get("garmin_zones"))},
        "recovery": rows(c, "SELECT created_at,recovery_score,hrv_rmssd_ms,resting_hr,spo2,skin_temp_c,source "
                             "FROM api_recovery WHERE recovery_score IS NOT NULL OR source = 'garmin' "
                             "ORDER BY created_at DESC LIMIT ?", HISTORY),
        "sleep": rows(c, "SELECT start,end,sleep_perf,sleep_efficiency,sleep_consistency,resp_rate,"
                         "total_in_bed_ms,total_rem_ms,total_sws_ms,total_light_ms,total_awake_ms,disturbances,source "
                         "FROM api_sleep WHERE COALESCE(nap,0)=0 AND total_in_bed_ms IS NOT NULL "
                         "ORDER BY start DESC LIMIT ?", HISTORY),
        "cycle": rows(c, "SELECT start,end,strain,avg_hr,max_hr,kilojoule,source FROM api_cycle "
                         "WHERE strain IS NOT NULL OR source = 'garmin' ORDER BY start DESC LIMIT ?", HISTORY),
        "workout": workouts,
        "metrics": rows(c, "SELECT * FROM garmin_metric ORDER BY date DESC LIMIT ?", HISTORY)
        if _has(c, "garmin_metric") else [],
        # strap estimates of the same scores (scores.py), only the ones that passed their checks
        "estimates": scores.for_app(c),
        # stress monitor (stress.py): today's 5-minute series, the reading now, the baseline used
        "stress": _safe(stress.for_app, c),
        # health alerts that are on (alerts.py): sustained changes against your usual
        "alerts": _safe(alerts.active, c) or [],
        "live_hr": rows(c, "SELECT ts,hr,source FROM ble_hr ORDER BY ts DESC LIMIT 600"),
        "soc": soc,
        "coverage": metrics.coverage_days(c, 7),     # how much of each day has heart rate, and why not
        "logs": logs,
        "sync": {"ble": float(last_ble) if last_ble else None, "api": db.get_state(c, "api_since"),
                 "garmin": _garmin_sync(c)},
        "demo": DEMO,
        "counts": {t: c.execute(f"SELECT COUNT(*) n FROM {t}").fetchone()["n"]
                   for t in ("api_recovery", "api_sleep", "api_cycle", "api_workout", "ble_hr", "ble_raw")},
    })


@app.route("/api/live")
def live():
    c = db.connect()
    r = c.execute("SELECT value FROM sync_state WHERE key='live_hr_now'").fetchone()
    cur = json.loads(r["value"]) if r else {}
    fresh = cur.get("hr") is not None and (time.time() - cur.get("ts", 0) < 8)
    source = "whoop" if fresh else None
    if not fresh:
        g = db.get_state(c, "garmin_hr_now")
        if g:
            try:
                garmin = json.loads(g)
            except ValueError:
                garmin = {}
            # A Connect sample is the latest reading, not a live strap stream.
            if garmin.get("hr") is not None and time.time() - garmin.get("ts", 0) < 6 * 3600:
                cur, fresh, source = garmin, False, "garmin"
    recent = rows(c, "SELECT ts,hr,source FROM ble_hr ORDER BY ts DESC LIMIT 120")
    return jsonify({"live": fresh, "hr": cur.get("hr"), "rr_ms": cur.get("rr_ms", []),
                    "ts": cur.get("ts"), "source": source,
                    "age": round(time.time() - cur.get("ts", 0), 1) if cur.get("ts") else None,
                    "recent": list(reversed(recent))})


# ───────── Resync: pull Garmin into whoop.db. Does not connect to a strap. ─────────
SYNC = {"running": False, "started": None, "finished": None, "ok": None, "log": []}
_sync_lock = threading.Lock()


class _SyncLog:
    """Collect print() lines from garmin_api.sync into the status the board polls."""

    def write(self, text):
        for line in str(text).splitlines():
            line = line.strip()
            if line:
                SYNC["log"] = (SYNC["log"] + [line])[-40:]

    def flush(self):
        return None


def _run_sync():
    """Same function as `python garmin_api.py sync`: 429/500 retry, then 28-day spans."""
    try:
        with contextlib.redirect_stdout(_SyncLog()):
            garmin_api.sync()
        SYNC["ok"] = True
    except Exception as ex:  # noqa: BLE001 - surface anything to the UI
        SYNC["log"] = (SYNC["log"] + [f"error: {ex}"])[-40:]
        SYNC["ok"] = False
    finally:
        SYNC["running"] = False
        SYNC["finished"] = time.time()


_SYNC_STOP = re.compile(r"dailysummaries failed", re.I)


def sync_payload():
    """Status for the board. A daily-summary 500 is a retry, not a stopped sync."""
    log = []
    for line in list(SYNC["log"]):
        if _SYNC_STOP.search(line):
            line = (f"Daily summaries returned an error. Continuing in "
                    f"{garmin_api.DAILY_SPAN}-day spans.")
        if log and log[-1] == line:
            continue
        log.append(line)
    return {"running": SYNC["running"], "started": SYNC["started"],
            "finished": SYNC["finished"], "ok": SYNC["ok"], "log": log[-40:]}


@app.route("/api/sync", methods=["GET", "POST"])
def resync():
    if request.method == "POST":
        # Custom header = browsers must preflight, so other sites can't trigger this.
        if request.headers.get("X-Vitals") != "1":
            return jsonify({"error": "missing header"}), 403
        if DEMO:
            return jsonify({"error": "Demo database: syncing is disabled so real data never lands in it."}), 409
        with _sync_lock:
            if SYNC["running"]:
                return jsonify(sync_payload()), 409
            SYNC.update(running=True, started=time.time(), finished=None, ok=None, log=[])
        threading.Thread(target=_run_sync, daemon=True).start()
    return jsonify(sync_payload())


@app.route("/api/feel", methods=["POST"])
def feel():
    """Record how you feel today (good / ok / rough); learn.py tunes its calls to these answers."""
    if request.headers.get("X-Vitals") != "1":
        return jsonify({"error": "missing header"}), 403
    word = (request.get_json(silent=True) or {}).get("feel")
    if word not in learn.FEEL:
        return jsonify({"error": "feel must be good, ok or rough"}), 400
    if DEMO:
        return jsonify({"error": "Demo database: nothing is recorded."}), 409
    c = db.connect()
    learn.record_feel(c, word)
    return jsonify({"feel": word, "thresholds": learn.thresholds(c)})


@app.route("/static/chart.js")
def chartjs():  # kept for any old bookmarks/pages
    return send_from_directory(STATIC, "chart.umd.js", mimetype="application/javascript")


@app.route("/")
def index():
    return send_from_directory(STATIC, "index.html")


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8787))
    print(f"Dashboard: http://localhost:{port}")
    app.run(host="127.0.0.1", port=port, debug=False)
