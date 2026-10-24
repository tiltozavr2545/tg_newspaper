// Cloudflare Worker: reverse proxy to the Gemini API.
// The script reaches Gemini through this Worker (GEMINI_BASE_URL=https://<name>.<account>.workers.dev),
// and the Worker forwards the request to Google from its own egress IP (usually in a supported region).
// This way the agent's IP (Russia) isn't involved — Google sees Cloudflare's address.
//
// Deploy: Cloudflare Dashboard → Workers & Pages → Create → Worker → paste this code → Deploy.
// (Full instructions — in the README, "Bypassing the geo-block via Cloudflare" section.)

const UPSTREAM = "https://generativelanguage.googleapis.com";

// (optional) a simple secret gate so the Worker isn't an open proxy.
// Set a value and add the same secret in the x-proxy-secret header on the client side.
// Empty = gate disabled.
const PROXY_SECRET = "";

export default {
  async fetch(request) {
    // Only allow Gemini API paths.
    const url = new URL(request.url);
    if (!url.pathname.startsWith("/v1")) {
      return new Response("Not found", { status: 404 });
    }

    if (PROXY_SECRET && request.headers.get("x-proxy-secret") !== PROXY_SECRET) {
      return new Response("Forbidden", { status: 403 });
    }

    const target = UPSTREAM + url.pathname + url.search;
    const headers = new Headers(request.headers);
    headers.delete("host");
    headers.delete("x-proxy-secret");

    const upstreamResp = await fetch(target, {
      method: request.method,
      headers,
      body: ["GET", "HEAD"].includes(request.method) ? undefined : request.body,
    });

    // Stream the response back as-is.
    return new Response(upstreamResp.body, {
      status: upstreamResp.status,
      headers: upstreamResp.headers,
    });
  },
};
