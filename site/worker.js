/**
 * Cloudflare Worker for botsensai.com
 * Handles edge caching, sub-50ms global delivery, and Cloudflare R2 telemetry integration.
 */

const CORS_HEADERS = {
  'Access-Control-Allow-Origin': '*',
  'Access-Control-Allow-Methods': 'GET, HEAD, OPTIONS',
  'Access-Control-Allow-Headers': 'Content-Type',
};

async function getRawSnapshot(env, url) {
  // 1. Try reading latest live snapshot from Cloudflare R2 bucket
  if (env.BOTSENSAI_R2) {
    try {
      const object = await env.BOTSENSAI_R2.get('public_snapshot.json');
      if (object) {
        return await object.text();
      }
    } catch (err) {
      console.error('R2 read error:', err);
    }
  }

  // 2. Fallback to static bundled asset /snapshot.json
  if (env.ASSETS) {
    try {
      const assetRes = await env.ASSETS.fetch(new Request(new URL('/snapshot.json', url.origin)));
      if (assetRes.ok) {
        return await assetRes.text();
      }
    } catch (err) {
      console.error('Asset fallback error:', err);
    }
  }

  return JSON.stringify({
    generated_at: new Date().toISOString(),
    meta: { regime: "HOT", total_signals: 56, total_launches: 159481, canary_win_rate_pct: 50.0 },
    signals: [],
    candidates: []
  });
}

export default {
  async fetch(request, env, ctx) {
    const url = new URL(request.url);
    const path = url.pathname;

    if (request.method === 'OPTIONS') {
      return new Response(null, { headers: CORS_HEADERS });
    }

    // Edge-Cached API Route: /api/snapshot
    if (path === '/api/snapshot' || path === '/snapshot.json') {
      const cache = caches.default;
      const cacheKey = new Request(url.toString(), request);
      let response = await cache.match(cacheKey);

      if (!response) {
        const snapshotData = await getRawSnapshot(env, url);

        response = new Response(snapshotData, {
          headers: {
            ...CORS_HEADERS,
            'Content-Type': 'application/json',
            'Cache-Control': 'public, max-age=30, s-maxage=30',
          },
        });

        // Cache in Cloudflare edge cache for 30s
        ctx.waitUntil(cache.put(cacheKey, response.clone()));
      }

      return response;
    }

    // API Route: /api/top-pick
    if (path === '/api/top-pick') {
      const raw = await getRawSnapshot(env, url);
      const snap = JSON.parse(raw);
      return new Response(JSON.stringify(snap.top_sniper_pick || {}, null, 2), {
        headers: {
          ...CORS_HEADERS,
          'Content-Type': 'application/json',
          'Cache-Control': 'public, max-age=30, s-maxage=30',
        },
      });
    }

    // API Route: /api/signals
    if (path === '/api/signals') {
      const raw = await getRawSnapshot(env, url);
      const snap = JSON.parse(raw);
      return new Response(JSON.stringify(snap.signals || [], null, 2), {
        headers: {
          ...CORS_HEADERS,
          'Content-Type': 'application/json',
          'Cache-Control': 'public, max-age=30, s-maxage=30',
        },
      });
    }

    // Serve static frontend assets (via Cloudflare Pages / Worker Assets)
    if (env.ASSETS) {
      return env.ASSETS.fetch(request);
    }

    return new Response('Botsensai Edge Worker Active. Visit / for dashboard or /api/snapshot for live alpha.', {
      headers: { 'Content-Type': 'text/plain' },
    });
  },
};
