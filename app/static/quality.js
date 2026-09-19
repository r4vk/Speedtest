/*
 * Panel "Jakość łącza" — status, wykres, statystyki, incydenty, adnotacje,
 * eksporty i ustawienia monitoringu jakości. Ładowany po app.js: reużywa
 * jego globalnych funkcji (`qs`, `paramsFromInputs`, `parseIsoToMs`,
 * `_escHtml`, `updateCfgDirty`) zamiast duplikować kod.
 *
 * Wszystko, co dotyka DOM/fetch/Chart, żyje wewnątrz funkcji — sam plik da
 * się bezpiecznie wczytać w Node (bez `document`) do testów czystych
 * helperów (`Quality.pickBucketSeconds`, `Quality.buildSumRow`): jedyny kod
 * wykonywany od razu przy wczytaniu to `init()`, i to tylko w przeglądarce.
 */
const Quality = (() => {
  "use strict";

  // ---------------------------------------------------------------------
  // stan modułu
  // ---------------------------------------------------------------------
  let chart = null;
  let lastTimeline = null;
  let targetsCache = [];
  let currentIncidentId = null;
  let lastQualityConfig = null;
  const visibleTargetIds = new Set();
  const knownTargetIds = new Set();

  const TARGET_KINDS = ["gateway", "internet", "dns", "https", "tcp"];
  const TARGET_PROTOCOLS = ["icmp", "tcp", "dns", "https"];
  const CHART_PALETTE = ["#6366f1", "#22c55e", "#f59e0b", "#ef4444", "#06b6d4", "#a855f7", "#eab308", "#14b8a6"];

  // Pola /api/config, którymi zarządza ten moduł (spec §12, quality_settings.py).
  const CONFIG_NUMBER_FIELDS = [
    ["q-cfg-incident-window-seconds", "incident_window_seconds"],
    ["q-cfg-incident-min-samples", "incident_min_samples"],
    ["q-cfg-incident-loss-pct-threshold", "incident_loss_pct_threshold"],
    ["q-cfg-incident-outage-loss-pct", "incident_outage_loss_pct"],
    ["q-cfg-incident-rtt-p95-ms-threshold", "incident_rtt_p95_ms_threshold"],
    ["q-cfg-incident-fail-streak-threshold", "incident_fail_streak_threshold"],
    ["q-cfg-incident-open-windows", "incident_open_windows"],
    ["q-cfg-incident-stabilization-seconds", "incident_stabilization_seconds"],
    ["q-cfg-incident-no-data-close-seconds", "incident_no_data_close_seconds"],
    ["q-cfg-availability-eval-seconds", "availability_eval_seconds"],
    ["q-cfg-availability-window-seconds", "availability_window_seconds"],
    ["q-cfg-load-test-interval-seconds", "load_test_interval_seconds"],
    ["q-cfg-load-test-port", "load_test_port"],
    ["q-cfg-load-test-duration-seconds", "load_test_duration_seconds"],
    ["q-cfg-load-test-datagram-len", "load_test_datagram_len"],
    ["q-cfg-diagnostics-min-interval-seconds", "diagnostics_min_interval_seconds"],
    ["q-cfg-diagnostics-max-per-incident", "diagnostics_max_per_incident"],
    ["q-cfg-diagnostics-max-concurrent", "diagnostics_max_concurrent"],
    ["q-cfg-diagnostics-mtr-count", "diagnostics_mtr_count"],
    ["q-cfg-diagnostics-mtr-timeout-seconds", "diagnostics_mtr_timeout_seconds"],
    ["q-cfg-retention-raw-days", "retention_raw_days"],
    ["q-cfg-retention-aggregate-days", "retention_aggregate_days"],
    ["q-cfg-retention-incident-days", "retention_incident_days"],
    ["q-cfg-retention-load-test-raw-days", "retention_load_test_raw_days"],
    ["q-cfg-retention-diagnostics-days", "retention_diagnostics_days"],
  ];
  const CONFIG_TEXT_FIELDS = [
    ["q-cfg-gateway-host", "gateway_host"],
    ["q-cfg-load-test-server", "load_test_server"],
    ["q-cfg-load-test-udp-bitrate", "load_test_udp_bitrate"],
    ["q-cfg-load-test-directions", "load_test_directions"],
    ["q-cfg-load-test-kind", "load_test_kind"],
  ];
  const CONFIG_BOOL_FIELDS = [
    ["q-cfg-diagnostic-mode", "diagnostic_mode"],
    ["q-cfg-load-test-enabled", "load_test_enabled"],
    ["q-cfg-diagnostics-enabled", "diagnostics_enabled"],
  ];

  // ---------------------------------------------------------------------
  // czyste helpery (bez DOM/fetch) — testowane bezpośrednio przez `node -e`
  // ---------------------------------------------------------------------

  /**
   * Szerokość kubełka wykresu tak, by zmieścić się w ~600 punktach
   * (brief T10): `max(60, ceil(range_seconds/600/60)*60)`.
   */
  function pickBucketSeconds(rangeSeconds) {
    const r = Number(rangeSeconds);
    if (!Number.isFinite(r) || r <= 0) return 60;
    return Math.max(60, Math.ceil(r / 600 / 60) * 60);
  }

  /**
   * Wiersz sumy dla tabeli statystyk: strata liczona z sumy liczników
   * (Σtimeouts / Σ(ok+timeouts)), nigdy jako średnia z gotowych procentów —
   * uśrednienie procentów myli różne wolumeny próbek (spec §1/§6).
   */
  function buildSumRow(entries) {
    let attempts = 0;
    let ok = 0;
    let timeouts = 0;
    let errors = 0;
    for (const entry of entries || []) {
      const s = (entry && entry.stats) || {};
      attempts += Number(s.attempts) || 0;
      ok += Number(s.ok) || 0;
      timeouts += Number(s.timeouts) || 0;
      errors += Number(s.errors) || 0;
    }
    const measurable = ok + timeouts;
    const loss_pct = measurable > 0 ? (timeouts / measurable) * 100 : null;
    return { attempts, ok, timeouts, errors, loss_pct };
  }

  /**
   * Wartość straty dla punktu wykresu: `attempts == 0` to luka (brak
   * pomiaru), nigdy 0 % straty (spec §1/§12).
   */
  function lossValueForPoint(point) {
    if (!point || !point.attempts) return null;
    return typeof point.loss_pct === "number" ? point.loss_pct : null;
  }

  /**
   * Suma liczników per cel (`skipped_ticks`, `restarts` to mapy id -> licznik).
   */
  function sumCounters(map) {
    if (!map || typeof map !== "object") return 0;
    let total = 0;
    for (const value of Object.values(map)) {
      const n = Number(value);
      if (Number.isFinite(n)) total += n;
    }
    return total;
  }

  /**
   * Teksty ostrzeżeń o utraconych pomiarach (spec §12, finding I5): pokrycie
   * jest liczone z sesji i bloków, więc nie wie nic o wierszach odrzuconych
   * przy zapisie — bez tego panel pokazywałby ~100 % pokrycia dla czasu,
   * którego pomiary przepadły. `null` = nie ma czego pokazywać.
   */
  function schedulerWarnings(status) {
    const s = (status && status.scheduler) || {};
    const dropped = Number(s.dropped_rows) || 0;
    const flushErrors = Number(s.flush_errors) || 0;
    const skipped = sumCounters(s.skipped_ticks);
    const restarts = sumCounters(s.restarts);
    return {
      lost: dropped > 0 || flushErrors > 0
        ? `utracone pomiary: ${dropped} (błędy zapisu: ${flushErrors})`
        : null,
      loops: skipped > 0 || restarts > 0
        ? `pominięte ticki: ${skipped} · restarty pętli: ${restarts}`
        : null,
    };
  }

  function fmtNum(value, digits) {
    const d = digits == null ? 1 : digits;
    return typeof value === "number" && Number.isFinite(value) ? value.toFixed(d) : "–";
  }

  function fmtPct(value) {
    return typeof value === "number" && Number.isFinite(value) ? `${value.toFixed(2)}%` : "–";
  }

  function hexToRgba(hex, alpha) {
    const m = /^#?([a-f\d]{2})([a-f\d]{2})([a-f\d]{2})$/i.exec(hex || "");
    if (!m) return `rgba(99,102,241,${alpha})`;
    const r = parseInt(m[1], 16);
    const g = parseInt(m[2], 16);
    const b = parseInt(m[3], 16);
    return `rgba(${r},${g},${b},${alpha})`;
  }

  // ---------------------------------------------------------------------
  // pomocnicze funkcje DOM/sieć (nie wywoływane przy wczytaniu modułu)
  // ---------------------------------------------------------------------

  function setMsg(id, text, ok) {
    const el = qs(id);
    if (!el) return;
    el.textContent = text;
    el.style.color = ok ? "rgba(34,197,94,.95)" : "rgba(239,68,68,.95)";
    setTimeout(() => {
      el.textContent = "";
      el.style.color = "";
    }, 4000);
  }

  async function safeErrorText(resp) {
    try {
      const data = await resp.clone().json();
      if (data && typeof data.detail === "string") return data.detail;
      if (data && data.detail) return JSON.stringify(data.detail);
    } catch {
      /* not JSON */
    }
    try {
      const text = await resp.text();
      if (text) return text;
    } catch {
      /* body already consumed / network gone */
    }
    return `HTTP ${resp.status}`;
  }

  async function fetchJson(url) {
    const resp = await fetch(url);
    if (!resp.ok) {
      throw new Error(await safeErrorText(resp));
    }
    return resp.json();
  }

  function showRangeError(reason) {
    const el = qs("q-range-error");
    if (!el) return;
    el.textContent = reason && reason.message ? reason.message : "Błąd wczytywania danych.";
    el.style.display = "";
  }

  function clearRangeError() {
    const el = qs("q-range-error");
    if (!el) return;
    el.style.display = "none";
    el.textContent = "";
  }

  function showDialog(dlg) {
    if (!dlg) return;
    if (typeof dlg.showModal === "function") dlg.showModal();
    else dlg.setAttribute("open", "open");
  }

  function closeDialog(dlg) {
    if (!dlg) return;
    if (typeof dlg.close === "function") dlg.close();
    else dlg.removeAttribute("open");
  }

  // ---------------------------------------------------------------------
  // pasek statusu
  // ---------------------------------------------------------------------

  const AVAILABILITY_TEXT = { up: "online", down: "offline", no_data: "brak danych" };
  const AVAILABILITY_CLASS = { up: "badge-ok", down: "badge-bad", no_data: "badge-gray" };
  const QUALITY_TEXT = { ok: "ok", degraded: "pogorszona", unknown: "nieznana" };
  const QUALITY_CLASS = { ok: "badge-ok", degraded: "badge-bad", unknown: "badge-gray" };
  const ICMP_METHOD_TEXT = { ping: "ICMP przez ping", unavailable: "ICMP niedostępne — sprawdź uprawnienia kontenera" };
  const BLOCKED_REASON_TEXT = { disabled: "monitor wyłączony", schedule: "blokada harmonogramu" };

  function renderStatus(status) {
    if (!status) return;
    const availBadge = qs("q-badge-availability");
    if (availBadge) {
      availBadge.textContent = `Dostępność: ${AVAILABILITY_TEXT[status.availability] || "brak danych"}`;
      availBadge.className = `badge ${AVAILABILITY_CLASS[status.availability] || "badge-gray"}`;
    }
    const qualityBadge = qs("q-badge-quality");
    if (qualityBadge) {
      qualityBadge.textContent = `Jakość: ${QUALITY_TEXT[status.quality] || "nieznana"}`;
      qualityBadge.className = `badge ${QUALITY_CLASS[status.quality] || "badge-gray"}`;
    }
    const lanBadge = qs("q-badge-lan");
    if (lanBadge) lanBadge.style.display = status.lan_degraded ? "" : "none";

    const icmpHint = qs("q-icmp-hint");
    if (icmpHint) {
      const method = status.icmp_method;
      if (method && method !== "dgram" && method !== "raw" && ICMP_METHOD_TEXT[method]) {
        icmpHint.textContent = ICMP_METHOD_TEXT[method];
        icmpHint.style.display = "";
        icmpHint.classList.toggle("q-hint-warn", method === "unavailable");
      } else {
        icmpHint.style.display = "none";
      }
    }

    const blockedHint = qs("q-blocked-hint");
    if (blockedHint) {
      const reason = status.blocked_reason;
      if (reason && BLOCKED_REASON_TEXT[reason]) {
        blockedHint.textContent = BLOCKED_REASON_TEXT[reason];
        blockedHint.style.display = "";
      } else {
        blockedHint.style.display = "none";
      }
    }

    const coverageEl = qs("q-coverage-24h");
    if (coverageEl) {
      const pct = status.coverage_24h_pct;
      coverageEl.textContent = `pokrycie danych 24h: ${typeof pct === "number" ? pct.toFixed(1) : "–"} %`;
    }

    // Pokrycie nie wie o wierszach odrzuconych przy zapisie — to jedyne
    // miejsce, w którym panel mógłby uznać za "zmierzony" czas, którego
    // pomiary przepadły (finding I5).
    const warnings = schedulerWarnings(status);
    const lostEl = qs("q-lost-rows");
    if (lostEl) {
      lostEl.textContent = warnings.lost || "";
      lostEl.style.display = warnings.lost ? "" : "none";
    }
    const loopsEl = qs("q-loop-hint");
    if (loopsEl) {
      loopsEl.textContent = warnings.loops || "";
      loopsEl.style.display = warnings.loops ? "" : "none";
    }

    const runBtn = qs("q-run-loadtest");
    if (runBtn) {
      if (status.load_test_running) {
        runBtn.disabled = true;
        runBtn.textContent = "Test w toku…";
      } else if (runBtn.textContent === "Test w toku…") {
        runBtn.disabled = false;
        runBtn.textContent = "Uruchom test obciążeniowy";
      }
    }
  }

  // ---------------------------------------------------------------------
  // wykres
  // ---------------------------------------------------------------------

  const qualityBackgroundPlugin = {
    id: "qualityBackgrounds",
    beforeDatasetsDraw(chartInstance) {
      const boxes = chartInstance.$quality && chartInstance.$quality.boxes;
      if (!boxes || !boxes.length) return;
      const { ctx, chartArea, scales } = chartInstance;
      const xScale = scales.x;
      if (!chartArea || !xScale) return;
      ctx.save();
      for (const box of boxes) {
        const px1 = xScale.getPixelForValue(box.from);
        const px2 = xScale.getPixelForValue(box.to);
        if (box.line) {
          const x = Math.max(chartArea.left, Math.min(chartArea.right, px1));
          ctx.save();
          ctx.strokeStyle = box.color;
          ctx.lineWidth = 1.5;
          ctx.setLineDash([4, 4]);
          ctx.beginPath();
          ctx.moveTo(x, chartArea.top);
          ctx.lineTo(x, chartArea.bottom);
          ctx.stroke();
          if (box.label) {
            ctx.setLineDash([]);
            ctx.fillStyle = box.color;
            ctx.font = "10px sans-serif";
            ctx.fillText(box.label, Math.min(x + 3, chartArea.right - 4), chartArea.top + 11);
          }
          ctx.restore();
          continue;
        }
        const left = Math.max(chartArea.left, Math.min(px1, px2));
        const right = Math.min(chartArea.right, Math.max(px1, px2));
        if (right <= left) continue;
        const top = chartArea.top;
        const height = chartArea.bottom - chartArea.top;
        if (box.hatch) {
          ctx.save();
          ctx.beginPath();
          ctx.rect(left, top, right - left, height);
          ctx.clip();
          ctx.fillStyle = box.color;
          ctx.fillRect(left, top, right - left, height);
          ctx.strokeStyle = box.color;
          ctx.lineWidth = 1;
          for (let sx = left - height; sx < right; sx += 6) {
            ctx.beginPath();
            ctx.moveTo(sx, top + height);
            ctx.lineTo(sx + height, top);
            ctx.stroke();
          }
          ctx.restore();
        } else {
          ctx.fillStyle = box.color;
          ctx.fillRect(left, top, right - left, height);
        }
      }
      ctx.restore();
    },
  };

  function buildChartBoxes(timeline) {
    const boxes = [];
    for (const inc of timeline.incidents || []) {
      const from = parseIsoToMs(inc.started_at);
      if (from == null) continue;
      // Open incidents (no ended_at/closed_at yet) highlight through "now" —
      // never collapse to a zero-width box that would look invisible.
      const to = parseIsoToMs(inc.ended_at || inc.closed_at);
      boxes.push({ from, to: to == null ? Date.now() : to, color: "rgba(239,68,68,0.20)" });
    }
    for (const gap of timeline.gaps || []) {
      const from = parseIsoToMs(gap.from);
      const to = parseIsoToMs(gap.to);
      if (from == null || to == null) continue;
      boxes.push({ from, to, color: "rgba(148,163,184,0.28)", hatch: true });
    }
    for (const lt of timeline.load_tests || []) {
      const from = parseIsoToMs(lt.started_at);
      if (from == null) continue;
      const to = parseIsoToMs(lt.ended_at);
      boxes.push({ from, to: to == null ? Date.now() : to, color: "rgba(59,130,246,0.22)" });
    }
    for (const ann of timeline.annotations || []) {
      const at = parseIsoToMs(ann.at);
      if (at == null) continue;
      boxes.push({ from: at, to: at, line: true, color: "rgba(234,179,8,0.9)", label: ann.label || "" });
    }
    return boxes;
  }

  function ensureVisibilityDefaults(targets) {
    const currentIds = new Set(targets.map((t) => t.id));
    for (const id of Array.from(visibleTargetIds)) {
      if (!currentIds.has(id)) visibleTargetIds.delete(id);
    }
    for (const t of targets) {
      if (!knownTargetIds.has(t.id)) {
        knownTargetIds.add(t.id);
        if (t.kind === "internet" || t.kind === "tcp") visibleTargetIds.add(t.id);
      }
    }
  }

  function renderTargetToggles(targets) {
    ensureVisibilityDefaults(targets);
    const container = qs("q-target-toggles");
    if (!container) return;
    container.innerHTML = "";
    for (const t of targets) {
      const label = document.createElement("label");
      label.className = "q-target-toggle";
      const input = document.createElement("input");
      input.type = "checkbox";
      input.checked = visibleTargetIds.has(t.id);
      input.addEventListener("change", () => {
        if (input.checked) visibleTargetIds.add(t.id);
        else visibleTargetIds.delete(t.id);
        if (lastTimeline) renderChart(lastTimeline);
      });
      label.appendChild(input);
      label.appendChild(document.createTextNode(` ${t.name} (${t.protocol})`));
      container.appendChild(label);
    }
  }

  function renderChart(timeline) {
    const canvas = qs("qualityChart");
    if (!canvas || typeof Chart === "undefined") return;
    const entries = timeline.targets || [];
    const datasets = [];
    let colorIdx = 0;
    for (const entry of entries) {
      const t = entry.target || {};
      if (!visibleTargetIds.has(t.id)) continue;
      const color = CHART_PALETTE[colorIdx % CHART_PALETTE.length];
      colorIdx += 1;
      const points = entry.points || [];
      const p95Data = points.map((p) => ({ x: parseIsoToMs(p.t), y: p.attempts ? p.p95 : null }));
      const lossData = points.map((p) => ({ x: parseIsoToMs(p.t), y: lossValueForPoint(p) }));
      datasets.push({
        type: "line",
        label: `${t.name} — p95`,
        yAxisID: "yP95",
        data: p95Data,
        borderColor: color,
        backgroundColor: color,
        borderWidth: 2,
        pointRadius: points.map((p) => (p.partial ? 3 : 1)),
        tension: 0.2,
        spanGaps: false,
        segment: {
          borderDash: (ctx) => {
            const p = points[ctx.p1DataIndex];
            return p && p.partial ? [4, 4] : undefined;
          },
        },
      });
      datasets.push({
        type: "bar",
        label: `${t.name} — strata %`,
        yAxisID: "yLoss",
        data: lossData,
        backgroundColor: points.map((p) => hexToRgba(color, p.partial ? 0.12 : 0.28)),
        borderColor: hexToRgba(color, 0.4),
        borderWidth: 1,
        barPercentage: 0.85,
        categoryPercentage: 0.8,
      });
    }

    const boxes = buildChartBoxes(timeline);
    const legendColor = (getComputedStyle(document.documentElement).getPropertyValue("--text") || "").trim() || "rgba(231,236,255,.9)";
    if (!chart) {
      chart = new Chart(canvas.getContext("2d"), {
        data: { datasets },
        options: {
          responsive: true,
          animation: false,
          interaction: { mode: "nearest", axis: "x", intersect: false },
          scales: {
            x: {
              type: "time",
              ticks: { color: "rgba(194,204,240,.92)", maxRotation: 0, autoSkip: true },
              grid: { color: "rgba(231,236,255,.10)" },
            },
            yP95: {
              position: "left",
              min: 0,
              title: { display: true, text: "p95 RTT (ms)", color: "rgba(169,180,221,.9)" },
              ticks: { color: "rgba(169,180,221,.8)" },
              grid: { color: "rgba(231,236,255,.06)" },
            },
            yLoss: {
              position: "right",
              min: 0,
              max: 100,
              stacked: false,
              title: { display: true, text: "Strata (%)", color: "rgba(169,180,221,.9)" },
              ticks: { color: "rgba(169,180,221,.8)" },
              grid: { drawOnChartArea: false },
            },
          },
          plugins: {
            legend: { labels: { color: legendColor } },
          },
        },
        plugins: [qualityBackgroundPlugin],
      });
    } else {
      chart.data.datasets = datasets;
      chart.options.plugins.legend.labels.color = legendColor;
    }
    chart.$quality = { boxes };
    chart.update();

    const caption = qs("q-chart-caption");
    if (caption) {
      const bucket = timeline.bucket_seconds;
      const lastComplete = timeline.last_complete_bucket;
      const parts = [];
      if (bucket) parts.push(`kubełek: ${bucket >= 60 ? `${Math.round(bucket / 60)} min` : `${bucket} s`}`);
      if (lastComplete) parts.push(`dane do: ${lastComplete}`);
      caption.textContent = parts.join(" · ");
    }
  }

  // ---------------------------------------------------------------------
  // tabela statystyk
  // ---------------------------------------------------------------------

  function buildStatsRow(entry) {
    const tr = document.createElement("tr");
    const t = entry.target || {};
    const s = entry.stats || {};
    const source = entry.data_source;
    const nameCell = `<td>${_escHtml(t.name || "")}</td><td>${_escHtml(t.protocol || "")}</td>`;
    if (source === "none") {
      tr.innerHTML = `${nameCell}<td colspan="12" class="tool-muted" style="font-style:italic;text-align:center;">brak danych</td>`;
      return tr;
    }
    const lossSuffix = source === "aggregates" ? ' <span class="q-tag">(z agregatów)</span>' : "";
    const errorKinds = entry.error_kinds || {};
    const errTitle = Object.entries(errorKinds)
      .map(([k, v]) => `${k}: ${v}`)
      .join(", ");
    tr.innerHTML = `
      ${nameCell}
      <td>${s.attempts ?? 0}</td>
      <td>${s.ok ?? 0}</td>
      <td>${s.timeouts ?? 0}</td>
      <td title="${_escHtml(errTitle)}">${s.errors ?? 0}</td>
      <td>${fmtPct(s.loss_pct)}${lossSuffix}</td>
      <td>${fmtNum(s.rtt_p50_ms)}</td>
      <td>${fmtNum(s.rtt_p95_ms)}</td>
      <td>${fmtNum(s.rtt_p99_ms)}</td>
      <td>${fmtNum(s.rtt_max_ms)}</td>
      <td>${fmtNum(s.rtt_variation_ms)}</td>
      <td>${s.longest_fail_streak ?? 0}</td>
      <td>${_escHtml(entry.note || "")}</td>
    `;
    return tr;
  }

  function renderStats(data) {
    const tbody = qs("q-stats-tbody");
    if (tbody) {
      tbody.innerHTML = "";
      const entries = data.targets || [];
      for (const entry of entries) {
        tbody.appendChild(buildStatsRow(entry));
      }
      const sumRow = qs("q-stats-sum-row");
      if (sumRow) {
        const measured = entries.filter((e) => e.data_source !== "none");
        const sum = buildSumRow(measured);
        sumRow.innerHTML = `
          <td><b>Razem</b></td><td></td>
          <td>${sum.attempts}</td><td>${sum.ok}</td><td>${sum.timeouts}</td><td>${sum.errors}</td>
          <td>${fmtPct(sum.loss_pct)}</td>
          <td>–</td><td>–</td><td>–</td><td>–</td><td>–</td><td>–</td>
          <td class="tool-muted">Σ liczników</td>
        `;
      }
    }
    const legacyWrap = qs("q-legacy-tcp-wrap");
    const legacyTbody = qs("q-legacy-tcp-tbody");
    if (legacyWrap && legacyTbody) {
      if (data.legacy_tcp) {
        legacyWrap.style.display = "";
        legacyTbody.innerHTML = `<tr><td>${data.legacy_tcp.attempts}</td><td>${data.legacy_tcp.failures}</td></tr>`;
      } else {
        legacyWrap.style.display = "none";
        legacyTbody.innerHTML = "";
      }
    }
  }

  // ---------------------------------------------------------------------
  // incydenty + szuflada szczegółów
  // ---------------------------------------------------------------------

  const INCIDENT_KIND_TEXT = { outage: "awaria", degraded: "pogorszenie" };
  const CLOSE_REASON_TEXT = { recovered: "odzyskano", no_data: "brak danych", shutdown: "zamknięcie monitora" };
  const VERDICT_COLOR = { ok: "#22c55e", degraded: "#f59e0b", outage: "#ef4444" };

  function renderIncidents(items) {
    const tbody = qs("q-incidents-tbody");
    const empty = qs("q-incidents-empty");
    if (!tbody) return;
    tbody.innerHTML = "";
    if (!items || !items.length) {
      if (empty) empty.style.display = "";
      return;
    }
    if (empty) empty.style.display = "none";
    for (const inc of items) {
      const tr = document.createElement("tr");
      tr.dataset.incidentId = String(inc.id);
      tr.tabIndex = 0;
      tr.className = "q-incident-row";
      tr.innerHTML = `
        <td>${_escHtml(inc.started_at || "")}</td>
        <td>${_escHtml(inc.ended_at || "otwarty")}</td>
        <td>${_escHtml(inc.target_name || String(inc.target_id ?? ""))}</td>
        <td>${_escHtml(INCIDENT_KIND_TEXT[inc.kind] || inc.kind || "")}</td>
        <td>${fmtPct(inc.peak_loss_pct)}</td>
        <td>${fmtNum(inc.peak_p95_rtt_ms)}</td>
        <td>${_escHtml(CLOSE_REASON_TEXT[inc.close_reason] || inc.close_reason || "-")}</td>
      `;
      tbody.appendChild(tr);
    }
  }

  function renderWindowsStrip(windows) {
    if (!windows || !windows.length) return '<p class="hint">Brak danych o oknach.</p>';
    const chips = windows
      .map((w) => {
        const color = VERDICT_COLOR[w.verdict] || "#64748b";
        const title = `${w.window_start || ""}: ${w.verdict || "?"} (strata ${fmtPct(w.loss_pct)}, p95 ${fmtNum(w.p95)} ms)`;
        return `<span class="q-window-chip" style="background:${color}" title="${_escHtml(title)}"></span>`;
      })
      .join("");
    return `<div class="q-window-strip">${chips}</div>`;
  }

  function parseDiagResult(diag) {
    if (diag && typeof diag.result === "object" && diag.result !== null) return diag.result;
    if (diag && typeof diag.result_json === "string") {
      try {
        return JSON.parse(diag.result_json);
      } catch {
        return null;
      }
    }
    return null;
  }

  function renderDiagnostics(diagnostics) {
    if (!diagnostics || !diagnostics.length) return '<p class="hint">Brak diagnostyki dla tego incydentu.</p>';
    return diagnostics
      .map((diag) => {
        const result = parseDiagResult(diag) || {};
        const hops = result.hops || result.report || [];
        let html = `<div class="q-diag-entry"><div class="q-diag-head">${_escHtml(diag.tool || "diagnostyka")} — ${_escHtml(diag.started_at || "")} — ${_escHtml(diag.status || "")}</div>`;
        if (diag.error) html += `<div class="tool-error">${_escHtml(diag.error)}</div>`;
        if (hops.length) {
          html += '<table class="tool-table"><tr><th scope="col">#</th><th scope="col">Host</th><th scope="col">Strata %</th><th scope="col">Śr. RTT</th></tr>';
          hops.forEach((h, i) => {
            html += `<tr><td>${i + 1}</td><td>${_escHtml(h.host || h.ip || "*")}</td><td>${fmtPct(h.loss_pct)}</td><td>${fmtNum(h.avg_ms)}</td></tr>`;
          });
          html += "</table>";
        }
        const hypotheses = result.hypotheses || [];
        if (hypotheses.length) {
          html += `<ul class="q-hypotheses">${hypotheses
            .map((h) => `<li>Hipoteza: ${_escHtml(typeof h === "string" ? h : h.text || JSON.stringify(h))}</li>`)
            .join("")}</ul>`;
        }
        html += "</div>";
        return html;
      })
      .join("");
  }

  function renderRelatedTargets(entries) {
    if (!entries || !entries.length) return '<p class="hint">Brak powiązanych celów.</p>';
    let html = '<table class="tool-table"><tr><th scope="col">Cel</th><th scope="col">Strata %</th><th scope="col">p95</th></tr>';
    for (const entry of entries) {
      const t = entry.target || {};
      const s = entry.stats || {};
      html += `<tr><td>${_escHtml(t.name || "")}</td><td>${fmtPct(s.loss_pct)}</td><td>${fmtNum(s.rtt_p95_ms)}</td></tr>`;
    }
    html += "</table>";
    return html;
  }

  function renderIncidentLoadTests(loadTests) {
    if (!loadTests || !loadTests.length) return '<p class="hint">Brak testów obciążeniowych w tym oknie.</p>';
    let html = '<table class="tool-table"><tr><th scope="col">Start</th><th scope="col">Rodzaj</th><th scope="col">Kierunek</th><th scope="col">Status</th></tr>';
    for (const lt of loadTests) {
      html += `<tr><td>${_escHtml(lt.started_at || "")}</td><td>${_escHtml(lt.kind || "")}</td><td>${_escHtml(lt.direction || "")}</td><td>${_escHtml(lt.status || "")}</td></tr>`;
    }
    html += "</table>";
    return html;
  }

  function renderIncidentAnnotations(annotations) {
    if (!annotations || !annotations.length) return '<p class="hint">Brak adnotacji.</p>';
    return annotations
      .map(
        (a) =>
          `<div class="q-annotation-item"><span class="badge badge-gray">zgłoszenie użytkownika</span> <i>${_escHtml(a.at || "")} — ${_escHtml(a.label || "")}</i>${a.note ? `: ${_escHtml(a.note)}` : ""}</div>`
      )
      .join("");
  }

  function renderIncidentDetail(data) {
    const inc = data.incident || {};
    return [
      `<p><b>Cel:</b> ${_escHtml(inc.target_name || "")} · <b>Rodzaj:</b> ${_escHtml(INCIDENT_KIND_TEXT[inc.kind] || inc.kind || "")} · <b>Start:</b> ${_escHtml(inc.started_at || "")} · <b>Koniec:</b> ${_escHtml(inc.ended_at || "otwarty")}</p>`,
      "<h4>Przebieg okien</h4>",
      renderWindowsStrip(data.windows),
      "<h4>Diagnostyka</h4>",
      renderDiagnostics(data.diagnostics),
      "<h4>Powiązane cele</h4>",
      renderRelatedTargets(data.related_targets),
      "<h4>Testy obciążeniowe w tym czasie</h4>",
      renderIncidentLoadTests(data.load_tests),
      "<h4>Adnotacje</h4>",
      renderIncidentAnnotations(data.annotations),
    ].join("\n");
  }

  async function openIncidentDrawer(id) {
    currentIncidentId = id;
    const dlg = qs("q-incident-drawer");
    const body = qs("q-incident-body");
    const title = qs("q-incident-title");
    if (!dlg || !body) return;
    if (title) title.textContent = `Incydent #${id}`;
    body.innerHTML = '<p class="hint">Wczytywanie…</p>';
    showDialog(dlg);
    try {
      const data = await fetchJson(`/api/quality/incidents/${id}`);
      body.innerHTML = renderIncidentDetail(data);
    } catch (e) {
      body.innerHTML = `<p class="tool-error">Błąd wczytywania incydentu: ${_escHtml(e.message)}</p>`;
    }
  }

  function closeIncidentDrawer() {
    closeDialog(qs("q-incident-drawer"));
  }

  // ---------------------------------------------------------------------
  // adnotacje
  // ---------------------------------------------------------------------

  function renderAnnotationsList(annotations) {
    const el = qs("q-annotations-list");
    if (!el) return;
    el.innerHTML = "";
    if (!annotations || !annotations.length) {
      el.innerHTML = '<p class="hint">Brak adnotacji w wybranym zakresie.</p>';
      return;
    }
    for (const ann of annotations) {
      const div = document.createElement("div");
      div.className = "q-annotation-item";

      const text = document.createElement("span");
      text.className = "q-annotation-text";
      text.innerHTML = `<span class="badge badge-gray">zgłoszenie użytkownika</span> <i>${_escHtml(ann.at || "")} — ${_escHtml(ann.label || "")}</i>${ann.note ? `: ${_escHtml(ann.note)}` : ""}`;

      const delBtn = document.createElement("button");
      delBtn.type = "button";
      delBtn.className = "btn-remove";
      delBtn.textContent = "×";
      delBtn.setAttribute("aria-label", `Usuń adnotację: ${ann.label || ""}`);
      delBtn.addEventListener("click", async () => {
        if (!confirm("Usunąć tę adnotację?")) return;
        try {
          const resp = await fetch(`/api/quality/annotations/${ann.id}`, { method: "DELETE" });
          if (!resp.ok) throw new Error(await safeErrorText(resp));
          await refresh();
        } catch (e) {
          alert(`Błąd usuwania adnotacji: ${e.message}`);
        }
      });

      div.appendChild(text);
      div.appendChild(delBtn);
      el.appendChild(div);
    }
  }

  async function onAnnotationSubmit(e) {
    e.preventDefault();
    const atRaw = qs("q-annotation-at")?.value.trim() || "";
    const label = qs("q-annotation-label")?.value || "inne";
    const note = qs("q-annotation-note")?.value.trim() || "";
    const body = { label, note: note || null };
    if (atRaw) body.at = atRaw;
    try {
      const resp = await fetch("/api/quality/annotations", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      });
      if (!resp.ok) throw new Error(await safeErrorText(resp));
      setMsg("q-annotation-msg", "Dodano zgłoszenie.", true);
      const noteEl = qs("q-annotation-note");
      if (noteEl) noteEl.value = "";
      await refresh();
    } catch (err) {
      setMsg("q-annotation-msg", `Błąd: ${err.message}`, false);
    }
  }

  async function onIncidentAnnotationSubmit(e) {
    e.preventDefault();
    if (currentIncidentId == null) return;
    const label = qs("q-incident-annotation-label")?.value || "inne";
    const note = qs("q-incident-annotation-note")?.value.trim() || "";
    try {
      const resp = await fetch("/api/quality/annotations", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ label, note: note || null, incident_id: currentIncidentId }),
      });
      if (!resp.ok) throw new Error(await safeErrorText(resp));
      setMsg("q-incident-annotation-msg", "Dodano objaw.", true);
      const noteEl = qs("q-incident-annotation-note");
      if (noteEl) noteEl.value = "";
      const openId = currentIncidentId;
      await openIncidentDrawer(openId);
      await refresh();
    } catch (err) {
      setMsg("q-incident-annotation-msg", `Błąd: ${err.message}`, false);
    }
  }

  // ---------------------------------------------------------------------
  // cele pomiarowe (ustawienia)
  // ---------------------------------------------------------------------

  function buildTargetRow(target) {
    const tr = document.createElement("tr");
    tr.dataset.targetId = String(target.id);
    const label = target.name || `cel #${target.id}`;

    const makeInput = (field, type, value, ariaLabel) => {
      const td = document.createElement("td");
      const input = document.createElement("input");
      input.type = type;
      input.dataset.field = field;
      input.setAttribute("aria-label", `${ariaLabel} — ${label}`);
      if (type === "checkbox") input.checked = Boolean(value);
      else input.value = value == null ? "" : String(value);
      if (type === "number") {
        input.step = field === "interval_seconds" ? "0.1" : "1";
      }
      td.appendChild(input);
      return td;
    };

    const makeSelect = (field, options, current, ariaLabel) => {
      const td = document.createElement("td");
      const select = document.createElement("select");
      select.dataset.field = field;
      select.setAttribute("aria-label", `${ariaLabel} — ${label}`);
      for (const opt of options) {
        const o = document.createElement("option");
        o.value = opt;
        o.textContent = opt;
        if (opt === current) o.selected = true;
        select.appendChild(o);
      }
      td.appendChild(select);
      return td;
    };

    const actionsTd = document.createElement("td");
    const saveBtn = document.createElement("button");
    saveBtn.type = "button";
    saveBtn.className = "btn-small";
    saveBtn.textContent = "Zapisz";
    saveBtn.dataset.action = "save";
    saveBtn.setAttribute("aria-label", `Zapisz cel ${label}`);
    const delBtn = document.createElement("button");
    delBtn.type = "button";
    delBtn.className = "btn-remove";
    delBtn.textContent = "×";
    delBtn.dataset.action = "delete";
    delBtn.setAttribute("aria-label", `Usuń cel ${label}`);
    actionsTd.appendChild(saveBtn);
    actionsTd.appendChild(delBtn);

    tr.appendChild(makeInput("name", "text", target.name, "Nazwa"));
    tr.appendChild(makeSelect("kind", TARGET_KINDS, target.kind, "Rodzaj"));
    tr.appendChild(makeSelect("protocol", TARGET_PROTOCOLS, target.protocol, "Protokół"));
    tr.appendChild(makeInput("host", "text", target.host, "Host"));
    tr.appendChild(makeInput("port", "number", target.port, "Port"));
    tr.appendChild(makeInput("interval_seconds", "number", target.interval_seconds, "Interwał w sekundach"));
    tr.appendChild(makeInput("timeout_ms", "number", target.timeout_ms, "Timeout w milisekundach"));
    tr.appendChild(makeInput("enabled", "checkbox", target.enabled, "Włączony"));
    tr.appendChild(actionsTd);
    return tr;
  }

  function renderTargetsTable(targets) {
    const tbody = qs("q-targets-tbody");
    if (!tbody) return;
    tbody.innerHTML = "";
    for (const t of targets) {
      tbody.appendChild(buildTargetRow(t));
    }
  }

  async function refreshTargetsTable() {
    try {
      const data = await fetchJson("/api/targets");
      targetsCache = data.items || [];
      renderTargetsTable(targetsCache);
      return targetsCache;
    } catch (e) {
      setMsg("q-targets-msg", `Błąd wczytywania celów: ${e.message}`, false);
      return targetsCache;
    }
  }

  function readTargetRow(tr) {
    const get = (field) => tr.querySelector(`[data-field="${field}"]`);
    const portVal = get("port") ? get("port").value : "";
    return {
      name: get("name") ? get("name").value.trim() : "",
      kind: get("kind") ? get("kind").value : "internet",
      protocol: get("protocol") ? get("protocol").value : "icmp",
      host: get("host") ? get("host").value.trim() : "",
      port: portVal ? Number(portVal) : null,
      interval_seconds: Number(get("interval_seconds") ? get("interval_seconds").value : 1),
      timeout_ms: Number(get("timeout_ms") ? get("timeout_ms").value : 1000),
      enabled: get("enabled") ? Boolean(get("enabled").checked) : true,
    };
  }

  async function onTargetsTableClick(e) {
    const btn = e.target.closest("button[data-action]");
    if (!btn) return;
    const tr = btn.closest("tr[data-target-id]");
    if (!tr) return;
    const id = Number(tr.dataset.targetId);
    if (btn.dataset.action === "delete") {
      const nameInput = tr.querySelector('[data-field="name"]');
      const label = nameInput ? nameInput.value : String(id);
      if (!confirm(`Usunąć cel "${label}"?`)) return;
      try {
        const resp = await fetch(`/api/targets/${id}`, { method: "DELETE" });
        if (!resp.ok) throw new Error(await safeErrorText(resp));
        setMsg("q-targets-msg", "Usunięto cel.", true);
        await refreshTargetsTable();
        await refresh();
      } catch (err) {
        setMsg("q-targets-msg", `Błąd usuwania: ${err.message}`, false);
      }
    } else if (btn.dataset.action === "save") {
      const payload = readTargetRow(tr);
      try {
        const resp = await fetch(`/api/targets/${id}`, {
          method: "PUT",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(payload),
        });
        if (!resp.ok) throw new Error(await safeErrorText(resp));
        setMsg("q-targets-msg", "Zapisano cel.", true);
        await refreshTargetsTable();
      } catch (err) {
        setMsg("q-targets-msg", `Błąd zapisu: ${err.message}`, false);
      }
    }
  }

  async function onAddTarget() {
    const nameEl = qs("q-target-new-name");
    const kindEl = qs("q-target-new-kind");
    const protocolEl = qs("q-target-new-protocol");
    const hostEl = qs("q-target-new-host");
    const portEl = qs("q-target-new-port");
    const intervalEl = qs("q-target-new-interval");
    const timeoutEl = qs("q-target-new-timeout");
    const payload = {
      name: nameEl ? nameEl.value.trim() : "",
      kind: kindEl ? kindEl.value : "internet",
      protocol: protocolEl ? protocolEl.value : "icmp",
      host: hostEl ? hostEl.value.trim() : "",
      port: portEl && portEl.value ? Number(portEl.value) : null,
      interval_seconds: Number(intervalEl && intervalEl.value ? intervalEl.value : 1),
      timeout_ms: Number(timeoutEl && timeoutEl.value ? timeoutEl.value : 1000),
      enabled: true,
    };
    if (!payload.name || !payload.host) {
      setMsg("q-targets-msg", "Podaj nazwę i host celu.", false);
      return;
    }
    try {
      const resp = await fetch("/api/targets", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      });
      if (!resp.ok) throw new Error(await safeErrorText(resp));
      setMsg("q-targets-msg", "Dodano cel.", true);
      if (nameEl) nameEl.value = "";
      if (hostEl) hostEl.value = "";
      if (portEl) portEl.value = "";
      await refreshTargetsTable();
    } catch (err) {
      setMsg("q-targets-msg", `Błąd dodawania: ${err.message}`, false);
    }
  }

  // ---------------------------------------------------------------------
  // eksporty + test obciążeniowy
  // ---------------------------------------------------------------------

  function refreshExportLinks(params) {
    const q = params.toString();
    const suffix = q ? `?${q}` : "";
    const reportEl = qs("q-export-report");
    if (reportEl) reportEl.href = `/api/quality/report.html${suffix}`;
    const probesEl = qs("q-export-probes");
    if (probesEl) probesEl.href = `/api/quality/export/probes.csv${suffix}`;
    const incidentsEl = qs("q-export-incidents");
    if (incidentsEl) incidentsEl.href = `/api/quality/export/incidents.csv${suffix}`;
    const loadtestsEl = qs("q-export-loadtests");
    if (loadtestsEl) loadtestsEl.href = `/api/quality/export/load-tests.csv${suffix}`;
    const aggEl = qs("q-export-aggregates");
    if (aggEl) {
      const aggParams = new URLSearchParams(params);
      aggParams.set("bucket", "1h");
      aggEl.href = `/api/quality/export/aggregates.csv?${aggParams.toString()}`;
    }
  }

  async function onRunLoadTest() {
    const btn = qs("q-run-loadtest");
    if (!btn || btn.disabled) return;
    const original = btn.textContent;
    btn.disabled = true;
    btn.textContent = "Uruchamianie…";
    try {
      const resp = await fetch("/api/quality/load-tests/run", { method: "POST" });
      if (!resp.ok) {
        setMsg("q-loadtest-msg", await safeErrorText(resp), false);
      } else {
        setMsg("q-loadtest-msg", "Uruchomiono test obciążeniowy.", true);
        await refresh();
      }
    } catch (e) {
      setMsg("q-loadtest-msg", `Błąd sieci: ${e.message}`, false);
    } finally {
      btn.disabled = false;
      btn.textContent = original;
    }
  }

  // ---------------------------------------------------------------------
  // ustawienia (config)
  // ---------------------------------------------------------------------

  function configPayload() {
    const payload = {};
    for (const [id, key] of CONFIG_NUMBER_FIELDS) {
      const el = qs(id);
      if (!el) continue;
      const n = Number(el.value);
      payload[key] = Number.isFinite(n) ? n : null;
    }
    for (const [id, key] of CONFIG_TEXT_FIELDS) {
      const el = qs(id);
      if (!el) continue;
      payload[key] = el.value.trim();
    }
    for (const [id, key] of CONFIG_BOOL_FIELDS) {
      const el = qs(id);
      if (!el) continue;
      payload[key] = Boolean(el.checked);
    }
    return payload;
  }

  function applyConfig(cfg) {
    if (!cfg) return;
    for (const [id, key] of CONFIG_NUMBER_FIELDS) {
      const el = qs(id);
      if (el && cfg[key] != null) el.value = cfg[key];
    }
    for (const [id, key] of CONFIG_TEXT_FIELDS) {
      const el = qs(id);
      if (el) el.value = cfg[key] ?? "";
    }
    for (const [id, key] of CONFIG_BOOL_FIELDS) {
      const el = qs(id);
      if (el) el.checked = Boolean(cfg[key]);
    }
    lastQualityConfig = configPayload();
  }

  function isDirty() {
    if (!lastQualityConfig) return false;
    const current = configPayload();
    for (const key of Object.keys(current)) {
      if (String(current[key]) !== String(lastQualityConfig[key])) return true;
    }
    return false;
  }

  function wireConfigDirtyTracking() {
    const notify = () => {
      if (typeof updateCfgDirty === "function") updateCfgDirty();
    };
    for (const [id] of CONFIG_NUMBER_FIELDS.concat(CONFIG_TEXT_FIELDS)) {
      qs(id)?.addEventListener("input", notify);
    }
    for (const [id] of CONFIG_BOOL_FIELDS) {
      qs(id)?.addEventListener("change", notify);
    }
  }

  // ---------------------------------------------------------------------
  // odświeżanie całości
  // ---------------------------------------------------------------------

  function computeBucketSeconds(params) {
    const fromStr = params.get("from");
    const toStr = params.get("to");
    const endMs = toStr ? parseIsoToMs(toStr) : Date.now();
    const end = endMs == null ? Date.now() : endMs;
    const startMs = fromStr ? parseIsoToMs(fromStr) : end - 24 * 3600 * 1000;
    const start = startMs == null ? end - 24 * 3600 * 1000 : startMs;
    const rangeSeconds = Math.max(0, (end - start) / 1000);
    return pickBucketSeconds(rangeSeconds);
  }

  async function refresh(paramsInput) {
    const params = paramsInput instanceof URLSearchParams ? paramsInput : paramsFromInputs();
    refreshExportLinks(params);
    clearRangeError();

    const bucket = computeBucketSeconds(params);
    const timelineParams = new URLSearchParams(params);
    timelineParams.set("bucket_seconds", String(bucket));

    const [statusResult, statsResult, timelineResult, incidentsResult, targetsResult] = await Promise.allSettled([
      fetchJson("/api/quality/status"),
      fetchJson(`/api/quality/stats?${params.toString()}`),
      fetchJson(`/api/quality/timeline?${timelineParams.toString()}`),
      fetchJson(`/api/quality/incidents?${params.toString()}`),
      fetchJson("/api/targets"),
    ]);

    if (statusResult.status === "fulfilled") {
      renderStatus(statusResult.value);
    } else {
      showRangeError(statusResult.reason);
    }

    if (targetsResult.status === "fulfilled") {
      targetsCache = targetsResult.value.items || [];
      renderTargetsTable(targetsCache);
    } else {
      showRangeError(targetsResult.reason);
    }

    if (statsResult.status === "fulfilled") {
      renderStats(statsResult.value);
    } else {
      showRangeError(statsResult.reason);
    }

    if (timelineResult.status === "fulfilled") {
      lastTimeline = timelineResult.value;
      renderTargetToggles((lastTimeline.targets || []).map((e) => e.target));
      renderChart(lastTimeline);
      renderAnnotationsList(lastTimeline.annotations || []);
    } else {
      showRangeError(timelineResult.reason);
    }

    if (incidentsResult.status === "fulfilled") {
      renderIncidents(incidentsResult.value.items || []);
    } else {
      showRangeError(incidentsResult.reason);
    }
  }

  // ---------------------------------------------------------------------
  // inicjalizacja (tylko w przeglądarce)
  // ---------------------------------------------------------------------

  function init() {
    wireConfigDirtyTracking();
    qs("q-annotation-form")?.addEventListener("submit", onAnnotationSubmit);
    qs("q-incident-annotation-form")?.addEventListener("submit", onIncidentAnnotationSubmit);
    qs("q-target-add")?.addEventListener("click", onAddTarget);
    qs("q-targets-tbody")?.addEventListener("click", onTargetsTableClick);
    qs("q-run-loadtest")?.addEventListener("click", onRunLoadTest);

    const incidentsBody = qs("q-incidents-tbody");
    incidentsBody?.addEventListener("click", (e) => {
      const tr = e.target.closest("tr[data-incident-id]");
      if (tr) openIncidentDrawer(Number(tr.dataset.incidentId));
    });
    incidentsBody?.addEventListener("keydown", (e) => {
      if (e.key !== "Enter" && e.key !== " ") return;
      const tr = e.target.closest("tr[data-incident-id]");
      if (!tr) return;
      e.preventDefault();
      openIncidentDrawer(Number(tr.dataset.incidentId));
    });

    qs("q-incident-close")?.addEventListener("click", closeIncidentDrawer);
    const drawer = qs("q-incident-drawer");
    drawer?.addEventListener("click", (e) => {
      if (e.target === drawer) closeIncidentDrawer();
    });

    if (typeof flatpickr === "function") {
      flatpickr(qs("q-annotation-at"), {
        enableTime: true,
        time_24hr: true,
        enableSeconds: true,
        allowInput: true,
        dateFormat: "Y-m-d H:i:S",
      });
    }
  }

  if (typeof document !== "undefined") {
    init();
  }

  return {
    refresh,
    configPayload,
    applyConfig,
    isDirty,
    refreshTargetsTable,
    pickBucketSeconds,
    buildSumRow,
    lossValueForPoint,
    schedulerWarnings,
  };
})();
