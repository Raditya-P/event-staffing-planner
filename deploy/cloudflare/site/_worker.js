/* Cloudflare Pages proxy: serves Event Staffing Planner at a short pages.dev address by forwarding every request to
   the Azure Container App. The app runs with this address as PUBLIC_BASE_URL, so the links, sign-in redirects and
   MCP address it builds point here. Deploy with deploy/cloudflare.ps1. */
const ORIGIN = "https://forecast-mcp.ashyground-d96d5f06.malaysiawest.azurecontainerapps.io";

export default {
  async fetch(request) {
    const url = new URL(request.url);
    const headers = new Headers(request.headers);
    headers.delete("host");
    // The app limits requests per client address, so pass the visitor's address rather than Cloudflare's.
    const ip = request.headers.get("cf-connecting-ip");
    if (ip) headers.set("x-forwarded-for", ip);
    else headers.delete("x-forwarded-for");
    headers.set("x-forwarded-host", url.host);
    headers.set("x-forwarded-proto", "https");
    const init = {
      method: request.method,
      headers,
      redirect: "manual",  // sign-in redirects go back to the browser, not followed here
      cache: "no-store",   // always the current app, never an edge copy from before a deploy
    };
    if (request.method !== "GET" && request.method !== "HEAD") init.body = request.body;
    return fetch(ORIGIN + url.pathname + url.search, init);
  },
};
