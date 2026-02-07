function qs(id) { return document.getElementById(id); }

function paramsFromInputs() {
  const from = qs("from").value.trim();
  const to = qs("to").value.trim();
  const params = new URLSearchParams();
  if (from) params.set("from", from);
  if (to) params.set("to", to);
  return params;
}

function initDatePickers() {
  if (typeof flatpickr !== "function") return;
  const common = {
    enableTime: true,
    time_24hr: true,
    enableSeconds: true,
    allowInput: true,
    dateFormat: "Y-m-d H:i:S",
  };
  flatpickr(qs("from"), {
    ...common,
  });
  flatpickr(qs("to"), {
    ...common,
  });
}

function setDefaultDateRange() {
  const now = new Date();
  const y = now.getFullYear();
  const m = String(now.getMonth() + 1).padStart(2, "0");
  const d = String(now.getDate()).padStart(2, "0");
  qs("from").value = `${y}-${m}-${d} 00:00:00`;
  qs("to").value = `${y}-${m}-${d} 23:59:59`;
}

async function loadVersion() {
  try {
    const resp = await fetch("/api/version");
    const data = await resp.json();
    const el = qs("app-version");
    if (el) el.textContent = data.version ?? "dev";
  } catch {
    const el = qs("app-version");
    if (el) el.textContent = "dev";
  }
}

function formatSeconds(sec) {
  const s = Math.floor(sec);
  const h = Math.floor(s / 3600);
  const m = Math.floor((s % 3600) / 60);
  const r = s % 60;
  if (h > 0) return `${h}h ${m}m ${r}s`;
  if (m > 0) return `${m}m ${r}s`;
  return `${r}s`;
}

let speedChart;
let currentAvgMbps = 0;
let lastLoadedConfig = null;
let lastIsUp = null;
let lastSpeedTestId = null;
let lastSpeedtestRunning = null;
let timeSeriesRefreshInFlight = null;
let lastTimeSeriesRefreshAtMs = 0;
const AUTO_SERIES_REFRESH_MS = 30_000;
let cfgDirty = false;

// Schedule data
let pingSchedules = [];
let speedSchedules = [];
const DAYS = ["Pn", "Wt", "Śr", "Cz", "Pt", "So", "Nd"];

function setCfgDirty(isDirty) {
  cfgDirty = Boolean(isDirty);
  const btn = qs("cfg-save");
  if (!btn) return;
  btn.classList.toggle("btn-dirty", cfgDirty);
}

function normalizeNum(v) {
  const n = Number(v);
  return Number.isFinite(n) ? n : null;
}

function currentConfigDraft() {
  return {
    connect_target: qs("cfg-connect-target").value.trim(),
    connect_interval_seconds: normalizeNum(qs("cfg-connect-interval").value),
    speedtest_mode: selectedSpeedtestMode(),
    speedtest_url: qs("cfg-speed-url").value.trim(),
    speedtest_interval_seconds: normalizeNum(qs("cfg-speed-interval").value),
    speedtest_duration_seconds: normalizeNum(qs("cfg-speed-duration").value),
    connectivity_check_buffer_seconds: normalizeNum(qs("cfg-ping-buffer-seconds").value),
    connectivity_check_buffer_max: normalizeNum(qs("cfg-ping-buffer-max").value),
    ping_enabled: qs("cfg-ping-enabled").checked,
    speed_enabled: qs("cfg-speed-enabled").checked,
    ping_schedules: JSON.stringify(pingSchedules),
    speed_schedules: JSON.stringify(speedSchedules),
    telemetry_enabled: qs("cfg-telemetry-enabled").checked,
  };
}

function isDraftDifferent(draft, cfg) {
  if (!cfg) return false;
  if ((cfg.connect_target ?? "").trim() !== (draft.connect_target ?? "").trim()) return true;
  if (normalizeNum(cfg.connect_interval_seconds) !== normalizeNum(draft.connect_interval_seconds)) return true;
  if ((cfg.speedtest_mode ?? "speedtest.net") !== (draft.speedtest_mode ?? "speedtest.net")) return true;
  if ((cfg.speedtest_url ?? "").trim() !== (draft.speedtest_url ?? "").trim()) return true;
  if (normalizeNum(cfg.speedtest_interval_seconds) !== normalizeNum(draft.speedtest_interval_seconds)) return true;
  if (normalizeNum(cfg.speedtest_duration_seconds) !== normalizeNum(draft.speedtest_duration_seconds)) return true;
  if (normalizeNum(cfg.connectivity_check_buffer_seconds) !== normalizeNum(draft.connectivity_check_buffer_seconds)) return true;
  if (normalizeNum(cfg.connectivity_check_buffer_max) !== normalizeNum(draft.connectivity_check_buffer_max)) return true;
  if (Boolean(cfg.ping_enabled) !== Boolean(draft.ping_enabled)) return true;
  if (Boolean(cfg.speed_enabled) !== Boolean(draft.speed_enabled)) return true;
  if ((cfg.ping_schedules ?? "[]") !== (draft.ping_schedules ?? "[]")) return true;
  if ((cfg.speed_schedules ?? "[]") !== (draft.speed_schedules ?? "[]")) return true;
  if (Boolean(cfg.telemetry_enabled) !== Boolean(draft.telemetry_enabled)) return true;
  return false;
}

function updateCfgDirty() {
  const draft = currentConfigDraft();
  setCfgDirty(isDraftDifferent(draft, lastLoadedConfig));
}

async function loadConfig() {
  const resp = await fetch("/api/config");
  const cfg = await resp.json();
  lastLoadedConfig = cfg;
  qs("cfg-connect-target").value = cfg.connect_target ?? "";
  qs("cfg-connect-interval").value = cfg.connect_interval_seconds ?? 1;
  const speedMode = cfg.speedtest_mode ?? "speedtest.net";
  const radios = document.querySelectorAll("input[name='speedtest-mode']");
  for (const r of radios) r.checked = (r.value === speedMode);
  qs("cfg-speed-url").value = cfg.speedtest_url ?? "";
  qs("cfg-speed-interval").value = cfg.speedtest_interval_seconds ?? 900;
  qs("cfg-speed-duration").value = cfg.speedtest_duration_seconds ?? 10;
  qs("cfg-ping-buffer-seconds").value = cfg.connectivity_check_buffer_seconds ?? 600;
  qs("cfg-ping-buffer-max").value = cfg.connectivity_check_buffer_max ?? 300;

  // Enable/disable toggles
  qs("cfg-ping-enabled").checked = cfg.ping_enabled !== false;
  qs("cfg-speed-enabled").checked = cfg.speed_enabled !== false;
  qs("cfg-telemetry-enabled").checked = cfg.telemetry_enabled !== false;
  updateSectionState("ping");
  updateSectionState("speed");

  // Schedules
  try {
    pingSchedules = JSON.parse(cfg.ping_schedules || "[]");
  } catch { pingSchedules = []; }
  try {
    speedSchedules = JSON.parse(cfg.speed_schedules || "[]");
  } catch { speedSchedules = []; }
  renderScheduleList("ping");
  renderScheduleList("speed");

  applySpeedtestModeUi(speedMode);
  setCfgDirty(false);
}

function updateSectionState(type) {
  const checkbox = qs(`cfg-${type}-enabled`);
  const section = checkbox.closest(".settings-section");
  if (checkbox.checked) {
    section.classList.remove("disabled");
  } else {
    section.classList.add("disabled");
  }
}

