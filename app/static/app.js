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

async function loadSpeed() {
  const params = paramsFromInputs();
  const resp = await fetch(`/api/speed?${params.toString()}`);
  const data = await resp.json();
  const items = data.items || [];

  const labels = items.map(i => i.started_at);
  const values = items.map(i => i.error ? null : i.mbps);

  const ctx = qs("speedChart").getContext("2d");
  if (!speedChart) {
    speedChart = new Chart(ctx, {
      type: "line",
      data: {
        labels,
        datasets: [{
          label: "Mbps",
          data: values,
          borderColor: "rgba(99, 102, 241, 0.9)",
          backgroundColor: "rgba(99, 102, 241, 0.2)",
          borderWidth: 2,
          pointRadius: 1.5,
          tension: 0.25,
          spanGaps: true,
        }]
      },
      options: {
        responsive: true,
        scales: {
          x: { ticks: { color: "rgba(169,180,221,.8)", maxRotation: 0, autoSkip: true } },
          y: { ticks: { color: "rgba(169,180,221,.8)" }, beginAtZero: true }
        },
        plugins: {
          legend: { labels: { color: "rgba(231,236,255,.9)" } },
          tooltip: { callbacks: { label: (ctx) => `${(ctx.raw ?? 0).toFixed(2)} Mbps` } }
        }
      }
    });
  } else {
    speedChart.data.labels = labels;
    speedChart.data.datasets[0].data = values;
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
  await Promise.all([loadStatus(), loadSpeed(), loadQuality(), loadOutagesList()]);
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
