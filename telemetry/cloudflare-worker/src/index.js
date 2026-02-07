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
  if (!checkBearerToken(request, env.INGEST_TOKEN)) {
    return json({ ok: false, error: "Unauthorized" }, 401);
  }

  const payload = await request.json().catch(() => null);
  if (!payload || typeof payload !== "object") {
    return json({ ok: false, error: "Invalid JSON payload" }, 400);
  }

  const event = String(payload.event || "").trim();
  const installId = String(payload.install_id || "").trim();
  const version = String(payload.version || "").trim();
  const startedAt = String(payload.started_at || "").trim();

  if (event !== "app_started") {
    return json({ ok: false, error: "Unsupported event type" }, 400);
  }
  if (!isValidInstallId(installId)) {
    return json({ ok: false, error: "Invalid install_id" }, 400);
  }
  if (!isValidVersion(version)) {
    return json({ ok: false, error: "Invalid version" }, 400);
  }
  if (Number.isNaN(Date.parse(startedAt))) {
    return json({ ok: false, error: "Invalid started_at" }, 400);
  }

  const receivedAtDate = new Date();
  const receivedAtIso = receivedAtDate.toISOString();
  const receivedAtEpoch = Math.floor(receivedAtDate.getTime() / 1000);

  await env.TELEMETRY_DB.prepare(
    `
      INSERT INTO startup_events (
        install_id, version, event, started_at, received_at, received_at_epoch
      )
      VALUES (?1, ?2, ?3, ?4, ?5, ?6)
    `
  )
    .bind(installId, version, event, startedAt, receivedAtIso, receivedAtEpoch)
    .run();

  return json({ ok: true });
}

async function stats(request, env) {
  if (!checkBearerToken(request, env.ADMIN_TOKEN)) {
    return json({ ok: false, error: "Unauthorized" }, 401);
  }

  const nowEpoch = Math.floor(Date.now() / 1000);
  const last24hEpoch = nowEpoch - 24 * 3600;
  const last30dEpoch = nowEpoch - 30 * 24 * 3600;

  const [totalRes, uniqueRes, last24hRes, versionsRes, dailyRes] = await Promise.all([
    env.TELEMETRY_DB.prepare("SELECT COUNT(*) AS n FROM startup_events").first(),
    env.TELEMETRY_DB.prepare("SELECT COUNT(DISTINCT install_id) AS n FROM startup_events").first(),
    env.TELEMETRY_DB.prepare("SELECT COUNT(*) AS n FROM startup_events WHERE received_at_epoch >= ?1")
      .bind(last24hEpoch)
      .first(),
    env.TELEMETRY_DB.prepare(
      `
        SELECT
          version,
          COUNT(*) AS starts,
          COUNT(DISTINCT install_id) AS installs
        FROM startup_events
        GROUP BY version
        ORDER BY starts DESC
        LIMIT 20
      `
    ).all(),
    env.TELEMETRY_DB.prepare(
      `
        SELECT
          strftime('%Y-%m-%d', received_at) AS day,
          COUNT(*) AS starts,
          COUNT(DISTINCT install_id) AS installs
        FROM startup_events
        WHERE received_at_epoch >= ?1
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
      starts: Number(totalRes?.n || 0),
      unique_installs: Number(uniqueRes?.n || 0),
      starts_last_24h: Number(last24hRes?.n || 0),
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
