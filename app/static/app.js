function qs(id) { return document.getElementById(id); }

function paramsFromInputs() {
  const from = qs("from").value.trim();
  const to = qs("to").value.trim();
  const params = new URLSearchParams();
  if (from) params.set("from", from);
  if (to) params.set("to", to);
  return params;
}

function isoWithOffset(date) {
  const pad = (n) => String(n).padStart(2, "0");
  const y = date.getFullYear();
  const m = pad(date.getMonth() + 1);
  const d = pad(date.getDate());
  const hh = pad(date.getHours());
  const mm = pad(date.getMinutes());
  const ss = pad(date.getSeconds());
  const tz = -date.getTimezoneOffset(); // minutes
  const sign = tz >= 0 ? "+" : "-";
  const tzh = pad(Math.floor(Math.abs(tz) / 60));
  const tzm = pad(Math.abs(tz) % 60);
  return `${y}-${m}-${d}T${hh}:${mm}:${ss}${sign}${tzh}:${tzm}`;
}

function initDatePickers() {
  if (typeof flatpickr !== "function") return;
  const common = {
    enableTime: true,
    time_24hr: true,
    seconds: false,
    allowInput: true,
    dateFormat: "Y-m-d H:i",
  };
  flatpickr(qs("from"), {
    ...common,
    onChange: (selectedDates, _dateStr, instance) => {
      if (selectedDates?.[0]) instance.input.value = isoWithOffset(selectedDates[0]);
    },
  });
  flatpickr(qs("to"), {
    ...common,
    onChange: (selectedDates, _dateStr, instance) => {
      if (selectedDates?.[0]) instance.input.value = isoWithOffset(selectedDates[0]);
    },
  });
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
  lastLoadedConfig = newCfg;

  if (modeChanged || urlChanged) {
    // po zmianie typu/usługi testu prędkości uruchom od razu pomiar
    fetch("/api/speedtest/run", { method: "POST" }).catch(() => {});
  }
  await refreshAll();
}

function parseIsoToMs(iso) {
  if (!iso) return null;
  const ms = Date.parse(iso);
  return Number.isFinite(ms) ? ms : null;
}

function isOnlineAt(outageIntervals, tMs) {
  if (tMs == null) return null;
  for (const it of outageIntervals) {
    if (tMs >= it.startMs && tMs <= it.endMs) return 0;
  }
  return 1;
}

function uniqueSortedIso(values) {
  const map = new Map();
  for (const v of values) {
    if (!v) continue;
    const ms = parseIsoToMs(v);
    if (ms == null) continue;
    // normalize key to original iso for display, but sort by ms
    map.set(v, ms);
  }
  return [...map.entries()]
    .sort((a, b) => a[1] - b[1])
    .map(([iso]) => iso);
}

async function loadStatus() {
  const resp = await fetch("/api/status");
  const data = await resp.json();

  const badgeOnline = qs("badge-online");
  const badgeLast = qs("badge-last");

  const isUp = data.connectivity?.is_up === 1;
  badgeOnline.textContent = isUp ? "Online" : "Offline";
  badgeOnline.className = `badge ${isUp ? "badge-ok" : "badge-bad"}`;

  if (data.speedtest_running) {
    badgeLast.style.display = "";
    badgeLast.textContent = "Trwa pomiar prędkości…";
    badgeLast.className = "badge badge-gray";
  } else if (data.last_speed_test?.started_at && !data.last_speed_test?.error) {
    badgeLast.style.display = "";
    const dl = (data.last_speed_test.mbps ?? 0);
    const ul = data.last_speed_test.upload_mbps;
    const ping = data.last_speed_test.ping_ms;
    const mode = data.last_speed_test.speedtest_mode || data.config?.speedtest_mode || "";
    const parts = [`Ostatni test: ${dl.toFixed(1)}↓ Mbps`];
    if (typeof ul === "number" && Number.isFinite(ul) && ul > 0) parts.push(`${ul.toFixed(1)}↑ Mbps`);
    if (typeof ping === "number" && Number.isFinite(ping) && ping > 0) parts.push(`ping ${ping.toFixed(0)} ms`);
    const srv = data.last_speed_test.server_name;
    const cc = data.last_speed_test.server_country;
    if (srv) parts.push(`serwer: ${srv}${cc ? " (" + cc + ")" : ""}`);
    if (mode && mode !== "url") parts.push(`tryb: ${mode}`);
    badgeLast.textContent = parts.join(" · ");
    badgeLast.className = "badge badge-ok";
  } else {
    // jeśli nie trwa pomiar, nie pokazuj komunikatu o błędzie
    badgeLast.textContent = "";
    badgeLast.style.display = "none";
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

  // Labels = union: range endpoints + speed tests + outage boundaries.
  const labelCandidates = [];
  if (rangeFrom) labelCandidates.push(rangeFrom);
  if (rangeTo) labelCandidates.push(rangeTo);
  for (const it of items) labelCandidates.push(it.started_at);
  for (const o of outages) {
    labelCandidates.push(o.started_at);
    labelCandidates.push(o.ended_at);
  }
  const labels = uniqueSortedIso(labelCandidates);

  const speedByTs = new Map();
  for (const it of items) speedByTs.set(it.started_at, it);

  const speedRel = labels.map((ts) => {
    const it = speedByTs.get(ts);
    if (!it || it.error || typeof it.mbps !== "number" || it.mbps <= 0 || avgMbps <= 0) return null;
    return it.mbps / avgMbps;
  });
  const onlineRel = labels.map((ts) => {
    const tMs = parseIsoToMs(ts);
    return isOnlineAt(outageIntervals, tMs);
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
          x: { ticks: { color: "rgba(169,180,221,.8)", maxRotation: 0, autoSkip: true } },
          yRel: {
            position: "left",
            min: 0,
            max: relMax,
            ticks: {
              color: "rgba(169,180,221,.8)",
              callback: (v) => (v === 0 || v === 1 ? String(v) : ""),
            },
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
                  return `Online: ${v === 1 ? "1" : "0"}`;
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
}

async function refreshAll() {
  refreshExports();
  await Promise.all([loadStatus(), loadChart(), loadQuality(), loadOutagesList()]);
}

qs("refresh").addEventListener("click", refreshAll);
qs("cfg-save").addEventListener("click", saveConfig);
for (const r of document.querySelectorAll("input[name='speedtest-mode']")) {
  r.addEventListener("change", () => applySpeedtestModeUi(selectedSpeedtestMode()));
}

 (async () => {
  initDatePickers();
  loadVersion();
  await loadConfig();
  await refreshAll();
  setInterval(loadStatus, 2000);
})().catch((e) => {
  console.error(e);
});
