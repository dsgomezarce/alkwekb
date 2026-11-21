/* Vitals — local WHOOP dashboard. Plain JS + vendored Chart.js, no build step. */
(() => {
  "use strict";

  const root = document.documentElement;
  const $ = (s, r = document) => r.querySelector(s);
  const el = (tag, cls, html) => {
    const n = document.createElement(tag);
    if (cls) n.className = cls;
    if (html != null) n.innerHTML = html;
    return n;
  };
  const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const reduceMotion = matchMedia("(prefers-reduced-motion: reduce)");

  /* ───────── Tokens → canvas-safe colors ───────── */
  const probe = document.createElement("canvas").getContext("2d");
  const tok = (name) => {
    probe.fillStyle = "#000";
    probe.fillStyle = getComputedStyle(root).getPropertyValue(name).trim() || "#000";
    return probe.fillStyle;
  };
  const alpha = (color, a) => {
    probe.fillStyle = color;
    const c = probe.fillStyle;
    if (c[0] === "#") {
      const n = parseInt(c.slice(1), 16);
      return `rgba(${(n >> 16) & 255},${(n >> 8) & 255},${n & 255},${a})`;
    }
    const [r, g, b] = c.match(/[\d.]+/g);
    return `rgba(${r},${g},${b},${a})`;
  };

  /* ───────── Formatting ───────── */
  const DAY = 864e5;
  const dShort = new Intl.DateTimeFormat(undefined, { month: "short", day: "numeric" });
  const dLong = new Intl.DateTimeFormat(undefined, { weekday: "short", month: "short", day: "numeric", year: "numeric" });
  const dTime = new Intl.DateTimeFormat(undefined, { hour: "numeric", minute: "2-digit" });
  const rtf = new Intl.RelativeTimeFormat(undefined, { numeric: "auto" });
  const nf = (d) => new Intl.NumberFormat(undefined, { minimumFractionDigits: d, maximumFractionDigits: d });
  const n0 = nf(0), n1 = nf(1);
  const fmt = (v, d = 0) => (v == null || Number.isNaN(v) ? "—" : (d ? n1 : n0).format(v));
  // Value with unit; the unit is dropped when there is no value ("—", not "—%").
  const fmtU = (v, u, d = 0) => (v == null || Number.isNaN(v) ? "—" : `${fmt(v, d)}${u}`);

  function ago(ms) {
    const s = (Date.now() - ms) / 1000;
    const steps = [[60, "second"], [3600, "minute", 60], [86400, "hour", 3600], [86400 * 30, "day", 86400], [86400 * 365, "month", 86400 * 30], [Infinity, "year", 86400 * 365]];
    for (const [lim, unit, div = 1] of steps) if (Math.abs(s) < lim) return rtf.format(-Math.round(s / div), unit);
  }
  const hm = (ms) => {
    if (ms == null) return "—";
    const m = Math.round(ms / 6e4);
    return `${Math.floor(m / 60)}h ${String(m % 60).padStart(2, "0")}m`;
  };
  const durMin = (ms) => {
    const m = Math.round(ms / 6e4);
    return m >= 60 ? (m % 60 ? `${Math.floor(m / 60)}h ${m % 60}m` : `${m / 60}h`) : `${m}m`;
  };
  const day = (iso) => new Date(iso);
  const titleCase = (s) => (s || "Activity").replace(/[-_]/g, " ").replace(/\b\w/g, (c) => c.toUpperCase());

  /* ───────── Stats ───────── */
  const nums = (a) => a.filter((v) => v != null && !Number.isNaN(v));
  const mean = (a) => (a = nums(a)).length ? a.reduce((s, v) => s + v, 0) / a.length : null;
  const sd = (a) => {
    a = nums(a);
    if (a.length < 2) return null;
    const m = mean(a);
    return Math.sqrt(a.reduce((s, v) => s + (v - m) ** 2, 0) / (a.length - 1));
  };
  const rolling = (a, w) => a.map((_, i) => mean(a.slice(Math.max(0, i - w + 1), i + 1)));
  function baseline(series) {
    // Baseline = the 30 readings before the latest one.
    const prior = series.slice(Math.max(0, series.length - 31), series.length - 1);
    const m = mean(prior), s = sd(prior);
    return m == null ? null : { mean: m, sd: s ?? 0, n: nums(prior).length };
  }

  const zoneOf = (s) => (s == null ? "none" : s >= 67 ? "good" : s >= 34 ? "fair" : "low");
  const ZONE_LABEL = { good: "High", fair: "Moderate", low: "Low", none: "No score" };

  /* ───────── Chart.js setup ───────── */
  const charts = new Map();
  function mount(id, cfg) {
    charts.get(id)?.destroy();
    const canvas = document.getElementById(id);
    if (!canvas) return null;
    const c = new Chart(canvas, cfg);
    charts.set(id, c);
    return c;
  }
  function emptyState(canvasId, title, hint) {
    charts.get(canvasId)?.destroy();
    charts.delete(canvasId);
    const host = document.getElementById(canvasId)?.parentElement;
    if (!host) return;
    host.querySelector(".empty")?.remove();
    const canvas = host.querySelector("canvas");
    canvas.hidden = true;
    host.append(el("div", "empty", `<div><strong>${esc(title)}</strong>${esc(hint)}</div>`));
  }
  function clearEmpty(canvasId) {
    const canvas = document.getElementById(canvasId);
    if (!canvas) return;
    canvas.hidden = false;
    canvas.parentElement.querySelector(".empty")?.remove();
  }

  const crosshair = {
    id: "crosshair",
    afterDatasetsDraw(chart) {
      const act = chart.tooltip?.getActiveElements?.();
      if (!act?.length) return;
      const x = act[0].element.x, { top, bottom } = chart.chartArea, ctx = chart.ctx;
      ctx.save();
      ctx.strokeStyle = tok("--border-strong");
      ctx.lineWidth = 1;
      ctx.beginPath(); ctx.moveTo(Math.round(x) + 0.5, top); ctx.lineTo(Math.round(x) + 0.5, bottom); ctx.stroke();
      ctx.restore();
    },
  };
  const band = {
    id: "band",
    beforeDatasetsDraw(chart, _a, o) {
      if (o?.lo == null || o?.hi == null) return;
      const { ctx, chartArea: a, scales: { y } } = chart;
      const top = Math.max(a.top, y.getPixelForValue(o.hi)), bot = Math.min(a.bottom, y.getPixelForValue(o.lo));
      if (bot <= top) return;
      ctx.save();
      ctx.fillStyle = alpha(tok("--text"), 0.06);
      ctx.fillRect(a.left, top, a.right - a.left, bot - top);
      ctx.strokeStyle = alpha(tok("--text"), 0.14);
      ctx.lineWidth = 1;
      ctx.beginPath();
      ctx.moveTo(a.left, Math.round(top) + 0.5); ctx.lineTo(a.right, Math.round(top) + 0.5);
      ctx.moveTo(a.left, Math.round(bot) - 0.5); ctx.lineTo(a.right, Math.round(bot) - 0.5);
      ctx.stroke();
      ctx.restore();
    },
  };

  // Soft phosphor glow for the monitor trace.
  const glow = {
    id: "glow",
    beforeDatasetDraw(chart, args, o) {
      if (!o?.color) return;
      chart.ctx.save();
      chart.ctx.shadowColor = o.color;
      chart.ctx.shadowBlur = o.blur ?? 10;
    },
    afterDatasetDraw(chart, args, o) { if (o?.color) chart.ctx.restore(); },
  };
  // Leading dot + halo on the newest sample (the "sweep head").
  const head = {
    id: "head",
    afterDatasetsDraw(chart, _a, o) {
      if (!o?.color) return;
      const pts = chart.getDatasetMeta(0).data;
      const p = pts[pts.length - 1];
      if (!p) return;
      const ctx = chart.ctx;
      ctx.save();
      ctx.fillStyle = alpha(o.color, 0.18);
      ctx.beginPath(); ctx.arc(p.x, p.y, 9, 0, Math.PI * 2); ctx.fill();
      ctx.shadowColor = o.color; ctx.shadowBlur = 12;
      ctx.fillStyle = o.color;
      ctx.beginPath(); ctx.arc(p.x, p.y, 3.5, 0, Math.PI * 2); ctx.fill();
      ctx.restore();
    },
  };
  // Dashed horizontal reference line (e.g. range average).
  const avgLine = {
    id: "avgLine",
    afterDatasetsDraw(chart, _a, o) {
      if (o?.value == null) return;
      const { ctx, chartArea: a, scales: { y } } = chart;
      const yy = Math.round(y.getPixelForValue(o.value)) + 0.5;
      ctx.save();
      ctx.setLineDash([4, 4]);
      ctx.strokeStyle = alpha(tok("--text"), 0.55);
      ctx.lineWidth = 1;
      ctx.beginPath(); ctx.moveTo(a.left, yy); ctx.lineTo(a.right, yy); ctx.stroke();
      ctx.restore();
    },
  };
  // Vertical gradient for bars: solid at the top, fading toward the baseline.
  // Flat fills: one solid tone per bar, like the rest of the interface.
  const vgrad = (color) => () => alpha(color, 0.85);
  // Monitor colors are theme-independent (bedside display is always dark).
  const MON = { trace: "#3df5b0", dim: "rgba(200,255,232,0.45)", bg: "#07090a" };

  function theme() {
    return {
      text: tok("--text"), text2: tok("--text-2"), text3: tok("--text-3"),
      grid: tok("--grid"), surface: tok("--surface"), surface3: tok("--surface-3"), border: tok("--border-strong"),
    };
  }

  function baseOptions({ y = {}, stacked = false, tooltip = {}, plugins = {}, xTicks = 6 } = {}) {
    const t = theme();
    const font = { family: getComputedStyle(document.body).fontFamily, size: 11 };
    return {
      responsive: true,
      maintainAspectRatio: false,
      animation: reduceMotion.matches ? false : { duration: 500, easing: "easeOutQuart" },
      interaction: { mode: "index", intersect: false },
      layout: { padding: { top: 4 } },
      plugins: {
        legend: { display: false },
        tooltip: {
          backgroundColor: t.surface, borderColor: t.border, borderWidth: 1,
          titleColor: t.text, bodyColor: t.text2, footerColor: t.text,
          titleFont: { ...font, size: 12, weight: "500" }, bodyFont: { ...font, size: 12 }, footerFont: { ...font, size: 12, weight: "500" },
          padding: 10, cornerRadius: 6, caretSize: 0, boxWidth: 8, boxHeight: 8, boxPadding: 4,
          usePointStyle: true, displayColors: true,
          ...tooltip,
        },
        ...plugins,
      },
      scales: {
        x: {
          stacked,
          grid: { display: false },
          border: { color: t.grid },
          ticks: { color: t.text3, font, maxTicksLimit: xTicks, maxRotation: 0, autoSkipPadding: 16 },
        },
        y: {
          stacked,
          position: "right",
          grid: { color: t.grid, drawTicks: false },
          border: { display: false },
          ticks: { color: t.text3, font, padding: 8, maxTicksLimit: 5 },
          ...y,
        },
      },
    };
  }

  /* ───────── State ───────── */
  let DATA = null;
  let RANGE = 30;
  let woExpanded = false;

  // The window ends at the chosen day (not at the newest row), so a 2026 day never shows 2025 as "the last 30 days".
  const inRange = (rows, key) => {
    if (!rows.length) return rows;
    const last = ADATE ? +fromYmd(ADATE) + DAY : Math.max(...rows.map((r) => +day(r[key])));
    const cut = last - RANGE * DAY;
    return rows.filter((r) => +day(r[key]) > cut);
  };

  /* ───────── Theme toggle ───────── */
  const themeBtn = $("#theme-toggle");
  const currentTheme = () => root.dataset.theme || (matchMedia("(prefers-color-scheme: light)").matches ? "light" : "dark");
  function syncThemeButton() {
    const label = currentTheme() === "dark" ? "Switch to light theme" : "Switch to dark theme";
    for (const b of document.querySelectorAll("#theme-toggle, #hv-theme, .side__theme")) b.setAttribute("aria-label", label);
  }
  if (!root.dataset.theme && matchMedia("(prefers-color-scheme: light)").matches) root.dataset.theme = "light";
  syncThemeButton();
  themeBtn.addEventListener("click", () => {
    const next = currentTheme() === "dark" ? "light" : "dark";
    // Swap the theme with every transition off, so colors don't all animate at once and smear.
    const off = document.createElement("style");
    off.textContent = "*,*::before,*::after{transition:none!important}";
    document.head.append(off);
    root.dataset.theme = next;
    getComputedStyle(root).color; // flush styles before transitions come back
    requestAnimationFrame(() => requestAnimationFrame(() => off.remove()));
    try { localStorage.setItem("vitals-theme", next); } catch { /* private mode */ }
    syncThemeButton();
    if (DATA) renderAll();
    renderLive(lastLive);
    dispatchEvent(new CustomEvent("vitals:theme"));
  });

  /* ───────── Top bar / header / sidebar ───────── */
  const dToday = new Intl.DateTimeFormat(undefined, { weekday: "long", month: "long", day: "numeric" });
  function renderHeader(d) {
    const name = d.profile?.garmin_name || d.profile?.first_name;
    const h = new Date().getHours();
    const part = h < 5 ? "evening" : h < 12 ? "morning" : h < 18 ? "afternoon" : "evening";
    $("#greeting").textContent = name ? `Good ${part}, ${name}` : `Good ${part}`;
    $("#today").textContent = dToday.format(new Date());
    $("#patient-name").textContent = name || "My vitals";
    $("#avatar").textContent = (name || "V").slice(0, 1).toUpperCase();

    const parts = [];
    if (d.sync?.garmin) parts.push(`Garmin ${ago(d.sync.garmin * 1000)}`);

    if (d.cloud && d.sync?.cloud) parts.push(`Mac pushed ${ago(d.sync.cloud * 1000)}`);
    document.querySelectorAll("[data-cloud-only]").forEach((n) => { n.hidden = !d.cloud; });
    $("#sync-text").textContent = parts.join(" · ") || "Not synced yet";
    const chip = $("#sync-status");
    const syncedAt = d.sync?.garmin;
    chip.toggleAttribute("data-stale", !syncedAt || Date.now() - syncedAt * 1000 > 6 * 36e5);
    chip.title = "Last Garmin sync";
    const bleLabel = document.getElementById("side-ble-label");
    if (bleLabel) bleLabel.textContent = "Garmin";
    $("#side-device").textContent = "Garmin";
    $("#side-ble").textContent = d.sync?.garmin ? ago(d.sync.garmin * 1000) : "Not synced";
    $("#side-bat").textContent = "Garmin";
    $("#side-api").textContent = d.archive?.end ? `to ${dFull.format(+day(d.archive.end))}` : "None";
    const kept = document.getElementById("side-kept");
    if (kept) kept.textContent = "";
    const mark = document.querySelector(".brand");
    if (mark) { mark.title = "dsgomezarce"; mark.setAttribute("aria-label", "dsgomezarce"); }
    const brandName = document.querySelector(".brand__name");
    if (brandName) brandName.textContent = "dsgomezarce";
    const worn = d.profile?.garmin_device;
    const meta = document.getElementById("device-meta");
    if (meta) meta.textContent = worn || "Garmin Connect";
    if (worn) $("#side-device").textContent = worn;
  }

  /* ───────── Reference range (lab-report style) ───────── */
  // Track = min..max of the last 30 readings; band = usual range (mean ± 1 SD); dot = latest.
  function refBar(series, latest, colorVar, d = 0) {
    const win = nums(series.slice(-31));
    if (win.length < 3 || latest == null) return "";
    const b = baseline(series);
    let lo = Math.min(...win), hi = Math.max(...win);
    if (hi === lo) { lo -= 1; hi += 1; }
    const pos = (x) => Math.min(100, Math.max(0, ((x - lo) / (hi - lo)) * 100));
    const bl = b ? pos(b.mean - b.sd) : 0, bh = b ? pos(b.mean + b.sd) : 0;
    return `<div class="ref" style="--c:var(${colorVar})" aria-hidden="true">
        ${b ? `<span class="ref__band" style="left:${bl}%;width:${Math.max(2, bh - bl)}%"></span>` : ""}
        <span class="ref__mark" style="left:${pos(latest)}%"></span>
      </div>
      <div class="ref__ends" aria-hidden="true"><span>${fmt(lo, d)}</span><span>30-day range</span><span>${fmt(hi, d)}</span></div>`;
  }
  const zScore = (series) => {
    const latest = series.at(-1), b = baseline(series);
    if (!b || latest == null || !b.sd) return null;
    return { z: (latest - b.mean) / b.sd, pct: ((latest - b.mean) / b.mean) * 100, mean: b.mean };
  };

  /* ───────── Recovery hero ───────── */
  function dayMetric(d) {
    return ofDay((d.metrics || [])[0], "at");
  }
  function renderRecovery(d) {
    const m = dayMetric(d);
    const score = m?.body_battery;
    const ring = $("#rec-ring"), status = $("#rec-status"), card = $(".card--readiness");
    const z = zoneOf(score);
    ring.dataset.zone = z;
    status.dataset.zone = z;
    card.dataset.zone = z;
    $("#rec-value").textContent = fmt(score, 0);
    const unit = $(".ring__unit", ring);
    if (unit) unit.textContent = "";
    const arcs = [$(".ring__value", ring), $(".ring__glow", ring)];
    arcs.forEach((a) => a.setAttribute("stroke-dasharray", "0 100"));
    requestAnimationFrame(() => requestAnimationFrame(() =>
      arcs.forEach((a) => a.setAttribute("stroke-dasharray", `${Math.max(0.5, score ?? 0)} 100`))));
    $(".status__text", status).textContent = score == null ? "No reading" : ZONE_LABEL[z];
    $("#rec-date").textContent = score == null
      ? "No Body Battery for this day"
      : `Body Battery ${dLong.format(+day(m.at))}`;
    ring.setAttribute("role", "img");
    ring.setAttribute("aria-label", score == null ? "No Body Battery" : `Body Battery ${fmt(score, 0)}`);

    const r = ofDay(d.recovery[0], "created_at");
    if (!r && score == null) { $("#rec-drivers").innerHTML = ""; $("#rec-insight").innerHTML = ""; return; }
    const rec = d.recovery.slice().reverse(), slp = d.sleep.slice().reverse();
    const DRIVERS = [
      { name: "Heart rate variability", series: rec.map((x) => x.hrv_rmssd_ms), unit: "ms", color: "--m-hrv" },
      { name: "Resting heart rate", series: rec.map((x) => x.resting_hr), unit: "bpm", color: "--m-rhr" },
      { name: "Sleep performance", series: slp.map((x) => x.sleep_perf), unit: "%", color: "--stage-rem" },
    ];
    $("#rec-drivers").innerHTML = DRIVERS.map((m) => `
      <li>
        <div class="driver__top"><span class="driver__name">${m.name}</span><span class="driver__val">${fmt(m.series.at(-1))}<small>${m.unit}</small></span></div>
        ${refBar(m.series, m.series.at(-1), m.color)}
      </li>`).join("");

    // One factual sentence. Describes the data; doesn't prescribe.
    const hrv = zScore(DRIVERS[0].series), rhr = zScore(DRIVERS[1].series);
    const say = (s, label, inv) => {
      if (!s) return null;
      const dir = s.pct >= 0 ? "above" : "below";
      if (Math.abs(s.z) <= 1) return `${label} is within your usual range`;
      const flag = inv ? s.z > 1 : s.z < -1;
      return `${label} is <strong>${n0.format(Math.abs(s.pct))}% ${dir}</strong> your baseline${flag ? "" : ""}`;
    };
    const bits = [say(hrv, "HRV"), say(rhr, "resting heart rate", true)].filter(Boolean);
    const batteryNote = score == null ? "" : `Body Battery ${fmt(score, 0)}`
      + (m.bb_charged != null ? `, charged ${fmt(m.bb_charged, 0)}` : "")
      + (m.bb_drained != null ? `, drained ${fmt(m.bb_drained, 0)}` : "");
    $("#rec-insight").innerHTML = [batteryNote, bits.join(", and ")].filter(Boolean).join(". ");
  }

  /* ───────── Vitals tiles ───────── */
  const goalNote = (lo, hi, unit) => {
    if (lo != null && hi != null && lo !== hi) return `goal ${fmt(lo, 1)}–${fmt(hi, 1)} ${unit}`;
    if (lo != null) return `goal ${fmt(lo, 0)} ${unit}`;
    if (hi != null) return `goal ${fmt(hi, 0)} ${unit}`;
    return "";
  };
  const VITALS = [
    { key: "resting_hr", src: "metrics", when: "at", label: "Resting heart rate", unit: "bpm", d: 0, color: "--m-rhr" },
    { key: "min_hr", src: "metrics", when: "at", label: "Lowest heart rate", unit: "bpm", d: 0, color: "--m-hr" },
    { key: "max_hr", src: "metrics", when: "at", label: "Highest heart rate", unit: "bpm", d: 0, color: "--m-hr" },
    { key: "hrv", src: "metrics", when: "at", label: "HRV", unit: "ms", d: 0, color: "--m-hrv",
      note: (r) => r.hrv_status ? String(r.hrv_status).toLowerCase().replaceAll("_", " ") : (r.hrv_weekly != null ? `7-day ${fmt(r.hrv_weekly, 0)} ms` : "") },
    { key: "stress_avg", src: "metrics", when: "at", label: "Stress", unit: "", d: 0, color: "--m-temp",
      note: (r) => r.stress_qualifier ? String(r.stress_qualifier).toLowerCase().replaceAll("_", " ") : "" },
    { key: "body_battery", src: "metrics", when: "at", label: "Body Battery", unit: "", d: 0, color: "--m-battery",
      note: (r) => [r.bb_charged != null ? `charged ${fmt(r.bb_charged, 0)}` : "", r.bb_drained != null ? `drained ${fmt(r.bb_drained, 0)}` : ""].filter(Boolean).join(" · ") },
    { key: "steps", src: "metrics", when: "at", label: "Steps", unit: "", d: 0, color: "--m-strain",
      note: (r) => r.step_goal != null ? `goal ${fmt(r.step_goal, 0)}` : "" },
    { key: "hydration_ml", src: "metrics", when: "at", label: "Hydration", unit: "ml", d: 0, color: "--m-spo2",
      note: (r) => r.hydration_goal_ml != null ? `goal ${fmt(r.hydration_goal_ml, 0)} ml` : "" },
    { key: "spo2", src: "metrics", when: "at", label: "Blood oxygen", unit: "%", d: 1, color: "--m-spo2" },
    { key: "resp_rate", src: "metrics", when: "at", label: "Respiratory rate", unit: "rpm", d: 1, color: "--m-resp" },
    { key: "weight_kg", src: "metrics", when: "at", label: "Weight", unit: "kg", d: 1, color: "--m-strain",
      note: (r) => goalNote(r.weight_goal_low_kg, r.weight_goal_high_kg, "kg") },
    { key: "vo2_run", src: "metrics", when: "at", label: "Running VO2 Max", unit: "ml/kg/min", d: 1, color: "--m-hrv" },
    { key: "vo2_cycle", src: "metrics", when: "at", label: "Cycling VO2 Max", unit: "ml/kg/min", d: 1, color: "--m-resp" },
    { key: "fitness_age", src: "metrics", when: "at", label: "Fitness age", unit: "yr", d: 0, color: "--m-rhr" },
  ];
  function renderVitals(d) {
    const host = $("#vitals");
    host.innerHTML = "";
    for (const m of VITALS) {
      const when = m.when || (m.src === "sleep" ? "end" : "created_at");
      const rows = (d[m.src] || []).slice().reverse();
      const series = rows.map((r) => r[m.key]);
      const latest = ofDay(rows.at(-1), when) ? series.at(-1) : null;
      const b = baseline(series);
      const card = el("article", "card vital");
      const id = `spark-${m.key}`;
      const last30 = rows.slice(-30);
      const hasLine = last30.length >= 2 && latest != null;
      let flag = "", delta = `<p class="vital__delta">${rows.length && latest == null ? "No reading this day" : "Not enough history yet"}</p>`, word = "";
      if (b && latest != null) {
        const z = b.sd ? (latest - b.mean) / b.sd : 0;
        const f = z > 1 ? "up" : z < -1 ? "down" : "info";
        word = f === "up" ? "Above usual" : f === "down" ? "Below usual" : "Usual";
        const pct = ((latest - b.mean) / b.mean) * 100;
        const sign = pct > 0 ? "+" : pct < 0 ? "−" : "±";
        // The state is written out, not carried by the dot's color alone.
        flag = `<span class="vital__flag" data-zone="${f}"><span class="status__icon" aria-hidden="true"></span>${word}</span>`;
        delta = `<p class="vital__delta" data-zone="${f}"><b>${sign}${n1.format(Math.abs(pct))}%</b> vs avg ${fmt(b.mean, m.d)} ${m.unit}</p>`;
      }
      const row = ofDay(rows.at(-1), when);
      const note = row && m.note ? m.note(row) : "";
      if (note) delta += `<p class="vital__delta">${note}</p>`;
      card.innerHTML = `
        <div class="vital__top">
          <h3 class="vital__label"><span class="swatch" style="--c:var(${m.color})" aria-hidden="true"></span>${m.label}</h3>
          ${flag}
        </div>
        <p class="vital__value"><span class="vital__num">${fmt(latest, m.d)}</span><span class="vital__unit">${m.unit}</span></p>
        ${delta}
        <div class="vital__spark">${hasLine ? `<canvas id="${id}" role="img" aria-label="${m.label}, last 30 readings"></canvas>` : ""}</div>
        ${refBar(series, latest, m.color, m.d)}`;
      host.append(card);
      if (!hasLine) continue;   // no line of older days under "no reading this day"
      const color = tok(m.color);
      const opts = baseOptions();
      opts.scales.x.display = false;
      opts.scales.y.display = false;
      opts.layout.padding = { top: 6, bottom: 2, left: 4, right: 6 };
      opts.plugins.tooltip.callbacks = {
        title: (i) => dShort.format(+day(last30[i[0].dataIndex][when])),
        label: (i) => ` ${fmt(i.parsed.y, m.d)} ${m.unit}`,
      };
      opts.plugins.tooltip.displayColors = false;
      mount(id, {
        type: "line",
        data: {
          labels: last30.map((_, i) => i),
          datasets: [{
            data: last30.map((r) => r[m.key]), borderColor: color, borderWidth: 1.75, cubicInterpolationMode: "monotone",
            pointRadius: (c) => (c.dataIndex === last30.length - 1 ? 3 : 0), pointBackgroundColor: color,
            pointHoverRadius: 4, pointHoverBorderWidth: 2, pointHoverBorderColor: tok("--surface"), spanGaps: true,
            fill: true,
            backgroundColor: (ctx) => {
              const a = ctx.chart.chartArea; if (!a) return "transparent";
              const g = ctx.chart.ctx.createLinearGradient(0, a.top, 0, a.bottom);
              g.addColorStop(0, alpha(color, 0.22)); g.addColorStop(1, alpha(color, 0)); return g;
            },
          }],
        },
        options: opts,
        plugins: [crosshair],
      });
    }
  }

  /* ───────── Trends ───────── */
  function renderTrends(d) {
    const mets = inRange((d.metrics || []).slice().reverse(), "at")
      .map((r) => ({ ...r, created_at: r.at }));
    const rec = mets.filter((r) => r.body_battery != null);
    const labels = rec.map((r) => dShort.format(+day(r.at)));

    if (rec.length) {
      clearEmpty("c-rec");
      const zc = { good: tok("--good"), fair: tok("--fair"), low: tok("--low") };
      const scores = rec.map((r) => r.body_battery);
      const counts = { good: 0, fair: 0, low: 0 };
      scores.forEach((s) => counts[zoneOf(s)]++);
      $("#t-rec-stat").innerHTML = `Average <strong>${fmt(mean(scores), 0)}</strong> · ${counts.good} high, ${counts.fair} moderate, ${counts.low} low`;
      const o = baseOptions({ y: { min: 0, max: 100, ticks: { ...baseOptions().scales.y.ticks, stepSize: 25 } }, plugins: { avgLine: { value: mean(scores) } } });
      o.plugins.tooltip.callbacks = {
        label: (i) => ` ${fmt(i.parsed.y, 0)} · ${ZONE_LABEL[zoneOf(i.parsed.y)]}`,
      };
      o.plugins.tooltip.displayColors = true;
      const c = mount("c-rec", {
        type: "bar",
        data: { labels, datasets: [{ data: scores, backgroundColor: (ctx) => vgrad(zc[zoneOf(scores[ctx.dataIndex])], 1, 0.45)(ctx), hoverBackgroundColor: scores.map((s) => zc[zoneOf(s)]), borderRadius: 4, borderSkipped: "start", categoryPercentage: 0.8, barPercentage: 0.88, maxBarThickness: 16 }] },
        options: o,
        plugins: [avgLine],
      });
      c.canvas.setAttribute("aria-label", `Body Battery over the last ${RANGE} days: average ${fmt(mean(scores), 0)}; ${counts.good} high, ${counts.fair} moderate and ${counts.low} low days.`);
    } else emptyState("c-rec", "No Body Battery in this range", "It fills after a Garmin sync.");

    lineTrend("c-hrv", "#t-hrv-stat", mets, "hrv", "--m-hrv", "ms", "Heart rate variability", d.metrics || []);
    lineTrend("c-rhr", "#t-rhr-stat", mets, "resting_hr", "--m-rhr", "bpm", "Resting heart rate", d.metrics || []);
  }

  function lineTrend(id, statSel, rows, key, colorVar, unit, name, all) {
    const vals = rows.map((r) => r[key]);
    if (nums(vals).length < 2) return emptyState(id, `No ${name.toLowerCase()} data`, "It fills after a Garmin sync.");
    clearEmpty(id);
    const color = tok(colorVar);
    // Baseline over the range shown, so the band matches what you see.
    const m = mean(vals), s = sd(vals) ?? 0;
    const avg7 = rolling(vals, 7);
    $(statSel).innerHTML = `Average <strong>${fmt(m)} ${unit}</strong> · range ${fmt(Math.min(...nums(vals)))}–${fmt(Math.max(...nums(vals)))} ${unit}`;
    const o = baseOptions({ plugins: { band: { lo: m - s, hi: m + s } } });
    const lo = Math.min(...nums(vals), m - s), hi = Math.max(...nums(vals), m + s), pad = (hi - lo) * 0.12;
    o.scales.y.min = Math.max(0, Math.floor((lo - pad) / 5) * 5);
    o.scales.y.max = Math.ceil((hi + pad) / 5) * 5;
    o.plugins.tooltip.callbacks = {
      label: (i) => ` ${i.dataset.label}: ${fmt(i.parsed.y)} ${unit}${i.datasetIndex === 0 ? estNote(rows[i.dataIndex]) : ""}`,
    };
    const c = mount(id, {
      type: "line",
      data: {
        labels: rows.map((r) => dShort.format(+day(r.created_at))),
        datasets: [
          { label: "Daily", data: vals, borderColor: alpha(color, 0.4), backgroundColor: alpha(color, 0.4), borderWidth: 1.25, cubicInterpolationMode: "monotone", pointRadius: 0, pointHoverRadius: 3, spanGaps: true, pointStyle: "circle" },
          { label: "7-day average", data: avg7, borderColor: color, backgroundColor: (ctx) => { const a = ctx.chart.chartArea; if (!a) return color; const g = ctx.chart.ctx.createLinearGradient(0, a.top, 0, a.bottom); g.addColorStop(0, alpha(color, 0.18)); g.addColorStop(1, alpha(color, 0)); return g; }, fill: "origin", borderWidth: 2.25, cubicInterpolationMode: "monotone", pointRadius: 0, pointHoverRadius: 4, pointHoverBorderWidth: 2, pointHoverBorderColor: tok("--surface"), spanGaps: true, pointStyle: "circle" },
        ],
      },
      options: o,
      plugins: [band, crosshair],
    });
    c.canvas.setAttribute("aria-label", `${name} over the last ${RANGE} days: average ${fmt(m)} ${unit}, from ${fmt(Math.min(...nums(vals)))} to ${fmt(Math.max(...nums(vals)))} ${unit}. Thin line is daily, thick line is the 7-day average.`);
  }

  /* ───────── Sleep ───────── */
  const STAGES = [
    { key: "total_sws_ms", label: "Deep", color: "--stage-deep" },
    { key: "total_rem_ms", label: "REM", color: "--stage-rem" },
    { key: "total_light_ms", label: "Light", color: "--stage-light" },
    { key: "total_awake_ms", label: "Awake", color: "--stage-awake" },
  ];
  // Strap estimates know asleep vs awake, not deep/REM/light.
  const stagesOf = (s) => (s.est ? [{ key: "_asleep", label: "Asleep", color: "--stage-light" }, STAGES[3]] : STAGES);
  const withAsleep = (s) => (s.est ? { ...s, _asleep: (s.total_in_bed_ms || 0) - (s.total_awake_ms || 0) } : s);
  function renderLastNight(d) {
    const s0 = ofDay(d.sleep[0], "end"), s = s0 && withAsleep(s0);
    const host = $("#lastnight");
    if (!s) {
      const why = heldWhy("sleep");
      $("#ln-date").textContent = "";
      host.innerHTML = `<div class="empty"><div><strong>No sleep for this night</strong>${why ? `Held back: ${esc(why)}` : "Sync Garmin. A night shows up here after Connect has scored it."}</div></div>`;
      return;
    }
    const recent = Date.now() - +day(s.end) < 36 * 36e5;
    $("#ln-h").textContent = recent ? "Last night" : "That night";
    $("#ln-date").textContent = `${recent ? "" : dShort.format(+day(s.end)) + (new Date(s.end).getFullYear() !== new Date().getFullYear() ? `, ${new Date(s.end).getFullYear()}` : "") + " · "}${dTime.format(+day(s.start))} – ${dTime.format(+day(s.end))}`;
    const asleep = (s.total_in_bed_ms || 0) - (s.total_awake_ms || 0);
    const SG = stagesOf(s);
    const total = SG.reduce((t, st) => t + (s[st.key] || 0), 0) || 1;
    host.innerHTML = `
      <div>
        <p class="headline" aria-label="${hm(asleep)} asleep"><span class="headline__num">${Math.floor(asleep / 36e5)}</span><span class="headline__unit">h</span><span class="headline__num">${Math.round((asleep % 36e5) / 6e4)}</span><span class="headline__unit">m</span></p>
        <p class="headline__label">Time asleep${s.sleep_perf == null ? "" : ` · ${fmt(s.sleep_perf)}% of sleep needed`} · Garmin</p>
      </div>
      <div>
        <div class="stagebar" role="img" aria-label="${SG.map((st) => `${st.label} ${hm(s[st.key])}`).join(", ")}">
          ${SG.map((st) => `<span style="--c:var(${st.color});flex:${s[st.key] || 0}"></span>`).join("")}
        </div>
      </div>
      <ul class="stagelist">
        ${SG.map((st) => `<li><span class="swatch" style="--c:var(${st.color})"></span><span>${st.label}</span><span class="v">${hm(s[st.key])}</span><span class="p">${n0.format(((s[st.key] || 0) / total) * 100)}%</span></li>`).join("")}
      </ul>
      <dl class="kv kv--grid">
        <div><dt>Efficiency</dt><dd>${fmtU(s.sleep_efficiency, "%")}</dd></div>
        <div><dt>Consistency</dt><dd>${fmtU(s.sleep_consistency, "%")}</dd></div>
        <div><dt>Respiratory rate</dt><dd>${fmtU(s.resp_rate, " rpm", 1)}</dd></div>
        <div><dt>Disturbances</dt><dd>${fmt(s.disturbances)}</dd></div>
      </dl>`;
  }

  function renderSleepChart(d) {
    const rows = inRange(d.sleep.slice().reverse(), "end");
    if (!rows.length) return emptyState("c-sleep", "No sleep in this range", "Sleep appears here after a Garmin sync.");
    clearEmpty("c-sleep");
    const h = (ms) => (ms || 0) / 36e5;
    const asleep = rows.map((s) => h(s.total_in_bed_ms) - h(s.total_awake_ms));
    $("#sh-stat").innerHTML = `Average <strong>${hm(mean(asleep) * 36e5)}</strong> asleep per night · ${rows.length} nights`;
    const surface = tok("--surface");
    const o = baseOptions({ stacked: true, y: { suggestedMax: 10, ticks: { ...baseOptions().scales.y.ticks, stepSize: 2, callback: (v) => `${v}h` } } });
    o.plugins.tooltip.callbacks = {
      label: (i) => ` ${i.dataset.label}: ${hm(i.parsed.y * 36e5)}`,
      footer: (items) => `Asleep ${hm(asleep[items[0].dataIndex] * 36e5)}`,
    };
    o.plugins.tooltip.itemSort = (a, b) => b.datasetIndex - a.datasetIndex;
    const c = mount("c-sleep", {
      type: "bar",
      data: {
        labels: rows.map((s) => dShort.format(+day(s.end))),
        datasets: [...STAGES, { key: "_asleep", label: "Asleep", color: "--stage-light" }].map((st, i, all) => ({
          label: st.label, data: rows.map((s) => h(withAsleep(s)[st.key])), backgroundColor: st.key === "_asleep" ? alpha(tok(st.color), 0.55) : tok(st.color),
          borderColor: surface, borderWidth: { top: i === all.length - 1 ? 0 : 1.5, bottom: 0, left: 0, right: 0 },
          borderRadius: i === all.length - 1 ? { topLeft: 3, topRight: 3 } : 0, borderSkipped: false,
          categoryPercentage: 0.82, barPercentage: 0.9, maxBarThickness: 14,
        })),
      },
      options: o,
    });
    c.canvas.setAttribute("aria-label", `Sleep stages per night over the last ${RANGE} days, average ${hm(mean(asleep) * 36e5)} asleep.`);
  }

  /* ───────── Activity ───────── */
  function renderStrain(d) {
    const garmin = true;
    const heading = document.getElementById("st-h");
    if (heading) heading.textContent = "Active calories";
    const rows = inRange(d.cycle.filter((c) => c.end && (c.kilojoule || 0) > 0).reverse(), "start");
    if (!rows.length) return emptyState("c-strain", "No Garmin activity in this range", "A day shows up here after Garmin sync when that day has calories.");
    clearEmpty("c-strain");
    const vals = rows.map((r) => garmin ? r.kilojoule / 4.184 : r.strain);
    const peak = rows[vals.indexOf(Math.max(...vals))];
    const peakVal = garmin ? peak.kilojoule / 4.184 : peak.strain;
    $("#st-stat").innerHTML = `Average <strong>${fmt(mean(vals), garmin ? 0 : 1)}</strong>${garmin ? " kcal" : ""} · peak ${fmt(peakVal, garmin ? 0 : 1)} on ${dShort.format(+day(peak.start))}`;
    const o = baseOptions(garmin
      ? { y: { min: 0, ticks: { ...baseOptions().scales.y.ticks } }, plugins: { avgLine: { value: mean(vals) } } }
      : { y: { min: 0, max: 21, ticks: { ...baseOptions().scales.y.ticks, stepSize: 7 } }, plugins: { avgLine: { value: mean(vals) } } });
    o.plugins.tooltip.callbacks = {
      label: (i) => garmin ? ` ${fmt(i.parsed.y, 0)} kcal` : ` Strain ${fmt(i.parsed.y, 1)}${estNote(rows[i.dataIndex])}`,
      afterLabel: (i) => {
        const r = rows[i.dataIndex];
        return r.avg_hr ? ` Avg ${fmt(r.avg_hr)} bpm · max ${fmt(r.max_hr)} bpm` : "";
      },
    };
    const c = mount("c-strain", {
      type: "bar",
      data: { labels: rows.map((r) => dShort.format(+day(r.start))), datasets: [{ data: vals, backgroundColor: vgrad(tok("--m-strain"), 1, 0.35), hoverBackgroundColor: tok("--m-strain"), borderRadius: 4, borderSkipped: "start", categoryPercentage: 0.8, barPercentage: 0.88, maxBarThickness: 16 }] },
      options: o,
      plugins: [avgLine],
    });
    c.canvas.setAttribute("aria-label", garmin
      ? `Active calories over the last ${RANGE} days, average ${fmt(mean(vals), 0)} kcal.`
      : `Day strain over the last ${RANGE} days, average ${fmt(mean(vals), 1)} out of 21.`);
  }

  const ZONES = [
    ["zone_zero_milli", "Zone 0", "<50%"], ["zone_one_milli", "Zone 1", "50–60%"], ["zone_two_milli", "Zone 2", "60–70%"],
    ["zone_three_milli", "Zone 3", "70–80%"], ["zone_four_milli", "Zone 4", "80–90%"], ["zone_five_milli", "Zone 5", "90–100%"],
  ];
  function renderZones(d) {
    const inR = inRange(d.workout.slice().reverse(), "start");
    const ws = inR.filter((w) => w.zones);
    const host = $("#zones");
    const tot = ZONES.map(([k]) => ws.reduce((s, w) => s + (w.zones[k] || 0), 0));
    const sum = tot.reduce((a, b) => a + b, 0);
    if (!sum) {
      const sets = (d.profile && d.profile.garmin_zones) || [];
      const zset = sets.find((z) => z && z.zone1Floor != null);
      if (zset) {
        const floors = [zset.zone1Floor, zset.zone2Floor, zset.zone3Floor, zset.zone4Floor, zset.zone5Floor];
        $("#hz-stat").textContent = zset.sport ? `${zset.sport} zone floors` : "Heart rate zone floors";
        host.innerHTML = floors.map((bpm, i) => `
          <div class="zone">
            <span class="zone__name">Zone ${i + 1}</span>
            <span class="zone__val">${bpm == null ? "—" : fmt(bpm, 0)} bpm</span>
          </div>`).join("");
        return;
      }
      const [t, h] = inR.length
        ? ["No zone data", `${inR.length} workout${inR.length === 1 ? "" : "s"} in this range, none with heart rate zones.`]
        : ["No heart rate zones yet", "They appear after a Garmin sync."];
      host.innerHTML = `<div class="empty"><div><strong>${t}</strong>${h}</div></div>`;
      $("#hz-stat").textContent = "";
      return;
    }
    const max = Math.max(...tot);
    $("#hz-stat").innerHTML = `<strong>${durMin(sum)}</strong> across ${ws.length} workout${ws.length === 1 ? "" : "s"}`;
    host.innerHTML = ZONES.map(([, name, pct], i) => `
      <div class="zone">
        <span class="zone__name">${name}<small>${pct}</small></span>
        <span class="zone__track" role="img" aria-label="${name}: ${durMin(tot[i])}, ${n0.format((tot[i] / sum) * 100)} percent"><span class="zone__fill" style="--c:var(--zone-${i});width:${(tot[i] / max) * 100}%"></span></span>
        <span class="zone__val">${durMin(tot[i])}</span>
      </div>`).join("");
  }

  const SPORT = {
    running: ["--m-rhr", '<circle cx="12" cy="4.5" r="1.8"/><path d="m8 21 2.5-6 3 2.5V22M6 11.5 9 8l4 1.5 2.5 3.5 3 .5M10.5 15l-1-4.5"/>'],
    cycling: ["--m-strain", '<circle cx="5.5" cy="16.5" r="3.5"/><circle cx="18.5" cy="16.5" r="3.5"/><path d="M5.5 16.5 9 9h6l3.5 7.5M9 9l3.5 7.5H5.5M14 5.5h2.5"/>'],
    strength: ["--m-spo2", '<path d="M6.5 7v10M17.5 7v10M3.5 9.5v5M20.5 9.5v5M6.5 12h11"/>'],
    yoga: ["--m-resp", '<circle cx="12" cy="4.5" r="1.8"/><path d="M12 7.5v6M6 10.5l6 1.5 6-1.5M8 20l4-6.5 4 6.5M5 20h14"/>'],
    rowing: ["--m-hrv", '<path d="M3 17c3 2 6 2 9 0s6-2 9 0M5 13l14-8M15 7.5l3 3"/>'],
  };
  const sportIcon = (key) => {
    const [c, path] = SPORT[key] || ["--text-3", '<path d="M13 3 5 13h6l-1 8 8-10h-6l1-8Z"/>'];
    return `<span class="activity__icon" style="--c:var(${c})" aria-hidden="true"><svg viewBox="0 0 24 24">${path}</svg></span>`;
  };
  function renderWorkouts(d) {
    const all = d.workout;
    const shown = woExpanded ? all : all.slice(0, 8);
    $("#wo-count").textContent = all.length ? `${all.length} recorded` : "";
    const body = $("#wo-body");
    if (!all.length) {
      body.innerHTML = `<tr><td colspan="7"><div class="empty"><div><strong>No workouts yet</strong>Garmin activities show up here after a sync.</div></div></td></tr>`;
      return;
    }
    body.innerHTML = shown.map((w) => {
      const dur = w.end ? +day(w.end) - +day(w.start) : null;
      const name = titleCase(w.sport_name);
      return `<tr>
        <td><span>${dShort.format(+day(w.start))}</span> <span class="muted">${dTime.format(+day(w.start))}</span></td>
        <td><span class="activity">${sportIcon((w.sport_name || "").toLowerCase())}${esc(name)}</span></td>
        <td class="num">${dur ? durMin(dur) : "—"}</td>
        <td><span class="strainbar"><span class="strainbar__track" aria-hidden="true"><span class="strainbar__fill" style="width:${((w.strain || 0) / 21) * 100}%"></span></span><span class="strainbar__v">${fmt(w.strain, 1)}</span></span></td>
        <td class="num">${fmt(w.avg_hr)} <span class="muted">bpm</span></td>
        <td class="num">${fmt(w.max_hr)} <span class="muted">bpm</span></td>
        <td class="num">${w.kilojoule ? fmt(w.kilojoule / 4.184) : "—"} <span class="muted">kcal</span></td>
      </tr>`;
    }).join("");
    const more = $("#wo-more");
    more.hidden = all.length <= 8;
    more.textContent = woExpanded ? "Show fewer" : `Show all ${all.length} workouts`;
    more.setAttribute("aria-expanded", String(woExpanded));
  }
  $("#wo-more").addEventListener("click", () => { woExpanded = !woExpanded; renderWorkouts(V()); });

  /* ───────── Device ───────── */
  const TAG = { SUPERVISOR: "#f4b740", BLE: "#6cb8ff", SENSOR: "#3df5b0", STORAGE: "#a08fff", HR: "#ff7a90" };
  function renderDevice(d) {
    const bat = $("#battery");
    $("#side-bat").textContent = "Garmin";
    bat.innerHTML = `<div class="empty"><div><strong>No battery</strong>Garmin Connect does not report one.</div></div>`;
    renderDeviceRest(d);
  }

  function renderDeviceRest(d) {

    const labels = { api_recovery: "Daily vitals", api_sleep: "Sleeps", api_cycle: "Daily cycles", api_workout: "Workouts", ble_hr: "Heart rate samples", ble_raw: "Raw frames" };
    $("#counts").innerHTML = Object.entries(d.counts).map(([k, v]) => `<div><dt>${labels[k] || k}</dt><dd>${n0.format(v)}</dd></div>`).join("");
    renderCoverage(d.coverage || []);

    const logs = d.logs || [];
    $("#logs").innerHTML = logs.length
      ? logs.map((l) => `<li><span class="t">${esc(l.rtc)}</span><span class="s" style="--tag:${TAG[l.subsystem] || "#8b949e"}">${esc(l.subsystem)}</span><span class="m">${esc(l.message)}</span></li>`).join("")
      : `<li class="empty"><div><strong>No device log</strong>Heart rate, sleep, and activity come from Garmin Connect.</div></li>`;
  }

  // How much of each day has heart rate (metrics.coverage_days on the Mac). The light part of
  // the bar is time the strap still holds and hands over at the next sync.
  function renderCoverage(days) {
    const el = $("#coverage");
    if (!days.length) {
      el.innerHTML = `<li class="empty"><div><strong>No heart rate yet</strong>Coverage shows after a Garmin sync.</div></li>`;
      return;
    }
    const hm = (t) => new Date(t * 1000).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
    const dur = (s) => (s >= 3600 ? `${Math.floor(s / 3600)}h ${Math.round((s % 3600) / 60)}m` : `${Math.round(s / 60)} min`);
    el.innerHTML = days.map((c, i) => {
      const day = i === 0 ? "Today" : i === 1 ? "Yesterday" : new Date(`${c.date}T12:00:00`).toLocaleDateString([], { weekday: "short", day: "numeric" });
      const rec = Math.min(100, c.share * 100);
      const wait = c.span_s ? Math.min(100 - rec, (c.pending_s / c.span_s) * 100) : 0;
      const notes = [];
      if (c.longest && c.share < 0.95) notes.push(`Longest gap ${hm(c.longest.start)}–${hm(c.longest.end)}.`);
      return `<li class="coverage__row">
        <span class="coverage__day">${esc(day)}</span>
        <span class="coverage__bar" role="img" aria-label="${fmt(rec)} percent recorded"><i style="width:${rec}%"></i><i style="width:${wait}%"></i></span>
        <span class="coverage__pct">${fmt(rec)}%</span>
        ${notes.length ? `<span class="coverage__why">${esc(notes.join(" "))}</span>` : ""}
      </li>`;
    }).join("");
  }

  /* ───────── Live heart rate ───────── */
  // A reading counts as "now" while the strap streams, or for a few minutes after (the phone gets
  // the Mac's copy every few minutes). Older than that there is no current value: an old number
  // is never shown as now.
  const FRESH_MS = 15 * 6e4;
  function currentHr(L) {
    if (L?.live) return L.hr;
    const last = (L?.recent || []).filter((r) => r.hr).at(-1);
    return last && Date.now() - last.ts * 1000 < FRESH_MS ? last.hr : null;
  }
  let lastLive = null;
  function renderLive(L) {
    lastLive = L;
    const card = $(".card--monitor");
    const pill = $("#live-pill"), pillText = $("#live-pill-text");
    const all = (L?.recent || []).filter((r) => r.hr && r.source === "garmin");
    const today = ymd(Date.now());
    const recent = all.filter((r) => ymd(r.ts * 1000) === today); // the trace and its stats are today's samples only
    const live = false;
    card.dataset.live = "false";
    pill.dataset.state = "off";
    pillText.textContent = "Garmin";
    $("#side-dot").dataset.state = recent.length ? "ok" : "off";
    $("#side-device").textContent = DATA?.profile?.garmin_device || "Garmin";
    const hr = currentHr({ ...L, live: false, recent: all });
    const heard = all.at(-1)?.ts * 1000;
    $("#device-state .dot").dataset.state = heard ? "ok" : "off";
    $("#device-state-text").textContent = heard ? `Last Garmin reading ${ago(heard)}` : "Not synced";
    $("#live-hr").textContent = hr ?? "—";
    const lastTs = all.at(-1)?.ts * 1000;
    $("#live-note").textContent = lastTs ? `Last reading ${ago(lastTs)}.` : "No readings yet. Sync Garmin.";
    const rr = L?.rr_ms?.[0];
    $("#live-rr").textContent = live && rr ? `${fmt(rr)} ms` : "—";
    const hrs = recent.map((r) => r.hr);
    $("#live-range").textContent = hrs.length ? `${Math.min(...hrs)} / ${Math.max(...hrs)} bpm` : "—";
    $("#live-avg").textContent = hrs.length ? `${fmt(mean(hrs))} bpm` : "—";

    const color = tok("--m-hr-ui");
    const data = recent.map((r) => r.hr);
    const labels = recent.map((r) => dTime.format(r.ts * 1000));
    let c = charts.get("c-live");
    if (c) {
      c.data.labels = labels;
      c.data.datasets[0].data = data;
      c.data.datasets[0].borderColor = live ? color : alpha(color, 0.55);
      c.options.plugins.head.color = live ? color : null;
      c.update("none");
    } else {
      const o = baseOptions({ y: { grace: "18%", ticks: { ...baseOptions().scales.y.ticks, maxTicksLimit: 4 } }, plugins: { glow: { color: "transparent", blur: 0 }, head: { color: live ? color : null } } });
      o.scales.x.display = false;
      o.animation = false;
      o.layout.padding = { top: 12, bottom: 6, left: 6, right: 0 };
      o.plugins.tooltip.callbacks = { label: (i) => ` ${i.parsed.y} bpm` };
      o.plugins.tooltip.displayColors = false;
      c = mount("c-live", {
        type: "line",
        data: { labels, datasets: [{ data, borderColor: live ? color : alpha(color, 0.55), borderWidth: 1.5, tension: 0.35, pointRadius: 0, pointHoverRadius: 4, pointHoverBackgroundColor: color, pointHoverBorderColor: tok("--surface"), pointHoverBorderWidth: 2, fill: true,
          backgroundColor: (ctx) => {
            const a = ctx.chart.chartArea; if (!a) return "transparent";
            const g = ctx.chart.ctx.createLinearGradient(0, a.top, 0, a.bottom); g.addColorStop(0, alpha(color, 0.12)); g.addColorStop(1, alpha(color, 0)); return g;
          } }] },
        options: o,
        plugins: [glow, head],
      });
    }
    c.canvas.setAttribute("aria-label", hrs.length ? `Heart rate trace, last ${hrs.length} samples, ${Math.min(...hrs)} to ${Math.max(...hrs)} bpm` : "No heart rate samples yet");
  }
  // Announce only connection changes, not every beat.
  const announcer = el("div", "sr-only"); announcer.setAttribute("aria-live", "polite"); document.body.append(announcer);
  let wasLive = null;
  async function pollLive() {
    try {
      const L = await (await fetch("/api/live", { cache: "no-store" })).json();
      if (wasLive !== null && wasLive !== !!L.hr) announcer.textContent = L.hr ? `Heart rate ${L.hr}` : "No current heart rate";
      wasLive = L.live;
      renderLive(L);
      dispatchEvent(new CustomEvent("vitals:live", { detail: L }));
    } catch { /* server restarting; next poll retries */ }
  }

  /* ───────── Range control ───────── */
  $("#range").addEventListener("change", (e) => {
    RANGE = +e.target.value;
    renderRanged();
  });
  function renderRanged() {
    const v = V();
    renderTrends(v);
    renderSleepChart(v);
    renderStrain(v);
    renderZones(v);
  }

  /* ───────── Nav highlight ───────── */
  const links = [...document.querySelectorAll(".nav a:not([data-view])")];
  const io = new IntersectionObserver((entries) => {
    if (document.body.dataset.view === "heart") return;
    for (const e of entries) if (e.isIntersecting) {
      links.forEach((a) => a.toggleAttribute("aria-current", a.getAttribute("href") === `#${e.target.id}`));
      links.forEach((a) => a.hasAttribute("aria-current") && a.setAttribute("aria-current", "true"));
    }
  }, { rootMargin: "-45% 0px -50% 0px" });
  document.querySelectorAll("main .section").forEach((s) => io.observe(s));

  const topbar = $(".topbar");
  const onScroll = () => topbar.toggleAttribute("data-scrolled", scrollY > 8);
  addEventListener("scroll", onScroll, { passive: true });

  const summaryUrl = () => { const d = new Date(); d.setHours(0, 0, 0, 0); return `/api/summary?since=${Math.floor(+d / 1000)}`; };

  /* ───────── Period: Today (strap) vs Archive (WHOOP cloud) ───────── */
  const dFull = new Intl.DateTimeFormat(undefined, { month: "short", day: "numeric", year: "numeric" });
  const dDayFull = new Intl.DateTimeFormat(undefined, { weekday: "short", month: "short", day: "numeric", year: "numeric" });
  const dMonth = new Intl.DateTimeFormat(undefined, { month: "long", year: "numeric" });
  const ymd = (t) => { const d = new Date(t); return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`; };
  const fromYmd = (k) => { const [y, m, d] = k.split("-").map(Number); return new Date(y, m - 1, d); };
  let PERIOD = "today", ADATE = null; // ADATE = "YYYY-MM-DD" of the archive day being viewed
  let REC_BY_DAY = new Map(), DAYS = new Set();
  // Strap estimates (scores.py) come next to WHOOP's own rows, shaped the same, marked with
  // `est` (the model version). Only days after WHOOP's last score of each kind are added, so a
  // WHOOP score always wins over an estimate.
  function mergeEstimates() {
    // This board shows Garmin Connect only. Strap estimates are not mixed in.
  }
  const estNote = () => "";
  // The Day view shows a score only on the day it belongs to: an older one is not "this day".
  const ofDay = (r, key) => (r && (!ADATE || ymd(+day(r[key])) === ADATE) ? r : null);
  // Why a strap estimate for the chosen day was held back (scores.py's checks), if it was.
  const heldWhy = (kind) => DATA?.estimates?.held?.find((h) => h.date === ADATE && h.kind === kind)?.why || null;
  const VS = (window.VITALS = { get period() { return PERIOD; }, get date() { return ADATE; }, today: null, baseline: null, setPeriod, setDate, openCal, currentHr });

  // Archive lens: everything up to the end of the chosen day, so "latest" = that day.
  const onFeed = (r) => !!(r && r.source === "garmin");
  function V() {
    if (!DATA) return DATA;
    let recovery = DATA.recovery, sleep = DATA.sleep, cycle = DATA.cycle, workout = DATA.workout;
    let metrics = DATA.metrics || [];
    if (ADATE) {
      const end = +fromYmd(ADATE) + DAY;
      const le = (k) => (r) => +day(r[k]) < end;
      recovery = recovery.filter(le("created_at"));
      sleep = sleep.filter(le("end"));
      cycle = cycle.filter(le("start"));
      workout = workout.filter(le("start"));
      metrics = metrics.filter(le("at"));
    }
    return { ...DATA, recovery: recovery.filter(onFeed), sleep: sleep.filter(onFeed),
      cycle: cycle.filter(onFeed), workout: workout.filter(onFeed),
      metrics: metrics.filter(onFeed) };
  }
  function setPeriod(p) {
    if (p === "archive" && !ADATE) return;
    PERIOD = p;
    renderPeriod();
  }
  function setDate(k) {
    if (!DAYS.has(k)) return;
    ADATE = k;
    if (DATA) renderAll();
    PERIOD = "archive";
    renderPeriod();
  }
  function renderPeriod() {
    document.body.dataset.period = PERIOD;
    document.querySelectorAll("[data-period-btn]").forEach((b) => b.setAttribute("aria-pressed", String(b.dataset.periodBtn === PERIOD)));
    const t = ADATE ? fromYmd(ADATE) : null;
    document.querySelectorAll("[data-archive-label]").forEach((n) => { n.textContent = t ? dFull.format(t) : "Archive"; });
    document.querySelectorAll("[data-archive-day]").forEach((n) => { n.textContent = t ? dDayFull.format(t) : "Day"; });
    if (t) $("#arch-title").textContent = `Archive · ${dDayFull.format(t)}`;
    const a = DATA?.archive;
    if (a?.start) {
      const range = `${dFull.format(+day(a.start))} – ${dFull.format(+day(a.end))}`;
      $("#arch-text").textContent = `Garmin Connect, recorded ${range}. Nothing on this page is from today.`;
      document.querySelectorAll("[data-archive-range]").forEach((n) => { n.textContent = `${new Date(a.start).getFullYear()}–${String(new Date(a.end).getFullYear()).slice(2)}`; });
    }
    dispatchEvent(new CustomEvent("vitals:period", { detail: { period: PERIOD, date: ADATE } }));
    syncTabs();
  }
  function syncTabs() {
    const view = document.body.dataset.view;
    const tab = view === "heart" ? "heart" : location.hash === "#device" ? "device" : PERIOD === "archive" ? "archive" : "today";
    document.querySelectorAll("[data-tab]").forEach((a) => a.toggleAttribute("aria-current", a.dataset.tab === tab));
    document.querySelectorAll("[data-tab][aria-current]").forEach((a) => a.setAttribute("aria-current", "page"));
  }
  addEventListener("hashchange", () => requestAnimationFrame(syncTabs));
  const ARCHIVE_HASHES = ["#overview", "#trends", "#sleep", "#activity"];
  function routePeriod() {
    const h = location.hash;
    if (ARCHIVE_HASHES.includes(h)) setPeriod("archive");
    else if (h === "#today" || h === "#device") setPeriod("today");
  }
  addEventListener("hashchange", routePeriod);
  document.addEventListener("click", (e) => {
    const t = e.target.closest('[data-period-btn="today"]');
    if (t && document.body.dataset.view === "heart") { e.preventDefault(); setPeriod("today"); }
  });

  /* ───────── Today: strap data only ───────── */
  const parseRR = (v) => String(v || "").replace(/[\[\],]/g, " ").split(/\s+/).map(Number).filter((x) => x > 280 && x < 2000);
  function todayStats(d) {
    const S = (d.today_hr || []).filter((r) => r.hr > 0 && onFeed(r));
    if (!S.length) return null;
    let active = 0, rr = [], diffs = [];
    for (let i = 0; i < S.length; i++) {
      if (i) { const g = S[i].ts - S[i - 1].ts; if (g < 60) active += g; }
      const cur = parseRR(S[i].rr_ms);
      for (const x of cur) {
        const prev = rr.at(-1);
        if (prev && S[i].ts - prev.ts < 3.5 && Math.abs(x - prev.v) < 220) diffs.push(x - prev.v);
        rr.push({ v: x, ts: S[i].ts });
      }
    }
    const hrs = S.map((r) => r.hr);
    // HRV comes from the server (metrics.py): it drops beat intervals that don't fit the heart
    // rate, and it is the same number the Telegram message uses.
    const sv = d.today;
    const rmssd = sv ? sv.rmssd : diffs.length >= 30 ? Math.sqrt(diffs.reduce((a, b) => a + b * b, 0) / diffs.length) : null;
    return { samples: S, first: S[0].ts * 1000, last: S.at(-1).ts * 1000, activeMs: active * 1000,
      avg: mean(hrs), min: Math.min(...hrs), max: Math.max(...hrs), rmssd, nRR: sv ? sv.n_rr ?? 0 : diffs.length };
  }
  // Your usual: learned from your own strap nights once there are enough (learn.py);
  // until then the WHOOP archive, labelled with its year so it isn't mistaken for now.
  function usualBaseline(d) {
    const u = d.personal?.usual;
    if (u?.source !== "own") return { ...archiveBaseline(d), own: false };
    return { own: true, nights: u.nights, avgHr: null,
      rhr: { mean: u.rhr, sd: u.rhr_spread, n: u.nights },
      hrv: u.hrv ? { mean: u.hrv, sd: u.hrv * u.hrv_spread, n: u.nights } : null };
  }
  function archiveBaseline(d) {
    const r = d.recovery.filter((x) => !x.est).slice(0, 30), c = d.cycle.filter((x) => x.end && x.avg_hr && !x.est).slice(0, 30);
    const ms = (a) => ({ mean: mean(a), sd: sd(a) ?? 0, n: nums(a).length });
    return { rhr: ms(r.map((x) => x.resting_hr)), hrv: ms(r.map((x) => x.hrv_rmssd_ms)), avgHr: ms(c.map((x) => x.avg_hr)),
      end: d.archive?.end };
  }
  // Today's value on a strip spanning the archive baseline (mean ± 2.5 SD); band = usual (±1 SD).
  function cmpBar(v, b, colorVar, d = 0, unit = "") {
    if (v == null || !b?.mean) return "";
    const sdv = b.sd || b.mean * 0.1, lo = Math.min(v, b.mean - 2.5 * sdv), hi = Math.max(v, b.mean + 2.5 * sdv);
    const pos = (x) => ((x - lo) / (hi - lo)) * 100;
    return `<div class="ref" style="--c:var(${colorVar})" aria-hidden="true">
      <span class="ref__band" style="left:${pos(b.mean - sdv)}%;width:${pos(b.mean + sdv) - pos(b.mean - sdv)}%"></span>
      <span class="ref__mark" style="left:${pos(v)}%"></span></div>
      <div class="ref__ends" aria-hidden="true"><span>${fmt(lo, d)}</span><span>${b.own ? "your" : "archive"} usual ${fmt(b.mean - sdv, d)}–${fmt(b.mean + sdv, d)}${unit}</span><span>${fmt(hi, d)}</span></div>`;
  }
  // What learn.py knows about you, and three buttons to tell it how you feel today.
  function learnedBlock(d) {
    const p = d.personal;
    if (!p) return "";
    const feels = [["good", "Good"], ["ok", "OK"], ["rough", "Rough"]];
    return `<div class="learned">
      <p class="learned__q" id="feel-q">How do you feel today?</p>
      <div class="learned__feel" role="group" aria-labelledby="feel-q">
        ${feels.map(([k, label]) => `<button type="button" class="btn" data-feel="${k}" aria-pressed="${p.feel === k}">${label}</button>`).join("")}
      </div>
      <ul class="learned__list">${(p.text || []).map((t) => `<li>${esc(t)}</li>`).join("")}</ul>
    </div>`;
  }
  document.addEventListener("click", async (e) => {
    const b = e.target.closest("[data-feel]");
    if (!b || !DATA || DATA.demo) return;
    try {
      const r = await fetch("/api/feel", { method: "POST", headers: { "X-Vitals": "1", "Content-Type": "application/json" }, body: JSON.stringify({ feel: b.dataset.feel }) });
      if (!r.ok) return;
      const fresh = await (await fetch("/api/summary", { cache: "no-store" })).json();
      DATA.personal = fresh.personal;
      renderToday(DATA);
    } catch { /* offline: the buttons stay as they were */ }
  });
  function renderToday(d) {
    const T = todayStats(d), B = usualBaseline(d);
    for (const k of ["rhr", "hrv", "avgHr"]) if (B[k]) B[k].own = B.own;
    VS.today = T; VS.baseline = B;
    $("#today-meta").textContent = T
      ? `From Garmin, ${dTime.format(T.first)} – ${dTime.format(T.last)}`
      : `${dToday.format(new Date())} · no Garmin heart rate yet today`;
    const host = $("#today-summary");
    if (!T) {
      $("#ts-hint").textContent = "";
      host.innerHTML = `<div class="empty"><div><strong>Nothing from Garmin yet today</strong>Press Sync Garmin.</div></div>`;
      emptyState("c-today", "No heart rate today yet", "Samples appear here after a Garmin sync.");
      $("#today-stat").textContent = "";
      return;
    }
    $("#ts-hint").textContent = `${dTime.format(T.first)} – ${dTime.format(T.last)} · ${n0.format(T.samples.length)} samples`;
    const row = (name, v, unit, b, color, note, dd = 0) => `
      <li>
        <div class="driver__top"><span class="driver__name">${name}</span><span class="driver__val">${fmt(v, dd)}<small>${unit}</small></span></div>
        ${b?.mean ? cmpBar(v, b, color, dd) : `<p class="driver__note">${note}</p>`}
      </li>`;
    host.innerHTML = `
      <p class="headline"><span class="headline__num">${Math.floor(T.activeMs / 36e5)}</span><span class="headline__unit">h</span><span class="headline__num">${Math.round((T.activeMs % 36e5) / 6e4)}</span><span class="headline__unit">m</span></p>
      <p class="headline__label">of heart rate recorded today</p>
      <ul class="drivers">
        ${row("Average heart rate", T.avg, "bpm", B.avgHr, "--m-rhr", "")}
        ${row("Lowest heart rate", T.min, "bpm", B.rhr, "--m-rhr", "")}
        ${T.rmssd ? row("HRV, daytime RMSSD", T.rmssd, "ms", B.hrv, "--m-hrv", "") : row("HRV, daytime RMSSD", null, "ms", null, "--m-hrv", `Needs more clean beat-to-beat data: ${T.nRR} usable intervals so far.`)}
      </ul>
      ${learnedBlock(d)}
      <p class="fine">${B.own ? `Your usual comes from your last ${B.nights} nights.` : `Baselines come from your last 30 archive days (through ${B.end ? dFull.format(+day(B.end)) : "—"}).`} Daytime HRV reads lower than overnight HRV.</p>`;

    // Per-minute chart
    const buckets = new Map();
    for (const r of T.samples) {
      const k = Math.floor(r.ts / 60) * 60;
      const b = buckets.get(k) || []; b.push(r.hr); buckets.set(k, b);
    }
    const keys = [...buckets.keys()].sort((a, b) => a - b);
    const mn = [], mx = [], av = [];
    keys.forEach((k, i) => {
      if (i && k - keys[i - 1] > 120) { const gx = (keys[i - 1] + 60) * 1000; mn.push({ x: gx, y: null }); mx.push({ x: gx, y: null }); av.push({ x: gx, y: null }); }
      const v = buckets.get(k), x = k * 1000 + 30000;
      mn.push({ x, y: Math.min(...v) }); mx.push({ x, y: Math.max(...v) }); av.push({ x, y: mean(v) });
    });
    clearEmpty("c-today");
    $("#today-stat").innerHTML = `Average <strong>${fmt(T.avg)} bpm</strong> · range ${T.min}–${T.max} bpm · ${keys.length} minute${keys.length === 1 ? "" : "s"} with data`;
    const color = tok("--m-hr-ui");
    const o = baseOptions({ y: { grace: "10%" }, plugins: { avgLine: { value: B.rhr.mean } } });
    const x0 = Math.min(T.first, Date.now() - 2 * 36e5), x1 = Date.now();
    Object.assign(o.scales.x, { type: "linear", min: x0, max: x1, ticks: { ...o.scales.x.ticks, maxTicksLimit: 8, callback: (v) => dTime.format(v) } });
    o.plugins.tooltip.callbacks = { title: (i) => dTime.format(i[0].parsed.x), label: (i) => ` ${i.dataset.label}: ${fmt(i.parsed.y)} bpm` };
    o.interaction = { mode: "nearest", axis: "x", intersect: false };
    mount("c-today", {
      type: "line",
      data: { datasets: [
        { label: "Min", data: mn, borderWidth: 0, pointRadius: 0, spanGaps: false },
        { label: "Max", data: mx, borderWidth: 0, pointRadius: 0, fill: "-1", backgroundColor: alpha(color, 0.18), spanGaps: false },
        { label: "Average", data: av, borderColor: color, backgroundColor: color, borderWidth: 1.5, pointRadius: (c) => { const d = c.dataset.data; const i = c.dataIndex; return d[i] != null && d[i - 1] == null && d[i + 1] == null ? 1.5 : 0; }, pointHoverRadius: 4, tension: 0.25, spanGaps: false },
      ] },
      options: o,
      plugins: [avgLine, crosshair],
    }).canvas.setAttribute("aria-label", `Heart rate today from ${dTime.format(T.first)} to ${dTime.format(T.last)}, average ${fmt(T.avg)} bpm.`);
  }

  /* Energy tile note: WHOOP's number, or the strap estimate (energy.py) with its error, or why it is held back */
  function energySub(cyc) {
    if (!cyc?.est) return "whole day";
    if (cyc.kilojoule) return "whole day · rough estimate, often off by 15-20%";
    return cyc.energy_why ? `Held back: ${esc(cyc.energy_why)}` : "whole day";
  }

  /* ───────── Archive: day at a glance ───────── */
  function renderGlance(v) {
    const host = $("#glance");
    if (!ADATE) { host.innerHTML = ""; return; }
    const start = +fromYmd(ADATE), end = start + DAY;
    const on = (t) => +day(t) >= start && +day(t) < end;
    const cyc = v.cycle.find((c) => on(c.start));
    const slp = v.sleep.find((s) => on(s.end));
    const wos = v.workout.filter((w) => on(w.start));
    const met = dayMetric(v);
    $("#gl-hint").textContent = dDayFull.format(start);
    const tile = (label, val, unit, sub) => `<div class="glance__tile"><p class="glance__label">${label}</p><p class="glance__val">${val}<small>${unit}</small></p><p class="glance__sub">${sub}</p></div>`;
    const asleep = slp ? (slp.total_in_bed_ms || 0) - (slp.total_awake_ms || 0) : null;
    host.innerHTML = [
      tile("Active calories", cyc?.kilojoule ? fmt(cyc.kilojoule / 4.184, 0) : "—", "kcal", cyc?.avg_hr ? `avg ${fmt(cyc.avg_hr)} · max ${fmt(cyc.max_hr)} bpm` : "No Garmin day yet"),
      tile("Sleep", slp ? hm(asleep) : "—", "", slp ? `${fmtU(slp.sleep_perf, "% performance")}` : "No sleep scored"),
      tile("Steps", met?.steps != null ? fmt(met.steps, 0) : "—", "", met?.step_goal != null ? `goal ${fmt(met.step_goal, 0)}` : "From the daily summary"),
      tile("Workouts", String(wos.length), wos.length === 1 ? "session" : "sessions", wos.length ? esc(wos.map((w) => titleCase(w.sport_name)).join(", ")) : "Rest day"),
    ].join("");
  }

  /* ───────── Calendar ───────── */
  const cal = $("#cal");
  let calMonth = null, calTrigger = null;
  function openCal(trigger) {
    calTrigger = trigger;
    const base = ADATE ? fromYmd(ADATE) : DATA?.archive?.end ? new Date(DATA.archive.end) : new Date();
    calMonth = new Date(base.getFullYear(), base.getMonth(), 1);
    cal.hidden = false;
    drawCal();
    placeCal();
    cal.querySelector("[aria-selected='true'], .cal__day:not(:disabled)")?.focus();
  }
  function placeCal() {
    const r = calTrigger.getBoundingClientRect(), w = cal.offsetWidth, h = cal.offsetHeight;
    let left = Math.min(innerWidth - w - 12, Math.max(12, r.right - w));
    let top = r.bottom + 8;
    if (top + h > innerHeight - 12) top = Math.max(12, r.top - h - 8);
    cal.style.left = `${left}px`; cal.style.top = `${top}px`;
  }
  function closeCal() { cal.hidden = true; calTrigger?.focus(); }
  function drawCal() {
    const y = calMonth.getFullYear(), m = calMonth.getMonth();
    const first = new Date(y, m, 1), days = new Date(y, m + 1, 0).getDate();
    const lead = (first.getDay() + 6) % 7; // Monday first
    const todayK = ymd(Date.now());
    const wk = ["Mo", "Tu", "We", "Th", "Fr", "Sa", "Su"];
    let cells = "";
    for (let i = 0; i < lead; i++) cells += `<span></span>`;
    for (let dd = 1; dd <= days; dd++) {
      const k = `${y}-${String(m + 1).padStart(2, "0")}-${String(dd).padStart(2, "0")}`;
      const r = REC_BY_DAY.get(k), isToday = k === todayK;
      const z = r ? zoneOf(r.recovery_score) : "";
      const sel = (PERIOD === "archive" && k === ADATE) || (PERIOD === "today" && isToday);
      cells += `<button type="button" class="cal__day${isToday ? " cal__day--today" : ""}" data-k="${k}" data-zone="${z}" aria-selected="${sel}"
        ${DAYS.has(k) || isToday ? "" : "disabled"} aria-label="${dDayFull.format(new Date(y, m, dd))}${isToday ? ", today" : r ? ", Garmin day" : ", no data"}">${dd}<i></i></button>`;
    }
    const a = DATA?.archive;
    cal.innerHTML = `
      <div class="cal__head">
        <button type="button" class="cal__nav" data-step="-1" aria-label="Previous month">‹</button>
        <p class="cal__month">${dMonth.format(first)}</p>
        <button type="button" class="cal__nav" data-step="1" aria-label="Next month">›</button>
      </div>
      <div class="cal__grid">${wk.map((w) => `<span class="cal__wk">${w}</span>`).join("")}${cells}</div>
      <div class="cal__foot">
        <button type="button" class="cal__jump" data-jump="today"><span class="period__live"></span>Today</button>
        ${a?.end ? `<button type="button" class="cal__jump" data-jump="archive">Latest archive day</button>` : ""}
      </div>
      <p class="cal__key"><span><i data-zone="good"></i>High</span><span><i data-zone="fair"></i>Moderate</span><span><i data-zone="low"></i>Low</span></p>`;
  }
  cal.addEventListener("click", (e) => {
    const st = e.target.closest("[data-step]");
    if (st) { calMonth = new Date(calMonth.getFullYear(), calMonth.getMonth() + +st.dataset.step, 1); drawCal(); return; }
    const j = e.target.closest("[data-jump]");
    if (j?.dataset.jump === "archive") { const t = new Date(DATA.archive.end); calMonth = new Date(t.getFullYear(), t.getMonth(), 1); drawCal(); return; }
    const dbtn = e.target.closest(".cal__day");
    const isToday = j?.dataset.jump === "today" || dbtn?.classList.contains("cal__day--today");
    if (isToday) {
      setPeriod("today");
      if (document.body.dataset.view !== "heart") location.hash = "#today";
      return closeCal();
    }
    if (dbtn) {
      setDate(dbtn.dataset.k);
      if (document.body.dataset.view !== "heart" && !ARCHIVE_HASHES.includes(location.hash)) location.hash = "#overview";
      closeCal();
    }
  });
  document.addEventListener("click", (e) => {
    const t = e.target.closest("[data-cal]");
    if (t) { e.preventDefault(); cal.hidden || calTrigger !== t ? openCal(t) : closeCal(); return; }
    if (!cal.hidden && !e.target.closest("#cal")) cal.hidden = true;
  });
  document.addEventListener("keydown", (e) => { if (e.key === "Escape" && !cal.hidden) closeCal(); });
  addEventListener("resize", () => { if (!cal.hidden) placeCal(); });

  /* ───────── Resync ───────── */
  const syncBtns = () => document.querySelectorAll("[data-resync]");
  function setSyncUI(state, msg, tone) {
    syncBtns().forEach((b) => {
      b.dataset.state = state;
      const l = b.querySelector("[data-resync-label]");
      if (l) l.textContent = state === "running" ? "Syncing…" : l.closest(".notice") ? "Sync Garmin" : "Sync Garmin";
    });
    document.querySelectorAll("[data-sync-msg]").forEach((m) => { m.textContent = msg || ""; m.dataset.tone = tone || ""; });
  }
  function syncLine(log) {
    const lines = (log || []).filter((line) => !/dailysummaries failed/i.test(line));
    const last = lines.at(-1) || "Syncing…";
    return last.length > 80 ? last.slice(0, 80) + "…" : last;
  }
  async function pollSync(before) {
    const s = await (await fetch("/api/sync", { cache: "no-store" })).json();
    if (s.running) {
      setSyncUI("running", syncLine(s.log));
      return setTimeout(() => pollSync(before), 1500);
    }
    await load();
    const after = DATA?.recovery[0]?.created_at;
    const fresh = after && after !== before;
    const retrying = (s.log || []).some((line) => /saved to retry|Continuing in/.test(line));
    const done = s.ok === false
      ? "Garmin sync did not finish. Run it again to fill any skipped days."
      : retrying
        ? "Garmin sync finished. Days that did not return will fill on the next sync."
        : "Garmin sync finished.";
    setSyncUI("idle", done, s.ok === false ? "warn" : fresh ? "ok" : "ok");
  }
  async function startSync() {
    const before = DATA?.recovery[0]?.created_at;
    setSyncUI("running", "Starting…");
    try {
      const r = await fetch("/api/sync", { method: "POST", headers: { "X-Vitals": "1" } });
      const j = await r.json();
      if (!r.ok && !j.running) return setSyncUI("idle", j.error || "Couldn't start a sync.", "warn");
      pollSync(before);
    } catch { setSyncUI("idle", "Dashboard server didn't answer.", "warn"); }
  }
  document.addEventListener("click", (e) => { if (e.target.closest("[data-resync]")) startSync(); });

  /* ───────── Boot ───────── */
  function renderAll() {
    const v = V();
    renderHeader(DATA);
    renderToday(DATA);
    renderRecovery(v);
    renderGlance(v);
    renderVitals(v);
    renderLastNight(v);
    renderWorkouts(v);
    renderDevice(DATA);
    renderRanged();
    document.querySelectorAll("[data-resync]").forEach((b) => {
      b.disabled = !!DATA.demo || !!DATA.cloud;
      if (DATA.cloud) {
        const l = b.querySelector("[data-resync-label]");
        if (l) l.textContent = "Syncs from your Mac";
        b.setAttribute("aria-label", "Syncs automatically from your Mac");
      }
      b.title = DATA.demo ? "Disabled on the demo database" : "Pull heart rate, sleep, and activity from Garmin.";
    });
  }

  async function load() {
    const main = $("#main"), err = $("#load-error");
    main.setAttribute("aria-busy", "true");
    err.hidden = true;
    try {
      const r = await fetch(summaryUrl(), { cache: "no-store" });
      if (!r.ok) throw new Error(r.status);
      DATA = await r.json();
      mergeEstimates(DATA);
      REC_BY_DAY = new Map();
      const fedRec = DATA.recovery.filter(onFeed);
      for (const x of fedRec.slice().reverse()) REC_BY_DAY.set(ymd(+day(x.created_at)), x);
      DAYS = new Set([
        ...REC_BY_DAY.keys(),
        ...DATA.sleep.filter(onFeed).map((x) => ymd(+day(x.end))),
        ...DATA.cycle.filter(onFeed).map((x) => ymd(+day(x.start))),
        ...(DATA.metrics || []).filter(onFeed).map((x) => x.date || ymd(+day(x.at))),
      ]);
      if (!ADATE || !DAYS.has(ADATE)) ADATE = [...DAYS].sort().at(-1) ?? null;
      main.setAttribute("aria-busy", "false");
      renderAll();
      routePeriod();
      renderPeriod();
      dispatchEvent(new CustomEvent("vitals:data", { detail: DATA }));
    } catch (e) {
      console.error(e);
      main.setAttribute("aria-busy", "false");
      err.hidden = false;
      $("#sync-status").textContent = "Offline";
    }
  }
  $("#retry").addEventListener("click", load);

  window.addEventListener("DOMContentLoaded", () => {
    try { localStorage.removeItem("vitals-mark"); } catch { /* private mode */ }
    Chart.defaults.font.family = getComputedStyle(document.body).fontFamily;
    load();
    pollLive();
    setInterval(pollLive, 2000);
    setInterval(() => DATA && renderHeader(DATA), 60_000);
    // Quiet refresh so Today and Device keep up with Garmin (no loading shimmer).
    // It runs every minute, and at once when the tab comes back into view.
    let refreshing = false, refreshedAt = Date.now();
    const refresh = async () => {
      if (!DATA || document.hidden || refreshing) return;
      refreshing = true;
      try {
        const r = await fetch(summaryUrl(), { cache: "no-store" });
        if (!r.ok) return;
        const fresh = await r.json();
        for (const k of ["today_hr", "today", "personal", "battery", "pack_battery", "sync", "counts", "soc", "logs", "stress", "alerts"]) DATA[k] = fresh[k];
        refreshedAt = Date.now();
        renderHeader(DATA);
        if (PERIOD === "today") { renderToday(DATA); renderDevice(DATA); }
        dispatchEvent(new CustomEvent("vitals:period", { detail: { period: PERIOD, date: ADATE } }));
      } catch { /* next tick */ } finally { refreshing = false; }
    };
    setInterval(refresh, 60_000);
    document.addEventListener("visibilitychange", () => { if (!document.hidden && Date.now() - refreshedAt > 10_000) { pollLive(); refresh(); } });
  });
})();