function renderScheduleList(type) {
  const list = qs(`${type}-schedule-list`);
  const schedules = type === "ping" ? pingSchedules : speedSchedules;

  if (schedules.length === 0) {
    list.innerHTML = '<div class="schedule-empty">Brak blokad - testy wykonywane zawsze</div>';
    return;
  }

  list.innerHTML = "";
  schedules.forEach((sched, idx) => {
    const item = document.createElement("div");
    item.className = "schedule-item";

    // Time from
    const timeFrom = document.createElement("input");
    timeFrom.type = "time";
    timeFrom.value = sched.from || "00:00";
    timeFrom.addEventListener("change", () => {
      sched.from = timeFrom.value;
      updateCfgDirty();
    });

    // Time to
    const timeTo = document.createElement("input");
    timeTo.type = "time";
    timeTo.value = sched.to || "23:59";
    timeTo.addEventListener("change", () => {
      sched.to = timeTo.value;
      updateCfgDirty();
    });

    // Days selection
    const daysDiv = document.createElement("div");
    daysDiv.className = "days-select";
    DAYS.forEach((day, dayIdx) => {
      const btn = document.createElement("button");
      btn.type = "button";
      btn.className = "day-btn" + (sched.days?.includes(dayIdx) ? " active" : "");
      btn.textContent = day;
      btn.addEventListener("click", () => {
        if (!sched.days) sched.days = [];
        const idx2 = sched.days.indexOf(dayIdx);
        if (idx2 >= 0) {
          sched.days.splice(idx2, 1);
          btn.classList.remove("active");
        } else {
          sched.days.push(dayIdx);
          btn.classList.add("active");
        }
        updateCfgDirty();
      });
      daysDiv.appendChild(btn);
    });

    // Remove button
    const removeBtn = document.createElement("button");
    removeBtn.type = "button";
    removeBtn.className = "btn-remove";
    removeBtn.textContent = "×";
    removeBtn.addEventListener("click", () => {
      if (type === "ping") {
        pingSchedules.splice(idx, 1);
      } else {
        speedSchedules.splice(idx, 1);
      }
      renderScheduleList(type);
      updateCfgDirty();
    });

    item.appendChild(timeFrom);
    item.appendChild(timeTo);
    item.appendChild(daysDiv);
    item.appendChild(removeBtn);
    list.appendChild(item);
  });
}

function addSchedule(type) {
  const newSched = { from: "00:00", to: "23:59", days: [] };
  if (type === "ping") {
    pingSchedules.push(newSched);
  } else {
    speedSchedules.push(newSched);
  }
  renderScheduleList(type);
  updateCfgDirty();
}

function setCfgMsg(text, ok) {
  const el = qs("cfg-msg");
  el.textContent = text;
  el.style.color = ok ? "rgba(34,197,94,.95)" : "rgba(239,68,68,.95)";
  setTimeout(() => { el.textContent = ""; el.style.color = ""; }, 3500);
}

function selectedSpeedtestMode() {
  const el = document.querySelector("input[name='speedtest-mode']:checked");
  return el ? el.value : "speedtest.net";
}

function applySpeedtestModeUi(mode) {
  const input = qs("cfg-speed-url");
  const label = input.closest("label");
  label.style.display = (mode === "url") ? "" : "none";
}

async function saveConfig() {
  const connectTarget = qs("cfg-connect-target").value.trim();
  if (!connectTarget) {
    setCfgMsg("Podaj adres do testu internetu.", false);
    return;
  }

  const speedMode = selectedSpeedtestMode();
  applySpeedtestModeUi(speedMode);

  const payload = {
    connect_target: connectTarget,
    connect_interval_seconds: Number(qs("cfg-connect-interval").value),
    speedtest_mode: speedMode,
    speedtest_url: qs("cfg-speed-url").value.trim(),
    speedtest_interval_seconds: Number(qs("cfg-speed-interval").value),
    speedtest_duration_seconds: Number(qs("cfg-speed-duration").value),
    connectivity_check_buffer_seconds: Number(qs("cfg-ping-buffer-seconds").value),
    connectivity_check_buffer_max: Number(qs("cfg-ping-buffer-max").value),
    ping_enabled: qs("cfg-ping-enabled").checked,
    speed_enabled: qs("cfg-speed-enabled").checked,
    ping_schedules: JSON.stringify(pingSchedules),
    speed_schedules: JSON.stringify(speedSchedules),
    telemetry_enabled: qs("cfg-telemetry-enabled").checked,
  };
  const resp = await fetch("/api/config", {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
  if (!resp.ok) {
    const text = await resp.text();
    setCfgMsg(`Błąd zapisu: ${text}`, false);
    return;
  }
  const newCfg = await resp.json();
  setCfgMsg("Zapisano.", true);
  const modeChanged = (lastLoadedConfig?.speedtest_mode ?? "speedtest.net") !== (newCfg?.speedtest_mode ?? "speedtest.net");
  const urlChanged = (lastLoadedConfig?.speedtest_url ?? "") !== (newCfg?.speedtest_url ?? "");
  const intervalChanged = Number(lastLoadedConfig?.speedtest_interval_seconds ?? 0) !== Number(newCfg?.speedtest_interval_seconds ?? 0);
  lastLoadedConfig = newCfg;
  setCfgDirty(false);

  if (modeChanged || urlChanged || intervalChanged) {
    // po zmianie typu/usługi testu prędkości uruchom od razu pomiar
    fetch("/api/speedtest/run", { method: "POST" }).catch(() => {});
  }
  await refreshAll();
}

function parseIsoToMs(iso) {
  if (!iso) return null;
  const s = String(iso).trim();
  // YYYY-MM-DD HH:MM:SS (local) or YYYY-MM-DDTHH:MM:SS (local)
  const m = /^(\d{4})-(\d{2})-(\d{2})[ T](\d{2}):(\d{2}):(\d{2})$/.exec(s);
  if (m) {
    const [, yy, mo, dd, hh, mm, ss] = m;
    const d = new Date(
      Number(yy),
      Number(mo) - 1,
      Number(dd),
      Number(hh),
      Number(mm),
      Number(ss),
    );
    const ms = d.getTime();
    return Number.isFinite(ms) ? ms : null;
  }
  const ms = Date.parse(s.replace(" ", "T"));
  return Number.isFinite(ms) ? ms : null;
}

function isOnlineAt(outageIntervals, tMs) {
  if (tMs == null) return null;
  for (const it of outageIntervals) {
    if (tMs >= it.startMs && tMs <= it.endMs) return 0;
  }
  return 1;
}

function normalizeIsoKey(iso) {
  // Normalize ISO string to consistent format for Map keys: "YYYY-MM-DD HH:MM:SS"
  if (!iso) return null;
  const s = String(iso).trim().replace("T", " ");
  // Remove any timezone suffix and keep only date/time part
  const m = /^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})/.exec(s);
  return m ? m[1] : s;
}

function uniqueSortedIso(values) {
  const map = new Map();
  for (const v of values) {
    if (!v) continue;
    const ms = parseIsoToMs(v);
    if (ms == null) continue;
    const key = normalizeIsoKey(v);
    if (!key) continue;
    // Store normalized key with ms for sorting, keep original for display
    if (!map.has(key)) {
      map.set(key, { ms, display: v });
    }
  }
  return [...map.entries()]
    .sort((a, b) => a[1].ms - b[1].ms)
    .map(([key]) => key);
}

