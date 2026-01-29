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
  };
}

function isDraftDifferent(draft, cfg) {
  if (!cfg) return false;
  if ((cfg.connect_target ?? "").trim() !== (draft.connect_target ?? "").trim()) return true;
  if (normalizeNum(cfg.connect_interval_seconds) !== normalizeNum(draft.connect_interval_seconds)) return true;
  if ((cfg.speedtest_mode ?? "url") !== (draft.speedtest_mode ?? "url")) return true;
  if ((cfg.speedtest_url ?? "").trim() !== (draft.speedtest_url ?? "").trim()) return true;
  if (normalizeNum(cfg.speedtest_interval_seconds) !== normalizeNum(draft.speedtest_interval_seconds)) return true;
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
  const speedMode = cfg.speedtest_mode ?? "url";
  const radios = document.querySelectorAll("input[name='speedtest-mode']");
  for (const r of radios) r.checked = (r.value === speedMode);
  qs("cfg-speed-url").value = cfg.speedtest_url ?? "";
  qs("cfg-speed-interval").value = cfg.speedtest_interval_seconds ?? 900;
  applySpeedtestModeUi(speedMode);
  setCfgDirty(false);
}

function setCfgMsg(text, ok) {
  const el = qs("cfg-msg");
  el.textContent = text;
  el.style.color = ok ? "rgba(34,197,94,.95)" : "rgba(239,68,68,.95)";
  setTimeout(() => { el.textContent = ""; el.style.color = ""; }, 3500);
}

function selectedSpeedtestMode() {
  const el = document.querySelector("input[name='speedtest-mode']:checked");
  return el ? el.value : "url";
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
  const modeChanged = (lastLoadedConfig?.speedtest_mode ?? "url") !== (newCfg?.speedtest_mode ?? "url");
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

async function loadChart() {
  const params = paramsFromInputs();
  const [speedResp, outagesResp] = await Promise.all([
    fetch(`/api/speed?${params.toString()}`),
    fetch(`/api/outages?${params.toString()}`),
  ]);
  const speedData = await speedResp.json();
  const outagesData = await outagesResp.json();
  const items = speedData.items || [];
  const outages = outagesData.items || [];

  const rangeFrom = speedData?.range?.from || outagesData?.range?.from || null;
  const rangeTo = speedData?.range?.to || outagesData?.range?.to || null;

  const outageIntervals = outages
    .map(o => ({ startMs: parseIsoToMs(o.started_at), endMs: parseIsoToMs(o.ended_at) }))
    .filter(o => o.startMs != null && o.endMs != null);

  const okSpeeds = items.filter(i => !i.error && typeof i.mbps === "number" && i.mbps > 0).map(i => i.mbps);
  const avgMbps = okSpeeds.length ? (okSpeeds.reduce((a, b) => a + b, 0) / okSpeeds.length) : 0;
  currentAvgMbps = avgMbps;

  // Labels = union: speed tests + outage boundaries (only actual data points).
  const labelCandidates = [];
  for (const it of items) labelCandidates.push(it.started_at);
  for (const o of outages) {
    labelCandidates.push(o.started_at);
    labelCandidates.push(o.ended_at);
  }
  const labels = uniqueSortedIso(labelCandidates);

  const speedByTs = new Map();
  for (const it of items) speedByTs.set(normalizeIsoKey(it.started_at), it);

  const speedRel = labels.map((ts) => {
    const it = speedByTs.get(ts);
    if (!it || it.error || typeof it.mbps !== "number" || it.mbps <= 0 || avgMbps <= 0) return null;
    return it.mbps / avgMbps;
  });
  const onlineRel = labels.map((ts) => {
    const tMs = parseIsoToMs(ts);
    const online = isOnlineAt(outageIntervals, tMs);
    // Skaluj wartość online (1) do 0.8, żeby linia nie zlewała się ze średnią prędkością
    return online === 1 ? 0.8 : online;
  });

  const ctx = qs("speedChart").getContext("2d");
  const maxRel = Math.max(
    1.2,
    ...speedRel.filter(v => typeof v === "number" && Number.isFinite(v)),
    ...onlineRel.filter(v => typeof v === "number" && Number.isFinite(v)),
  );
  const relMax = Math.max(1.2, maxRel * 1.15);

  if (!speedChart) {
    speedChart = new Chart(ctx, {
      type: "line",
      data: {
        labels,
        datasets: [{
          label: "Prędkość (Mbps)",
          yAxisID: "yMbps",
          data: labels.map((ts, idx) => {
            const it = speedByTs.get(ts);
            return {
              x: ts,
              y: speedRel[idx],
              mbps: (it && !it.error) ? it.mbps : null,
              error: it?.error || null,
            };
          }),
          borderColor: "rgba(99, 102, 241, 0.95)",
          backgroundColor: "rgba(99, 102, 241, 0.2)",
          borderWidth: 2,
          pointRadius: 1.5,
          tension: 0.25,
          spanGaps: true,
        }, {
          label: "Online (0/1)",
          yAxisID: "yRel",
          data: onlineRel,
          borderColor: "rgba(34, 197, 94, 0.95)",
          backgroundColor: "rgba(34, 197, 94, 0.15)",
          borderWidth: 2,
          pointRadius: 0,
          stepped: true,
          spanGaps: true,
        }],
      },
      options: {
        responsive: true,
        scales: {
          x: {
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
                  const v = ctx.raw;
                  return `Online: ${v > 0 ? "1" : "0"}`;
                }
                return ctx.formattedValue;
              }
            }
          }
        }
      }
    });
  } else {
    speedChart.data.labels = labels;
    speedChart.data.datasets[0].data = labels.map((ts, idx) => {
      const it = speedByTs.get(ts);
      return {
        x: ts,
        y: speedRel[idx],
        mbps: (it && !it.error) ? it.mbps : null,
        error: it?.error || null,
      };
    });
    speedChart.data.datasets[1].data = onlineRel;
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
]) {
  el?.addEventListener("input", updateCfgDirty);
}
