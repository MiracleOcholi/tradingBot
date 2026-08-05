// Maverick keep-alive — Cloudflare Worker.
// Pings the app's /health every 5 minutes so the free instance never sleeps
// (Render free spins down after ~15 min idle). Deploy: `wrangler deploy` from
// this folder (see wrangler.toml), or paste into the Cloudflare dashboard and
// add a cron trigger */5 * * * *.
export default {
  async scheduled(event, env, ctx) {
    const url = (env.TARGET_URL || "https://REPLACE-ME.onrender.com") + "/health";
    ctx.waitUntil(
      fetch(url, { headers: { "User-Agent": "maverick-keepalive" } })
        .then(r => console.log(`keepalive ${url} -> ${r.status}`))
        .catch(e => console.error(`keepalive failed: ${e}`))
    );
  },
  // Optional manual check: hitting the worker URL pings once.
  async fetch(request, env) {
    const url = (env.TARGET_URL || "https://REPLACE-ME.onrender.com") + "/health";
    const r = await fetch(url, { headers: { "User-Agent": "maverick-keepalive" } });
    return new Response(`pinged ${url} -> ${r.status}`);
  },
};