async function loadStatus() {
  const resp = await fetch("/api/status");
  const data = await resp.json();

  const badgeOnline = qs("badge-online");
  const badgeLast = qs("badge-last");

  const isUp = data.connectivity?.is_up === 1;
  const lastSpeedId = (typeof data.last_speed_test?.id === "number") ? data.last_speed_test.id : null;
  badgeOnline.textContent = isUp ? "Online" : "Offline";
  badgeOnline.className = `badge ${isUp ? "badge-ok" : "badge-bad"}`;

  if (data.speedtest_running) {
    badgeLast.style.display = "";
    badgeLast.textContent = "Trwa pomiar prędkości…";
    badgeLast.className = "badge badge-gray";
  } else {
    const last = data.last_speed_test || null;
    const ok = data.last_speed_test_ok || null;

    const formatOk = (it) => {
      const dl = (it.mbps ?? 0);
      const ul = it.upload_mbps;
      const ping = it.ping_ms;
      const mode = it.speedtest_mode || data.config?.speedtest_mode || "";
      const parts = [`${dl.toFixed(1)}↓ Mbps`];
      if (typeof ul === "number" && Number.isFinite(ul) && ul > 0) parts.push(`${ul.toFixed(1)}↑ Mbps`);
      if (typeof ping === "number" && Number.isFinite(ping) && ping > 0) parts.push(`ping ${ping.toFixed(0)} ms`);
      const srv = it.server_name;
      const cc = it.server_country;
      if (srv) parts.push(`serwer: ${srv}${cc ? " (" + cc + ")" : ""}`);
      if (mode && mode !== "url") parts.push(`tryb: ${mode}`);
      return parts.join(" · ");
    };

    if (last?.started_at && last.error) {
      // pokaż, że test się odbył, ale bez szczegółów błędu
      const time = String(last.started_at).replace("T", " ");
      if (ok?.started_at) {
        badgeLast.textContent = `Ostatni test: ${time} (nieudany) · Ostatni udany: ${formatOk(ok)}`;
      } else {
        badgeLast.textContent = `Ostatni test: ${time} (nieudany)`;
      }
      badgeLast.className = "badge badge-gray";
      badgeLast.style.display = "";
      return;
    }

    if (ok?.started_at) {
      badgeLast.textContent = `Ostatni udany test: ${formatOk(ok)}`;
      badgeLast.className = "badge badge-ok";
      badgeLast.style.display = "";
      return;
    }

    badgeLast.textContent = "";
    badgeLast.style.display = "none";
  }

  const speedtestRunning = Boolean(data.speedtest_running);
  const connectivityChanged = (lastIsUp !== null) && (isUp !== lastIsUp);
  const speedTestChanged = (lastSpeedTestId !== null) && (lastSpeedId !== null) && (lastSpeedId !== lastSpeedTestId);
  const speedtestRunningChanged = (lastSpeedtestRunning !== null) && (speedtestRunning !== lastSpeedtestRunning);
  lastIsUp = isUp;
  lastSpeedTestId = lastSpeedId;
  lastSpeedtestRunning = speedtestRunning;

  const followNow = !qs("to").value.trim();
  const dueToTime = followNow && lastTimeSeriesRefreshAtMs && (Date.now() - lastTimeSeriesRefreshAtMs > AUTO_SERIES_REFRESH_MS);
  if (connectivityChanged || speedTestChanged || speedtestRunningChanged || dueToTime) {
    scheduleTimeSeriesRefresh();
  }
}

function buildOnlineDataPoints(outageIntervals, rangeFromMs, rangeToMs) {
  // Generate online status data points with proper time-based representation.
  // For each outage, we create 4 points: before-start (online), at-start (offline),
  // at-end (offline), after-end (online).
  // This ensures the offline period is accurately represented in time.
  const points = [];
  const ONLINE_Y = 0.8; // Scale to not overlap with speed average line

  // Sort outages by start time
  const sorted = [...outageIntervals].sort((a, b) => a.startMs - b.startMs);

  if (sorted.length === 0) {
    // No outages - just show online for entire range
    points.push({ x: rangeFromMs, y: ONLINE_Y });
    points.push({ x: rangeToMs, y: ONLINE_Y });
    return points;
  }

  // Start with online at range beginning (if before first outage)
  if (sorted[0].startMs > rangeFromMs) {
    points.push({ x: rangeFromMs, y: ONLINE_Y });
  }

  for (let i = 0; i < sorted.length; i++) {
    const outage = sorted[i];
    const startMs = Math.max(outage.startMs, rangeFromMs);
    const endMs = Math.min(outage.endMs, rangeToMs);

    // Point just before outage (online) - only if there's a gap
    if (startMs > rangeFromMs && (points.length === 0 || points[points.length - 1].x < startMs - 1)) {
      points.push({ x: startMs - 1, y: ONLINE_Y });
    }

    // Outage start (transition to offline)
    points.push({ x: startMs, y: 0 });

    // Outage end (still offline)
    points.push({ x: endMs, y: 0 });

    // Point just after outage (back online)
    if (endMs < rangeToMs) {
      points.push({ x: endMs + 1, y: ONLINE_Y });
    }
  }

  // End with online at range end (if after last outage)
  const lastOutageEnd = sorted[sorted.length - 1].endMs;
  if (lastOutageEnd < rangeToMs && (points.length === 0 || points[points.length - 1].x < rangeToMs)) {
    points.push({ x: rangeToMs, y: ONLINE_Y });
  }

  return points;
}

function buildBlockedDataPoints(blockedIntervals, rangeFromMs, rangeToMs, speedDataPoints) {
  // Generate blocked periods data points as a dashed line connecting
  // the last speed test before the blocked period to the first speed test after.
  // speedDataPoints must be sorted by x (timestamp).
  const points = [];

  if (!blockedIntervals || blockedIntervals.length === 0) {
    return points;
  }

  const sorted = [...blockedIntervals].sort((a, b) => a.startMs - b.startMs);
  const speeds = speedDataPoints || [];

  for (const block of sorted) {
    const startMs = Math.max(block.startMs, rangeFromMs);
    const endMs = Math.min(block.endMs, rangeToMs);

    if (startMs >= endMs) continue;

    // Find last speed data point before (or at) the block start
    let lastBefore = null;
    for (let i = speeds.length - 1; i >= 0; i--) {
      if (speeds[i].x <= startMs) {
        lastBefore = speeds[i];
        break;
      }
    }

    // Find first speed data point after (or at) the block end
    let firstAfter = null;
    for (let i = 0; i < speeds.length; i++) {
      if (speeds[i].x >= endMs) {
        firstAfter = speeds[i];
        break;
      }
    }

    // Build segment: from last test before → to first test after
    const segStart = lastBefore ? { x: lastBefore.x, y: lastBefore.y } : { x: startMs, y: 0.4 };
    const segEnd = firstAfter ? { x: firstAfter.x, y: firstAfter.y } : { x: endMs, y: 0.4 };

    // Add null gap between segments
    if (points.length > 0) {
      points.push({ x: segStart.x - 1, y: null });
    }
    points.push(segStart);
    points.push(segEnd);
    points.push({ x: segEnd.x + 1, y: null });
  }

  return points;
}

