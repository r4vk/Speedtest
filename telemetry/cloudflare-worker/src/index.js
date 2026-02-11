const JSON_HEADERS = { "content-type": "application/json; charset=utf-8" };

function json(data, status = 200) {
  return new Response(JSON.stringify(data), { status, headers: JSON_HEADERS });
}

function isValidInstallId(value) {
  return /^[a-f0-9]{32}$/i.test(value);
}

function isValidVersion(value) {
  return /^[A-Za-z0-9._-]{1,64}$/.test(value);
}

function checkBearerToken(request, expectedToken) {
  if (!expectedToken) return true;
  const auth = request.headers.get("authorization") || "";
  return auth === `Bearer ${expectedToken}`;
}

async function collect(request, env) {
  const payload = await request.json().catch(() => null);
  if (!payload || typeof payload !== "object") {
    return json({ ok: false, error: "Invalid JSON payload" }, 400);
  }

  const event = String(payload.event || "").trim();
  const installId = String(payload.install_id || "").trim();
  const version = String(payload.version || "").trim();

  if (event !== "app_started" && event !== "app_active") {
    return json({ ok: false, error: "Unsupported event type" }, 400);
  }
  if (!isValidInstallId(installId)) {
    return json({ ok: false, error: "Invalid install_id" }, 400);
  }
  if (!isValidVersion(version)) {
    return json({ ok: false, error: "Invalid version" }, 400);
  }

  const receivedAtDate = new Date();
  const receivedAtIso = receivedAtDate.toISOString();
  const receivedAtEpoch = Math.floor(receivedAtDate.getTime() / 1000);
  const day = receivedAtIso.slice(0, 10);

  await env.TELEMETRY_DB.prepare(
    `
      INSERT INTO daily_events (
        day, install_id, event, version,
        first_seen_at, last_seen_at, first_seen_epoch, last_seen_epoch, count
      )
      VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8, 1)
      ON CONFLICT(day, install_id, event) DO UPDATE SET
        last_seen_at = excluded.last_seen_at,
        last_seen_epoch = excluded.last_seen_epoch,
        count = daily_events.count + 1,
        version = excluded.version
    `
  )
    .bind(day, installId, event, version, receivedAtIso, receivedAtIso, receivedAtEpoch, receivedAtEpoch)
    .run();

  return new Response(null, { status: 204 });
}

async function stats(request, env) {
  if (!checkBearerToken(request, env.ADMIN_TOKEN)) {
    return json({ ok: false, error: "Unauthorized" }, 401);
  }

  const nowEpoch = Math.floor(Date.now() / 1000);
  const last24hEpoch = nowEpoch - 24 * 3600;
  const last30dEpoch = nowEpoch - 30 * 24 * 3600;
  const today = new Date().toISOString().slice(0, 10);

  const [active24hRes, activeTodayRes, totalActiveRes, versionsRes, dailyRes] = await Promise.all([
    env.TELEMETRY_DB.prepare(
      "SELECT COUNT(DISTINCT install_id) AS n FROM daily_events WHERE event = 'app_active' AND last_seen_epoch >= ?1"
    )
      .bind(last24hEpoch)
      .first(),
    env.TELEMETRY_DB.prepare(
      "SELECT COUNT(DISTINCT install_id) AS n FROM daily_events WHERE event = 'app_active' AND day = ?1"
    )
      .bind(today)
      .first(),
    env.TELEMETRY_DB.prepare(
      "SELECT COUNT(DISTINCT install_id) AS n FROM daily_events WHERE event = 'app_active'"
    ).first(),
    env.TELEMETRY_DB.prepare(
      `
        SELECT
          version,
          COUNT(DISTINCT install_id) AS installs
        FROM daily_events
        WHERE event = 'app_active' AND last_seen_epoch >= ?1
        GROUP BY version
        ORDER BY installs DESC
        LIMIT 20
      `
    )
      .bind(last30dEpoch)
      .all(),
    env.TELEMETRY_DB.prepare(
      `
        SELECT
          day,
          COUNT(DISTINCT install_id) AS installs
        FROM daily_events
        WHERE event = 'app_active' AND last_seen_epoch >= ?1
        GROUP BY day
        ORDER BY day DESC
      `
    )
      .bind(last30dEpoch)
      .all(),
  ]);

  return json({
    ok: true,
    totals: {
      active_installs_total: Number(totalActiveRes?.n || 0),
      active_installs_today: Number(activeTodayRes?.n || 0),
      active_installs_last_24h: Number(active24hRes?.n || 0),
    },
    versions: versionsRes.results || [],
    daily_last_30d: dailyRes.results || [],
  });
}

export default {
  async fetch(request, env) {
    const url = new URL(request.url);

    if (url.pathname === "/" || url.pathname === "/healthz") {
      return json({ ok: true, service: "speedtest-telemetry-worker" });
    }

    if (url.pathname === "/collect" && request.method === "POST") {
      return collect(request, env);
    }

    if (url.pathname === "/stats" && request.method === "GET") {
      return stats(request, env);
    }

    return json({ ok: false, error: "Not Found" }, 404);
  },
};
