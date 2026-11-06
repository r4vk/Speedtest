/**
 * Urządzenia wysyłające wyniki (etap 9) i porównanie urządzenie ↔ ten serwer.
 *
 * Ładowany po `app.js` i `quality.js`; korzysta z ich globalnych `qs` i
 * `paramsFromInputs`. `app.js` woła `Devices.refresh(params)` razem z
 * `Quality.refresh`, więc porównanie zawsze pokazuje ten sam zakres co reszta panelu.
 */
const Devices = (() => {
  let devices = [];
  let lastCompare = null;
  let compareChart = null;
  let lastParams = null;

  const SERVER_COLOR = "#22c55e";
  const DEVICE_COLOR = "#f59e0b";

  function el(id) {
    return document.getElementById(id);
  }

  function fmtPct(value) {
    return typeof value === "number" && Number.isFinite(value) ? `${value.toFixed(2)}%` : "–";
  }

  function fmtMs(value) {
    return typeof value === "number" && Number.isFinite(value) ? `${value.toFixed(1)} ms` : "–";
  }

  function fmtSkew(ms) {
    if (typeof ms !== "number" || !Number.isFinite(ms)) return "–";
    const s = Math.abs(ms) / 1000;
    return s < 1 ? `${Math.round(ms)} ms` : `${ms < 0 ? "−" : ""}${s.toFixed(1)} s`;
  }

  function fmtWhen(iso) {
    return iso ? iso.replace("T", " ").slice(0, 19) : "nigdy";
  }

  function flash(id, text, ok) {
    const node = el(id);
    if (!node) return;
    node.textContent = text;
    node.style.color = ok ? "rgba(34,197,94,.95)" : "rgba(239,68,68,.95)";
    setTimeout(() => {
      node.textContent = "";
      node.style.color = "";
    }, 5000);
  }

  async function errorText(resp) {
    try {
      const body = await resp.json();
      return typeof body.detail === "string" ? body.detail : `HTTP ${resp.status}`;
    } catch (_) {
      return `HTTP ${resp.status}`;
    }
  }

  function uploaders() {
    return devices.filter((d) => d.kind !== "nas");
  }

  // ---------------------------------------------------------------------
  // lista urządzeń (ustawienia)
  // ---------------------------------------------------------------------

  function renderDevicesTable() {
    const tbody = el("q-devices-tbody");
    if (!tbody) return;
    tbody.innerHTML = "";
    const list = uploaders();
    if (!list.length) {
      const tr = document.createElement("tr");
      const td = document.createElement("td");
      td.colSpan = 5;
      td.className = "tool-muted";
      td.textContent = "Brak urządzeń. Dodaj Maca z NetLogiem poniżej.";
      tr.appendChild(td);
      tbody.appendChild(tr);
      return;
    }
    for (const d of list) {
      const tr = document.createElement("tr");
      for (const text of [d.name, d.id, fmtWhen(d.last_seen_at), fmtSkew(d.last_skew_ms)]) {
        const td = document.createElement("td");
        td.textContent = text;
        tr.appendChild(td);
      }
      const td = document.createElement("td");
      const btn = document.createElement("button");
      btn.type = "button";
      btn.className = "btn-small";
      btn.dataset.deleteDevice = d.id;
      btn.textContent = "Usuń";
      td.appendChild(btn);
      tr.appendChild(td);
      tbody.appendChild(tr);
    }
  }

  function renderDeviceSelect() {
    const select = el("q-compare-device");
    if (!select) return;
    const previous = select.value;
    select.innerHTML = "";
    for (const d of uploaders()) {
      const opt = document.createElement("option");
      opt.value = d.id;
      opt.textContent = d.name;
      select.appendChild(opt);
    }
    if (uploaders().some((d) => d.id === previous)) select.value = previous;
  }

  async function loadDevices() {
    const resp = await fetch("/api/devices");
    if (!resp.ok) throw new Error(await errorText(resp));
    devices = (await resp.json()).items || [];
    renderDevicesTable();
    renderDeviceSelect();
  }

  async function onAddDevice(e) {
    e.preventDefault();
    const input = el("q-device-new-name");
    const name = (input?.value || "").trim();
    if (!name) {
      flash("q-devices-msg", "Podaj nazwę urządzenia.", false);
      return;
    }
    const resp = await fetch("/api/devices", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name, kind: "macos" }),
    });
    if (!resp.ok) {
      flash("q-devices-msg", await errorText(resp), false);
      return;
    }
    const body = await resp.json();
    if (input) input.value = "";
    const box = el("q-device-token");
    if (box) {
      box.style.display = "";
      box.textContent = `Token dla „${body.device.name}” (${body.device.id}), skopiuj go teraz — później nie będzie widoczny: ${body.token}`;
    }
    await loadDevices();
    await refresh(lastParams);
  }

  async function onDevicesTableClick(e) {
    const id = e.target?.dataset?.deleteDevice;
    if (!id) return;
    if (!window.confirm(`Usunąć urządzenie ${id}? Jego token przestanie działać.`)) return;
    const resp = await fetch(`/api/devices/${encodeURIComponent(id)}`, { method: "DELETE" });
    if (!resp.ok) {
      flash("q-devices-msg", await errorText(resp), false);
      return;
    }
    flash("q-devices-msg", "Usunięto.", true);
    await loadDevices();
    await refresh(lastParams);
  }

  // ---------------------------------------------------------------------
  // porównanie
  // ---------------------------------------------------------------------

  function renderCompareTable(data) {
    const tbody = el("q-compare-tbody");
    if (!tbody) return;
    tbody.innerHTML = "";
    for (const entry of data.targets || []) {
      const nas = entry.nas.stats || {};
      const dev = entry.device.stats || {};
      const tr = document.createElement("tr");
      const cells = [
        entry.target.name,
        fmtPct(nas.loss_pct),
        fmtPct(dev.loss_pct),
        fmtMs(nas.rtt_p50_ms),
        fmtMs(dev.rtt_p50_ms),
        dev.longest_fail_streak != null ? String(dev.longest_fail_streak) : "–",
      ];
      for (const text of cells) {
        const td = document.createElement("td");
        td.textContent = text;
        tr.appendChild(td);
      }
      tbody.appendChild(tr);
    }
  }

  function renderTargetSelect(data) {
    const select = el("q-compare-target");
    if (!select) return;
    const previous = select.value;
    select.innerHTML = "";
    for (const entry of data.targets || []) {
      const opt = document.createElement("option");
      opt.value = String(entry.target.id);
      opt.textContent = entry.target.name;
      select.appendChild(opt);
    }
    if ((data.targets || []).some((e) => String(e.target.id) === previous)) select.value = previous;
  }

  function lossSeries(points) {
    return (points || []).map((p) => ({
      x: Date.parse(p.t),
      y: typeof p.loss_pct === "number" ? p.loss_pct : null,
    }));
  }

  function renderCompareChart() {
    const canvas = el("qualityCompareChart");
    if (!canvas || typeof Chart === "undefined" || !lastCompare) return;
    const id = el("q-compare-target")?.value;
    const entry = (lastCompare.targets || []).find((e) => String(e.target.id) === id);
    if (compareChart) {
      compareChart.destroy();
      compareChart = null;
    }
    if (!entry) return;
    // Oś tylko tam, gdzie urządzenie coś wysłało (± jeden przedział): Mac bywa
    // uśpiony, a dobowy zakres zmieściłby kilka minut jego danych w jednym pikselu.
    const measured = (entry.device.points || []).filter((p) => p.attempts > 0).map((p) => Date.parse(p.t));
    const pad = (lastCompare.bucket_seconds || 60) * 1000;
    const xMin = measured.length ? Math.min(...measured) - pad : undefined;
    const xMax = measured.length ? Math.max(...measured) + 2 * pad : undefined;
    const text =(getComputedStyle(document.documentElement).getPropertyValue("--text") || "").trim() || "#e7ecff";
    compareChart = new Chart(canvas, {
      type: "bar",
      data: {
        datasets: [
          { label: "Ten serwer", data: lossSeries(entry.nas.points), backgroundColor: SERVER_COLOR },
          { label: lastCompare.device.name, data: lossSeries(entry.device.points), backgroundColor: DEVICE_COLOR },
        ],
      },
      options: {
        animation: false,
        parsing: false,
        plugins: { legend: { labels: { color: text } } },
        scales: {
          x: { type: "time", min: xMin, max: xMax, offset: true, ticks: { color: text } },
          y: { beginAtZero: true, suggestedMax: 5, title: { display: true, text: "utrata %", color: text }, ticks: { color: text } },
        },
      },
    });
  }

  async function refreshCompare(params) {
    const wrap = el("q-compare-wrap");
    const deviceId = el("q-compare-device")?.value;
    if (!wrap) return;
    if (!deviceId) {
      wrap.style.display = "none";
      return;
    }
    wrap.style.display = "";
    const q = new URLSearchParams(params || "");
    q.set("device", deviceId);
    const resp = await fetch(`/api/quality/compare?${q}`);
    const msg = el("q-compare-msg");
    if (!resp.ok) {
      lastCompare = null;
      if (msg) msg.textContent = await errorText(resp);
      renderCompareChart();
      return;
    }
    lastCompare = await resp.json();
    const d = lastCompare.device;
    const info = el("q-compare-device-info");
    if (info) info.textContent = `ostatnia wysyłka: ${fmtWhen(d.last_seen_at)}, przesunięcie zegara: ${fmtSkew(d.last_skew_ms)}`;
    if (msg) {
      msg.textContent = (lastCompare.targets || []).length
        ? ""
        : "Brak wyników tego urządzenia w wybranym zakresie dla celów, które mierzy też serwer.";
    }
    renderCompareTable(lastCompare);
    renderTargetSelect(lastCompare);
    renderCompareChart();
  }

  async function refresh(params) {
    lastParams = params || (typeof paramsFromInputs === "function" ? paramsFromInputs() : null);
    try {
      await loadDevices();
      await refreshCompare(lastParams);
    } catch (err) {
      const msg = el("q-compare-msg");
      if (msg) msg.textContent = String(err && err.message ? err.message : err);
    }
  }

  function init() {
    el("q-device-form")?.addEventListener("submit", onAddDevice);
    el("q-devices-tbody")?.addEventListener("click", onDevicesTableClick);
    el("q-compare-device")?.addEventListener("change", () => refreshCompare(lastParams));
    el("q-compare-target")?.addEventListener("change", renderCompareChart);
  }

  if (typeof document !== "undefined") init();

  return { refresh, fmtSkew };
})();