async function loadChart() {
  const params = paramsFromInputs();
  const [speedResp, outagesResp, blockedPingResp, blockedSpeedResp] = await Promise.all([
    fetch(`/api/speed?${params.toString()}`),
    fetch(`/api/outages?${params.toString()}`),
    fetch(`/api/blocked-periods?${params.toString()}&test_type=ping`),
    fetch(`/api/blocked-periods?${params.toString()}&test_type=speed`),
  ]);
  const speedData = await speedResp.json();
  const outagesData = await outagesResp.json();
  const blockedPingData = await blockedPingResp.json();
  const blockedSpeedData = await blockedSpeedResp.json();
  const items = speedData.items || [];
  const outages = outagesData.items || [];
  const blockedPing = blockedPingData.items || [];
  const blockedSpeed = blockedSpeedData.items || [];

  const rangeFrom = speedData?.range?.from || outagesData?.range?.from || null;
  const rangeTo = speedData?.range?.to || outagesData?.range?.to || null;
  const rangeFromMs = parseIsoToMs(rangeFrom);
  const rangeToMs = parseIsoToMs(rangeTo);

  const outageIntervals = outages
    .map(o => ({ startMs: parseIsoToMs(o.started_at), endMs: parseIsoToMs(o.ended_at) }))
    .filter(o => o.startMs != null && o.endMs != null);

  const blockedPingIntervals = blockedPing
    .map(b => ({ startMs: parseIsoToMs(b.started_at), endMs: parseIsoToMs(b.ended_at) }))
    .filter(b => b.startMs != null && b.endMs != null);

  const blockedSpeedIntervals = blockedSpeed
    .map(b => ({ startMs: parseIsoToMs(b.started_at), endMs: parseIsoToMs(b.ended_at) }))
    .filter(b => b.startMs != null && b.endMs != null);

  const okSpeeds = items.filter(i => !i.error && typeof i.mbps === "number" && i.mbps > 0).map(i => i.mbps);
  const avgMbps = okSpeeds.length ? (okSpeeds.reduce((a, b) => a + b, 0) / okSpeeds.length) : 0;
  currentAvgMbps = avgMbps;

  // Build speed data points with timestamps
  const speedDataPoints = items
    .filter(it => !it.error && typeof it.mbps === "number" && it.mbps > 0 && avgMbps > 0)
    .map(it => {
      const tMs = parseIsoToMs(it.started_at);
      return {
        x: tMs,
        y: it.mbps / avgMbps,
        mbps: it.mbps,
      };
    })
    .filter(p => p.x != null)
    .sort((a, b) => a.x - b.x);

  // Build online status data points (time-accurate)
  const onlineDataPoints = buildOnlineDataPoints(outageIntervals, rangeFromMs, rangeToMs);

  // Build blocked periods data points (pass speed data so the line connects between tests)
  const blockedPingPoints = buildBlockedDataPoints(blockedPingIntervals, rangeFromMs, rangeToMs, speedDataPoints);
  const blockedSpeedPoints = buildBlockedDataPoints(blockedSpeedIntervals, rangeFromMs, rangeToMs, speedDataPoints);

  // Insert null gaps into speed data where speedtest was blocked,
  // so the purple line breaks and doesn't overlap the red dashed line.
  if (blockedSpeedIntervals.length > 0) {
    const sortedBlocked = [...blockedSpeedIntervals].sort((a, b) => a.startMs - b.startMs);
    for (let bi = sortedBlocked.length - 1; bi >= 0; bi--) {
      const bStart = Math.max(sortedBlocked[bi].startMs, rangeFromMs);
      const bEnd = Math.min(sortedBlocked[bi].endMs, rangeToMs);
      if (bStart >= bEnd) continue;

      // Find insert position: after the last point <= bStart
      let insertIdx = speedDataPoints.length;
      for (let i = 0; i < speedDataPoints.length; i++) {
        if (speedDataPoints[i].x > bStart) {
          insertIdx = i;
          break;
        }
      }
      // Insert null to break the line
      speedDataPoints.splice(insertIdx, 0, { x: bStart + 1, y: null });
    }
  }

  const ctx = qs("speedChart").getContext("2d");
  const maxRel = Math.max(
    1.2,
    ...speedDataPoints.map(p => p.y).filter(v => Number.isFinite(v)),
    ...onlineDataPoints.map(p => p.y).filter(v => Number.isFinite(v)),
  );
  const relMax = Math.max(1.2, maxRel * 1.15);

  const datasets = [{
    label: "Prędkość (Mbps)",
    yAxisID: "yMbps",
    data: speedDataPoints,
    borderColor: "rgba(99, 102, 241, 0.95)",
    backgroundColor: "rgba(99, 102, 241, 0.2)",
    borderWidth: 2,
    pointRadius: 1.5,
    tension: 0.25,
    spanGaps: false,
  }, {
    label: "Online (0/1)",
    yAxisID: "yRel",
    data: onlineDataPoints,
    borderColor: "rgba(34, 197, 94, 0.95)",
    backgroundColor: "rgba(34, 197, 94, 0.15)",
    borderWidth: 2,
    pointRadius: 0,
    stepped: true,
    fill: true,
    spanGaps: false,
  }];

  // Add blocked ping dataset if there are blocked periods
  if (blockedPingPoints.length > 0) {
    datasets.push({
      label: "Ping wył.",
      yAxisID: "yRel",
      data: blockedPingPoints,
      borderColor: "rgba(245, 158, 11, 0.8)",
      backgroundColor: "rgba(245, 158, 11, 0.1)",
      borderWidth: 2,
      borderDash: [6, 4],
      pointRadius: 0,
      fill: false,
      spanGaps: false,
    });
  }

  // Add blocked speed dataset if there are blocked periods
  if (blockedSpeedPoints.length > 0) {
    datasets.push({
      label: "Speedtest wył.",
      yAxisID: "yMbps",
      data: blockedSpeedPoints,
      borderColor: "rgba(239, 68, 68, 0.8)",
      backgroundColor: "rgba(239, 68, 68, 0.1)",
      borderWidth: 2,
      borderDash: [6, 4],
      pointRadius: 0,
      fill: false,
      spanGaps: false,
    });
  }

  const chartConfig = {
    type: "line",
    data: { datasets },
    options: {
      responsive: true,
      scales: {
        x: {
          type: "time",
          time: {
            displayFormats: {
              hour: "HH:mm",
              minute: "HH:mm",
              second: "HH:mm:ss",
              day: "MM-dd",
            },
            tooltipFormat: "yyyy-MM-dd HH:mm:ss",
          },
          min: rangeFromMs,
          max: rangeToMs,
          ticks: { color: "rgba(194,204,240,.92)", maxRotation: 0, autoSkip: true },
          grid: {
            color: "rgba(231,236,255,.10)",
            tickColor: "rgba(231,236,255,.10)",
          },
        },
        yRel: {
          position: "left",
          min: 0,
          max: relMax,
          ticks: {
            color: "rgba(169,180,221,.8)",
            callback: (v) => (v === 0 || v === 1 ? String(v) : ""),
          },
          grid: { color: "rgba(231,236,255,.06)" },
          title: { display: true, text: "Online (0/1)", color: "rgba(169,180,221,.9)" },
        },
        yMbps: {
          position: "right",
          min: 0,
          max: relMax,
          grid: { drawOnChartArea: false },
          ticks: {
            color: "rgba(169,180,221,.8)",
            callback: (v) => {
              if (!currentAvgMbps || !Number.isFinite(currentAvgMbps)) return "";
              return (Number(v) * currentAvgMbps).toFixed(0);
            },
          },
          title: { display: true, text: "Prędkość (Mbps)", color: "rgba(169,180,221,.9)" },
        },
      },
      plugins: {
        legend: {
          labels: {
            color: "rgba(231,236,255,.9)",
            usePointStyle: true,
            pointStyleWidth: 40,
            generateLabels: (chart) => {
              return chart.data.datasets.map((ds, i) => {
                // Create a small canvas for each legend icon to draw a line (solid or dashed)
                const cvs = document.createElement("canvas");
                cvs.width = 40;
                cvs.height = 16;
                const c = cvs.getContext("2d");
                c.strokeStyle = ds.borderColor;
                c.lineWidth = ds.borderWidth || 2;
                c.setLineDash(ds.borderDash || []);
                c.beginPath();
                c.moveTo(0, 8);
                c.lineTo(40, 8);
                c.stroke();
                return {
                  text: ds.label,
                  pointStyle: cvs,
                  fillStyle: "transparent",
                  strokeStyle: "transparent",
                  hidden: !chart.isDatasetVisible(i),
                  datasetIndex: i,
                };
              });
            },
          },
        },
        tooltip: {
          callbacks: {
            label: (ctx) => {
              if (ctx.dataset?.label?.startsWith("Prędkość")) {
                const mbps = ctx.raw && typeof ctx.raw === "object" ? ctx.raw.mbps : null;
                if (typeof mbps === "number" && Number.isFinite(mbps)) return `Prędkość: ${mbps.toFixed(2)} Mbps`;
                return "Prędkość: brak";
              }
              if (ctx.dataset?.label?.startsWith("Online")) {
                const v = ctx.raw && typeof ctx.raw === "object" ? ctx.raw.y : ctx.raw;
                return `Online: ${v > 0 ? "1" : "0"}`;
              }
              if (ctx.dataset?.label?.includes("wył.")) {
                return `${ctx.dataset.label}: pomiar wyłączony`;
              }
              return ctx.formattedValue;
            }
          }
        }
      }
    }
  };

  if (!speedChart) {
    speedChart = new Chart(ctx, chartConfig);
  } else {
    speedChart.data.datasets = datasets;
    speedChart.options.scales.x.min = rangeFromMs;
    speedChart.options.scales.x.max = rangeToMs;
    speedChart.options.scales.yRel.max = relMax;
    speedChart.options.scales.yMbps.max = relMax;
    speedChart.update();
  }
}

