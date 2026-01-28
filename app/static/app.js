function qs(id) { return document.getElementById(id); }

function paramsFromInputs() {
  const from = qs("from").value.trim();
  const to = qs("to").value.trim();
  const params = new URLSearchParams();
  if (from) params.set("from", from);
  if (to) params.set("to", to);
  return params;
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

async function loadConfig() {
  const resp = await fetch("/api/config");
  const cfg = await resp.json();
  qs("cfg-connect-target").value = cfg.connect_target ?? "";
  qs("cfg-connect-interval").value = cfg.connect_interval_seconds ?? 1;
  qs("cfg-speed-url").value = cfg.speedtest_url ?? "";
  qs("cfg-speed-interval").value = cfg.speedtest_interval_seconds ?? 900;
}

function setCfgMsg(text, ok) {
  const el = qs("cfg-msg");
  el.textContent = text;
  el.style.color = ok ? "rgba(34,197,94,.95)" : "rgba(239,68,68,.95)";
  setTimeout(() => { el.textContent = ""; el.style.color = ""; }, 3500);
}

async function saveConfig() {
  const connectTarget = qs("cfg-connect-target").value.trim();
  if (!connectTarget) {
    setCfgMsg("Podaj adres do testu internetu.", false);
    return;
  }
  const payload = {
    connect_target: connectTarget,
    connect_interval_seconds: Number(qs("cfg-connect-interval").value),
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
  setCfgMsg("Zapisano.", true);
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

async function loadStatus() {
  const resp = await fetch("/api/status");
  const data = await resp.json();

  const badgeOnline = qs("badge-online");
  const badgeLast = qs("badge-last");

  const isUp = data.connectivity?.is_up === 1;
  badgeOnline.textContent = isUp ? "Online" : "Offline";
  badgeOnline.className = `badge ${isUp ? "badge-ok" : "badge-bad"}`;

  if (data.last_speed_test?.started_at) {
    const mbps = (data.last_speed_test.mbps ?? 0).toFixed(1);
    const err = data.last_speed_test.error ? ` (${data.last_speed_test.error})` : "";
    badgeLast.textContent = `Ostatni test: ${mbps} Mbps${err}`;
    badgeLast.className = `badge ${data.last_speed_test.error ? "badge-gray" : "badge-ok"}`;
  } else {
    badgeLast.textContent = "Ostatni test: brak";
    badgeLast.className = "badge badge-gray";
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

  const outageIntervals = outages
    .map(o => ({ startMs: parseIsoToMs(o.started_at), endMs: parseIsoToMs(o.ended_at) }))
    .filter(o => o.startMs != null && o.endMs != null);

  const okSpeeds = items.filter(i => !i.error && typeof i.mbps === "number" && i.mbps > 0).map(i => i.mbps);
  const avgMbps = okSpeeds.length ? (okSpeeds.reduce((a, b) => a + b, 0) / okSpeeds.length) : 0;
  currentAvgMbps = avgMbps;

  const labels = items.map(i => i.started_at);
  const speedRel = items.map(i => {
    if (i.error || typeof i.mbps !== "number" || i.mbps <= 0 || avgMbps <= 0) return null;
    return i.mbps / avgMbps;
  });
  const onlineRel = items.map(i => {
    const tMs = parseIsoToMs(i.started_at);
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
          data: items.map((it, idx) => ({
            x: labels[idx],
            y: speedRel[idx],
            mbps: (it && !it.error) ? it.mbps : null,
          })),
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
    speedChart.data.datasets[0].data = items.map((it, idx) => ({
      x: labels[idx],
      y: speedRel[idx],
      mbps: (it && !it.error) ? it.mbps : null,
    }));
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

 (async () => {
  await loadConfig();
  await refreshAll();
  setInterval(loadStatus, 2000);
})().catch((e) => {
  console.error(e);
});
