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

function buildBlockedDataPoints(blockedIntervals, rangeFromMs, rangeToMs) {
  // Generate blocked periods data points as a dashed line at y=0.4
  // This shows when measurements were disabled/blocked by schedule
  const points = [];
  const BLOCKED_Y = 0.4;

  if (!blockedIntervals || blockedIntervals.length === 0) {
    return points;
  }

  // Sort by start time
  const sorted = [...blockedIntervals].sort((a, b) => a.startMs - b.startMs);

  for (const block of sorted) {
    const startMs = Math.max(block.startMs, rangeFromMs);
    const endMs = Math.min(block.endMs, rangeToMs);

    if (startMs < endMs) {
      // Add null before to create gap
      if (points.length > 0) {
        points.push({ x: startMs - 1, y: null });
      }
      // Blocked period start
      points.push({ x: startMs, y: BLOCKED_Y });
      // Blocked period end
      points.push({ x: endMs, y: BLOCKED_Y });
      // Add null after to create gap
      points.push({ x: endMs + 1, y: null });
    }
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

  // Build blocked periods data points
  const blockedPingPoints = buildBlockedDataPoints(blockedPingIntervals, rangeFromMs, rangeToMs);
  const blockedSpeedPoints = buildBlockedDataPoints(blockedSpeedIntervals, rangeFromMs, rangeToMs);

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
    spanGaps: true,
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
      yAxisID: "yRel",
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
        legend: { labels: { color: "rgba(231,236,255,.9)" } },
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

// Schedule buttons
qs("add-ping-schedule")?.addEventListener("click", () => addSchedule("ping"));
qs("add-speed-schedule")?.addEventListener("click", () => addSchedule("speed"));