async function loadQuality() {
  const params = paramsFromInputs();
  const resp = await fetch(`/api/report/quality?${params.toString()}`);
  const data = await resp.json();

  qs("q-incidents").textContent = `${data.incident_count ?? 0}`;
  qs("q-downtime").textContent = formatSeconds(data.downtime_seconds ?? 0);
  qs("q-percent").textContent = `${(data.downtime_percent ?? 0).toFixed(3)}%`;
}

async function loadOutagesList() {
  const params = paramsFromInputs();
  const resp = await fetch(`/api/outages?${params.toString()}`);
  const data = await resp.json();
  const items = data.items || [];

  const el = qs("outagesList");
  el.innerHTML = "";
  if (items.length === 0) {
    const div = document.createElement("div");
    div.className = "outage";
    div.textContent = "Brak awarii w wybranym zakresie.";
    el.appendChild(div);
    return;
  }
  for (const it of items) {
    const div = document.createElement("div");
    div.className = "outage";
    div.textContent = `Od: ${it.started_at}  Do: ${it.ended_at}`;
    el.appendChild(div);
  }
}

function refreshExports() {
  const params = paramsFromInputs();
  const q = params.toString();
  qs("export-speed").href = `/api/export/speed.csv${q ? "?" + q : ""}`;
  qs("export-outages").href = `/api/export/outages.csv${q ? "?" + q : ""}`;
  qs("export-pings").href = `/api/export/pings.csv${q ? "?" + q : ""}`;
}

async function refreshTimeSeries() {
  refreshExports();
  await Promise.all([loadChart(), loadQuality(), loadOutagesList()]);
}

function scheduleTimeSeriesRefresh() {
  if (timeSeriesRefreshInFlight) return;
  timeSeriesRefreshInFlight = (async () => {
    try {
      await refreshTimeSeries();
      lastTimeSeriesRefreshAtMs = Date.now();
    } finally {
      timeSeriesRefreshInFlight = null;
    }
  })();
}

async function refreshAll() {
  refreshExports();
  await Promise.all([loadStatus(), loadChart(), loadQuality(), loadOutagesList()]);
  lastTimeSeriesRefreshAtMs = Date.now();
}

qs("refresh").addEventListener("click", refreshAll);
qs("cfg-save").addEventListener("click", saveConfig);
for (const r of document.querySelectorAll("input[name='speedtest-mode']")) {
  r.addEventListener("change", () => {
    applySpeedtestModeUi(selectedSpeedtestMode());
    updateCfgDirty();
  });
}

 (async () => {
  initDatePickers();
  setDefaultDateRange();
  loadVersion();
  await loadConfig();
  await refreshAll();
  setInterval(loadStatus, 2000);
})().catch((e) => {
  console.error(e);
});

function getSettingsDialog() {
  return qs("settings-modal");
}

function openSettings() {
  const dlg = getSettingsDialog();
  if (!dlg) return;
  if (typeof dlg.showModal === "function") dlg.showModal();
  else dlg.setAttribute("open", "open");
}

function closeSettings() {
  const dlg = getSettingsDialog();
  if (!dlg) return;
  if (typeof dlg.close === "function") dlg.close();
  else dlg.removeAttribute("open");
}

qs("open-settings")?.addEventListener("click", openSettings);
qs("close-settings")?.addEventListener("click", closeSettings);
qs("dismiss-settings")?.addEventListener("click", closeSettings);

getSettingsDialog()?.addEventListener("click", (e) => {
  const dlg = getSettingsDialog();
  if (dlg && e.target === dlg) closeSettings();
});

for (const el of [
  qs("cfg-connect-target"),
  qs("cfg-connect-interval"),
  qs("cfg-speed-url"),
  qs("cfg-speed-interval"),
  qs("cfg-speed-duration"),
  qs("cfg-ping-buffer-seconds"),
  qs("cfg-ping-buffer-max"),
]) {
  el?.addEventListener("input", updateCfgDirty);
}

// Toggle switches
qs("cfg-ping-enabled")?.addEventListener("change", () => {
  updateSectionState("ping");
  updateCfgDirty();
});
qs("cfg-speed-enabled")?.addEventListener("change", () => {
  updateSectionState("speed");
  updateCfgDirty();
});
qs("cfg-telemetry-enabled")?.addEventListener("change", updateCfgDirty);

// Schedule buttons
qs("add-ping-schedule")?.addEventListener("click", () => addSchedule("ping"));
qs("add-speed-schedule")?.addEventListener("click", () => addSchedule("speed"));

// ============================================================
// Network Tools Section
// ============================================================

const TOOL_GROUPS = [
  {
    id: "dns", label: "DNS",
    tools: [
      {
        name: "dns_resolve_time",
        label: "DNS Resolve Time",
        desc: "Zmierz czas rozwiazywania nazwy domeny przez konkretny serwer DNS",
        params: [
          { name: "domain", label: "Domena", type: "text", default: "google.com" },
          { name: "dns_server", label: "Serwer DNS", type: "text", default: "8.8.8.8" },
          { name: "record_type", label: "Typ rekordu", type: "select", options: ["A","AAAA","MX","CNAME","TXT","NS"], default: "A" },
        ],
      },
      {
        name: "dns_propagation",
        label: "DNS Propagation Check",
        desc: "Sprawdz wynik DNS na wielu publicznych serwerach (Cloudflare, Google, Quad9, OpenDNS...)",
        params: [
          { name: "domain", label: "Domena", type: "text", default: "example.com" },
          { name: "record_type", label: "Typ rekordu", type: "select", options: ["A","AAAA","MX","CNAME","TXT"], default: "A" },
        ],
      },
      {
        name: "dns_leak_test",
        label: "DNS Leak Test",
        desc: "Wykryj, ktore serwery DNS sa faktycznie uzywane przez Twoje polaczenie",
        params: [],
      },
      {
        name: "dns_doh_dot_status",
        label: "DNS over HTTPS/TLS",
        desc: "Sprawdz dostepnosc DoH i DoT na podanym serwerze DNS",
        params: [
          { name: "dns_server", label: "Serwer DNS", type: "text", default: "1.1.1.1" },
        ],
      },
      {
        name: "dns_server_comparison",
        label: "Porownanie serwerow DNS",
        desc: "Benchmark wielu serwerow DNS — sredni, min i max czas odpowiedzi",
        params: [
          { name: "domain", label: "Domena", type: "text", default: "google.com" },
          { name: "iterations", label: "Iteracje", type: "number", default: "5", min: 1, max: 20 },
        ],
      },
    ],
  },
  {
    id: "routing", label: "Routing i sciezka sieciowa",
    tools: [
      {
        name: "traceroute",
        label: "Traceroute",
        desc: "Pokaz sciezke pakietow do celu z opoznieniem na kazdym hop-ie",
        params: [
          { name: "target", label: "Cel", type: "text", default: "google.com" },
          { name: "max_hops", label: "Max hops", type: "number", default: "30", min: 1, max: 40 },
        ],
      },
      {
        name: "mtr",
        label: "MTR (My Traceroute)",
        desc: "Ciagly traceroute ze statystykami strat pakietow na kazdym hop-ie",
        params: [
          { name: "target", label: "Cel", type: "text", default: "google.com" },
          { name: "count", label: "Liczba prob", type: "number", default: "10", min: 1, max: 100 },
        ],
      },
      {
        name: "bgp_as_path",
        label: "BGP/AS Path Lookup",
        desc: "Sprawdz ASN, nazwe sieci i prefix CIDR dla podanego IP",
        params: [
          { name: "target", label: "IP lub host", type: "text", default: "8.8.8.8" },
        ],
      },
      {
        name: "reverse_dns",
        label: "Reverse DNS Lookup",
        desc: "Wyszukaj nazwe hosta (PTR) dla podanego adresu IP",
        params: [
          { name: "ip", label: "Adres IP", type: "text", default: "8.8.8.8" },
        ],
      },
    ],
  },
  {
    id: "connection", label: "Info o polaczeniu",
    tools: [
      {
        name: "mtu_discovery",
        label: "MTU Discovery",
        desc: "Odkryj maksymalny rozmiar pakietu (MTU) na sciezce do celu",
        params: [
          { name: "target", label: "Cel", type: "text", default: "google.com" },
        ],
      },
      {
        name: "geoip",
        label: "Geolokalizacja IP",
        desc: "Pokaz lokalizacje geograficzna, ISP i organizacje dla IP (puste = Twoj IP)",
        params: [
          { name: "ip", label: "Adres IP (puste = Twoj)", type: "text", default: "" },
        ],
      },
      {
        name: "public_ip",
        label: "Public IP",
        desc: "Pobierz publiczny adres IP z 3 niezaleznych serwisow i sprawdz spojnosc",
        params: [],
      },
      {
        name: "nat_type",
        label: "NAT Type Detection",
        desc: "Wykryj typ NAT (Full Cone, Symmetric, etc.) przez protokol STUN",
        params: [],
      },
    ],
  },
  {
    id: "local", label: "Siec lokalna",
    tools: [
      {
        name: "port_scan",
        label: "Skaner portow TCP",
        desc: "Skanuj otwarte porty TCP na podanym hoscie (max 1000 portow)",
        params: [
          { name: "target", label: "Adres IP / host", type: "text", default: "192.168.1.1" },
          { name: "ports", label: "Porty (np. 22,80,443 lub 1-1024)", type: "text", default: "22,80,443,8080,8443,3389,21,25,53,3306,5432" },
          { name: "timeout", label: "Timeout (sek)", type: "number", default: "2", min: 1, max: 10 },
        ],
      },
      {
        name: "lan_discovery",
        label: "LAN Device Discovery",
        desc: "Skanuj podsiec w poszukiwaniu urzadzen (nmap -sn). Wymaga min /24",
        params: [
          { name: "subnet", label: "Podsiec CIDR", type: "text", default: "192.168.1.0/24" },
        ],
      },
      {
        name: "iperf_test",
        label: "Internal Speed Test (iPerf3)",
        desc: "Zmierz przepustowosc LAN do serwera iperf3. Wymaga uruchomionego serwera iperf3 na celu.",
        params: [
          { name: "server", label: "Serwer iperf3", type: "text", default: "" },
          { name: "port", label: "Port", type: "number", default: "5201", min: 1, max: 65535 },
          { name: "duration", label: "Czas (sek)", type: "number", default: "5", min: 1, max: 30 },
          { name: "direction", label: "Kierunek", type: "select", options: ["download","upload"], default: "download" },
        ],
      },
      {
        name: "dhcp_leases",
        label: "DHCP / Konfiguracja sieci",
        desc: "Pokaz dzierzawy DHCP lub aktualny config sieci kontenera (ip addr/route)",
        params: [],
      },
    ],
  },
];

// ---- Tool rendering ----

function _escHtml(str) {
  const d = document.createElement("div");
  d.textContent = String(str ?? "");
  return d.innerHTML;
}

function renderToolsSection() {
  const body = qs("tools-body");
  if (!body) return;
  body.innerHTML = "";
  for (const group of TOOL_GROUPS) {
    const gDiv = document.createElement("div");
    gDiv.className = "tool-group";

    const gH = document.createElement("h3");
    gH.className = "tool-group-header";
    gH.textContent = group.label;
    gDiv.appendChild(gH);

    const cardsDiv = document.createElement("div");
    cardsDiv.className = "tool-cards";
    for (const tool of group.tools) {
      cardsDiv.appendChild(_createToolCard(tool));
    }
    gDiv.appendChild(cardsDiv);
    body.appendChild(gDiv);
  }
}

function _createToolCard(tool) {
  const card = document.createElement("div");
  card.className = "tool-card";
  card.dataset.tool = tool.name;

  // Title
  const title = document.createElement("div");
  title.className = "tool-card-title";
  title.textContent = tool.label;
  card.appendChild(title);

  // Desc
  const desc = document.createElement("div");
  desc.className = "tool-card-desc";
  desc.textContent = tool.desc;
  card.appendChild(desc);

  // Params
  if (tool.params && tool.params.length) {
    const pDiv = document.createElement("div");
    pDiv.className = "tool-params";
    for (const p of tool.params) {
      pDiv.appendChild(_createParamInput(tool.name, p));
    }
    card.appendChild(pDiv);
  }

  // Actions row
  const actDiv = document.createElement("div");
  actDiv.className = "tool-actions";

  const runBtn = document.createElement("button");
  runBtn.type = "button";
  runBtn.className = "tool-run-btn";
  runBtn.textContent = "Uruchom";
  runBtn.addEventListener("click", () => _runTool(tool.name, card));
  actDiv.appendChild(runBtn);

  const spinner = document.createElement("span");
  spinner.className = "tool-spinner";
  spinner.style.display = "none";
  spinner.textContent = "\u23F3";
  actDiv.appendChild(spinner);

  const dur = document.createElement("span");
  dur.className = "tool-duration";
  actDiv.appendChild(dur);

  card.appendChild(actDiv);

  // Result area
  const resDiv = document.createElement("div");
  resDiv.className = "tool-result";
  resDiv.style.display = "none";
  card.appendChild(resDiv);

  return card;
}

function _createParamInput(toolName, param) {
  const wrap = document.createElement("label");
  wrap.className = "tool-param";

  const lbl = document.createElement("span");
  lbl.textContent = param.label;
  wrap.appendChild(lbl);

  if (param.type === "select") {
    const sel = document.createElement("select");
    sel.id = `tp-${toolName}-${param.name}`;
    for (const opt of (param.options || [])) {
      const o = document.createElement("option");
      o.value = opt; o.textContent = opt;
      if (opt === param.default) o.selected = true;
      sel.appendChild(o);
    }
    wrap.appendChild(sel);
  } else {
    const inp = document.createElement("input");
    inp.type = param.type || "text";
    inp.id = `tp-${toolName}-${param.name}`;
    inp.value = param.default || "";
    inp.placeholder = param.default || "";
    if (param.min !== undefined) inp.min = param.min;
    if (param.max !== undefined) inp.max = param.max;
    wrap.appendChild(inp);
  }
  return wrap;
}

async function _runTool(toolName, card) {
  const runBtn = card.querySelector(".tool-run-btn");
  const spinner = card.querySelector(".tool-spinner");
  const durEl = card.querySelector(".tool-duration");
  const resDiv = card.querySelector(".tool-result");

  // Gather params
  const params = {};
  const inputs = card.querySelectorAll(`[id^="tp-${toolName}-"]`);
  for (const inp of inputs) {
    const pName = inp.id.replace(`tp-${toolName}-`, "");
    const v = inp.value.trim();
    if (v !== "") params[pName] = inp.type === "number" ? Number(v) : v;
  }

  // Loading state
  runBtn.disabled = true;
  spinner.style.display = "";
  durEl.textContent = "";
  resDiv.style.display = "none";
  resDiv.innerHTML = "";

  try {
    const resp = await fetch(`/api/tools/${toolName}`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(params),
    });
    const data = await resp.json();

    durEl.textContent = `${data.duration_ms?.toFixed(0) ?? "?"} ms`;

    if (data.status === "error") {
      resDiv.innerHTML = `<div class="tool-error">Blad: ${_escHtml(data.error)}</div>`;
    } else {
      resDiv.innerHTML = _formatToolResult(toolName, data.result);
    }
    resDiv.style.display = "";
  } catch (e) {
    resDiv.innerHTML = `<div class="tool-error">Blad sieci: ${_escHtml(e.message)}</div>`;
    resDiv.style.display = "";
  } finally {
    runBtn.disabled = false;
    spinner.style.display = "none";
  }
}

// ---- Result formatters ----

function _formatToolResult(toolName, result) {
  if (!result) return '<div class="tool-empty">Brak wynikow</div>';
  switch (toolName) {
    case "dns_resolve_time": return _fmtDnsResolve(result);
    case "dns_propagation": return _fmtDnsPropagation(result);
    case "dns_leak_test": return _fmtDnsLeak(result);
    case "dns_doh_dot_status": return _fmtDohDot(result);
    case "dns_server_comparison": return _fmtDnsComparison(result);
    case "traceroute": return _fmtTraceroute(result);
    case "mtr": return _fmtMtr(result);
    case "bgp_as_path": return _fmtBgp(result);
    case "reverse_dns": return _fmtReverseDns(result);
    case "mtu_discovery": return _fmtMtu(result);
    case "geoip": return _fmtGeoip(result);
    case "public_ip": return _fmtPublicIp(result);
    case "nat_type": return _fmtNat(result);
    case "port_scan": return _fmtPortScan(result);
    case "lan_discovery": return _fmtLanDiscovery(result);
    case "iperf_test": return _fmtIperf(result);
    case "dhcp_leases": return _fmtDhcp(result);
    default: return _fmtGeneric(result);
  }
}

function _fmtDnsResolve(r) {
  return `<table class="tool-table">
    <tr><td class="tool-key">Domena</td><td>${_escHtml(r.domain)}</td></tr>
    <tr><td class="tool-key">Serwer DNS</td><td>${_escHtml(r.dns_server)}</td></tr>
    <tr><td class="tool-key">Typ</td><td>${_escHtml(r.record_type)}</td></tr>
    <tr><td class="tool-key">Czas</td><td><b>${r.resolve_time_ms} ms</b></td></tr>
    <tr><td class="tool-key">Odpowiedzi</td><td>${(r.answers||[]).map(a => _escHtml(a)).join("<br>")}</td></tr>
  </table>`;
}

function _fmtDnsPropagation(r) {
  const consistent = r.consistent
    ? '<span class="tool-ok">Spojne</span>'
    : '<span class="tool-bad">Niespojne</span>';
  let html = `<div style="margin-bottom:8px">Domena: <b>${_escHtml(r.domain)}</b> (${_escHtml(r.record_type)}) — ${consistent}</div>`;
  html += `<table class="tool-table"><tr><th>Serwer</th><th>IP</th><th>Czas</th><th>Odpowiedzi</th><th></th></tr>`;
  for (const s of (r.servers || [])) {
    const errTd = s.error ? `<td class="tool-bad">${_escHtml(s.error)}</td>` : "<td></td>";
    html += `<tr><td>${_escHtml(s.name)}</td><td class="tool-muted">${_escHtml(s.ip)}</td><td>${s.time_ms} ms</td><td>${(s.answers||[]).join(", ")}</td>${errTd}</tr>`;
  }
  html += "</table>";
  return html;
}

function _fmtDnsLeak(r) {
  let html = `<div style="margin-bottom:8px">Wykryte serwery DNS: <b>${r.count}</b></div>`;
  if (r.detected_dns_servers?.length) {
    html += `<table class="tool-table"><tr><th>IP</th><th>Hostname</th></tr>`;
    for (const s of r.detected_dns_servers) {
      html += `<tr><td>${_escHtml(s.ip)}</td><td class="tool-muted">${_escHtml(s.hostname)}</td></tr>`;
    }
    html += "</table>";
  }
  html += `<div class="tool-muted" style="margin-top:6px;font-size:11px">Metoda: ${_escHtml(r.method)}</div>`;
  return html;
}

function _fmtDohDot(r) {
  const _status = (obj) => obj?.available
    ? `<span class="tool-ok">Dostepny</span> (${obj.time_ms} ms)`
    : `<span class="tool-bad">Niedostepny</span>` + (obj?.error ? ` — ${_escHtml(obj.error)}` : "");
  return `<table class="tool-table">
    <tr><td class="tool-key">Serwer</td><td>${_escHtml(r.dns_server)}</td></tr>
    <tr><td class="tool-key">Plain DNS</td><td>${_status(r.plain_dns)}</td></tr>
    <tr><td class="tool-key">DNS over TLS (853)</td><td>${_status(r.dot)}</td></tr>
    <tr><td class="tool-key">DNS over HTTPS</td><td>${_status(r.doh)}</td></tr>
  </table>`;
}

function _fmtDnsComparison(r) {
  let html = `<div style="margin-bottom:8px">Domena: <b>${_escHtml(r.domain)}</b>, iteracje: ${r.iterations}</div>`;
  html += `<table class="tool-table"><tr><th>Serwer</th><th>IP</th><th>Avg</th><th>Min</th><th>Max</th></tr>`;
  for (const s of (r.servers || [])) {
    html += `<tr><td>${_escHtml(s.name)}</td><td class="tool-muted">${_escHtml(s.ip)}</td>
      <td><b>${s.avg_ms ?? "-"} ms</b></td><td>${s.min_ms ?? "-"} ms</td><td>${s.max_ms ?? "-"} ms</td></tr>`;
  }
  html += "</table>";
  return html;
}

function _fmtTraceroute(r) {
  let html = `<div style="margin-bottom:8px">Cel: <b>${_escHtml(r.target)}</b>`;
  if (r.resolved_ip) html += ` (${_escHtml(r.resolved_ip)})`;
  html += "</div>";
  if (r.hops?.length) {
    html += `<table class="tool-table"><tr><th>#</th><th>IP</th><th>RTT 1</th><th>RTT 2</th><th>RTT 3</th></tr>`;
    for (const h of r.hops) {
      const rtt = h.rtt_ms || [];
      const fmtRtt = (v) => v != null ? `${v} ms` : "*";
      html += `<tr><td>${h.hop}</td><td>${_escHtml(h.ip || "*")}</td>
        <td>${fmtRtt(rtt[0])}</td><td>${fmtRtt(rtt[1])}</td><td>${fmtRtt(rtt[2])}</td></tr>`;
    }
    html += "</table>";
  } else if (r.raw) {
    html += `<pre style="white-space:pre-wrap;font-size:11px">${_escHtml(r.raw)}</pre>`;
  }
  return html;
}

function _fmtMtr(r) {
  let html = `<div style="margin-bottom:8px">Cel: <b>${_escHtml(r.target)}</b>, proby: ${r.count}</div>`;
  if (r.report?.length) {
    html += `<table class="tool-table"><tr><th>#</th><th>Host</th><th>Loss%</th><th>Sent</th><th>Avg</th><th>Best</th><th>Worst</th><th>StDev</th></tr>`;
    for (let i = 0; i < r.report.length; i++) {
      const h = r.report[i];
      const lossClass = h.loss_pct > 0 ? "tool-bad" : "";
      html += `<tr><td>${i+1}</td><td>${_escHtml(h.host)}</td>
        <td class="${lossClass}">${h.loss_pct}%</td><td>${h.sent}</td>
        <td><b>${h.avg_ms} ms</b></td><td>${h.best_ms} ms</td><td>${h.worst_ms} ms</td><td>${h.stdev_ms}</td></tr>`;
    }
    html += "</table>";
  }
  return html;
}

function _fmtBgp(r) {
  return `<table class="tool-table">
    <tr><td class="tool-key">Cel</td><td>${_escHtml(r.target)}</td></tr>
    <tr><td class="tool-key">IP</td><td>${_escHtml(r.ip)}</td></tr>
    <tr><td class="tool-key">ASN</td><td><b>${_escHtml(r.asn)}</b></td></tr>
    <tr><td class="tool-key">Nazwa AS</td><td>${_escHtml(r.as_name)}</td></tr>
    <tr><td class="tool-key">Kraj AS</td><td>${_escHtml(r.as_country)}</td></tr>
    <tr><td class="tool-key">Prefix</td><td>${_escHtml(r.prefix)}</td></tr>
    <tr><td class="tool-key">Siec</td><td>${_escHtml(r.network_name)}</td></tr>
  </table>`;
}

function _fmtReverseDns(r) {
  let html = `<table class="tool-table">
    <tr><td class="tool-key">IP</td><td>${_escHtml(r.ip)}</td></tr>
    <tr><td class="tool-key">Czas</td><td>${r.time_ms} ms</td></tr>`;
  if (r.hostnames?.length) {
    html += `<tr><td class="tool-key">Hostname(s)</td><td><b>${r.hostnames.map(h => _escHtml(h)).join("<br>")}</b></td></tr>`;
  }
  if (r.error) {
    html += `<tr><td class="tool-key">Blad</td><td class="tool-bad">${_escHtml(r.error)}</td></tr>`;
  }
  html += "</table>";
  return html;
}

function _fmtMtu(r) {
  return `<table class="tool-table">
    <tr><td class="tool-key">Cel</td><td>${_escHtml(r.target)}</td></tr>
    <tr><td class="tool-key">MTU</td><td><b>${r.mtu} bajtow</b></td></tr>
    <tr><td class="tool-key">Payload MTU</td><td>${r.path_mtu_payload} bajtow</td></tr>
  </table>`;
}

function _fmtGeoip(r) {
  return `<table class="tool-table">
    <tr><td class="tool-key">IP</td><td><b>${_escHtml(r.ip)}</b></td></tr>
    <tr><td class="tool-key">Kraj</td><td>${_escHtml(r.country)} (${_escHtml(r.country_code)})</td></tr>
    <tr><td class="tool-key">Region</td><td>${_escHtml(r.region)}</td></tr>
    <tr><td class="tool-key">Miasto</td><td>${_escHtml(r.city)}</td></tr>
    <tr><td class="tool-key">ISP</td><td>${_escHtml(r.isp)}</td></tr>
    <tr><td class="tool-key">Organizacja</td><td>${_escHtml(r.org)}</td></tr>
    <tr><td class="tool-key">AS</td><td>${_escHtml(r.as)}</td></tr>
    <tr><td class="tool-key">Wspolrzedne</td><td>${r.lat}, ${r.lon}</td></tr>
    <tr><td class="tool-key">Strefa czasowa</td><td>${_escHtml(r.timezone)}</td></tr>
  </table>`;
}

function _fmtPublicIp(r) {
  const icon = r.consistent ? '<span class="tool-ok">Spojne</span>' : '<span class="tool-bad">Niespojne</span>';
  let html = `<div style="margin-bottom:8px">Publiczny IP: <b>${_escHtml(r.ip)}</b> — ${icon}</div>`;
  html += `<table class="tool-table"><tr><th>Serwis</th><th>Wynik</th></tr>`;
  for (const [svc, ip] of Object.entries(r.sources || {})) {
    html += `<tr><td>${_escHtml(svc)}</td><td>${_escHtml(ip)}</td></tr>`;
  }
  html += "</table>";
  return html;
}

function _fmtNat(r) {
  return `<table class="tool-table">
    <tr><td class="tool-key">Typ NAT</td><td><b>${_escHtml(r.nat_type)}</b></td></tr>
    <tr><td class="tool-key">Zewnetrzny IP</td><td>${_escHtml(r.external_ip)}</td></tr>
    <tr><td class="tool-key">Zewnetrzny port</td><td>${r.external_port}</td></tr>
  </table>`;
}

function _fmtPortScan(r) {
  const open = (r.open_ports || []).length;
  let html = `<div style="margin-bottom:8px">Cel: <b>${_escHtml(r.target)}</b> — skanowano ${r.scanned_ports} portow, otwartych: <b>${open}</b></div>`;
  const ports = r.all_ports || r.open_ports || [];
  if (ports.length) {
    html += `<table class="tool-table"><tr><th>Port</th><th>Stan</th><th>Usluga</th></tr>`;
    for (const p of ports) {
      const cls = p.state === "open" ? "tool-ok" : "tool-muted";
      html += `<tr><td>${p.port}</td><td class="${cls}">${p.state}</td><td class="tool-muted">${_escHtml(p.service)}</td></tr>`;
    }
    html += "</table>";
  }
  return html;
}

function _fmtLanDiscovery(r) {
  let html = `<div style="margin-bottom:8px">Podsiec: <b>${_escHtml(r.subnet)}</b> — znalezione urzadzenia: <b>${r.total}</b></div>`;
  if (r.devices?.length) {
    html += `<table class="tool-table"><tr><th>IP</th><th>MAC</th><th>Vendor</th><th>Hostname</th></tr>`;
    for (const d of r.devices) {
      html += `<tr><td>${_escHtml(d.ip)}</td><td class="tool-muted">${_escHtml(d.mac)}</td><td class="tool-muted">${_escHtml(d.vendor)}</td><td>${_escHtml(d.hostname)}</td></tr>`;
    }
    html += "</table>";
  }
  if (r.raw) {
    html += `<pre style="white-space:pre-wrap;font-size:11px;margin-top:8px">${_escHtml(r.raw)}</pre>`;
  }
  return html;
}

function _fmtIperf(r) {
  return `<table class="tool-table">
    <tr><td class="tool-key">Serwer</td><td>${_escHtml(r.server)}:${r.port}</td></tr>
    <tr><td class="tool-key">Kierunek</td><td>${_escHtml(r.direction)}</td></tr>
    <tr><td class="tool-key">Czas</td><td>${r.duration_s} s</td></tr>
    <tr><td class="tool-key">Transfer</td><td>${(r.transfer_bytes / 1048576).toFixed(2)} MB</td></tr>
    <tr><td class="tool-key">Przepustowosc</td><td><b>${r.bandwidth_mbps} Mbps</b></td></tr>
  </table>`;
}

function _fmtDhcp(r) {
  let html = `<div style="margin-bottom:8px">Zrodlo: <b>${_escHtml(r.source)}</b> — wpisow: ${r.count}</div>`;
  if (r.leases?.length) {
    html += `<table class="tool-table">`;
    for (const lease of r.leases) {
      for (const [k, v] of Object.entries(lease)) {
        html += `<tr><td class="tool-key">${_escHtml(k)}</td><td>${_escHtml(v)}</td></tr>`;
      }
      html += `<tr><td colspan="2" style="border-bottom:2px solid var(--border)"></td></tr>`;
    }
    html += "</table>";
  }
  return html;
}

function _fmtGeneric(r) {
  let html = '<table class="tool-table">';
  for (const [k, v] of Object.entries(r)) {
    const display = typeof v === "object" ? JSON.stringify(v, null, 2) : String(v);
    html += `<tr><td class="tool-key">${_escHtml(k)}</td><td>${_escHtml(display)}</td></tr>`;
  }
  html += "</table>";
  return html;
}

// ---- Tools toggle ----
qs("tools-header")?.addEventListener("click", () => {
  const body = qs("tools-body");
  const btn = qs("tools-toggle");
  if (!body || !btn) return;
  if (body.style.display === "none") {
    body.style.display = "";
    btn.innerHTML = "&#9650;";
  } else {
    body.style.display = "none";
    btn.innerHTML = "&#9660;";
  }
});

// Initialize tools section
renderToolsSection();
