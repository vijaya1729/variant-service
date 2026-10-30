(function () {
  "use strict";

  const STORAGE_KEY = "variants_visitor_id";
  const COOKIE_MAX_AGE_SECONDS = 31536000;
  const ASSIGN_TIMEOUT_MS = 200;

  const script = document.currentScript;
  let endpoint = script && script.src ? new URL(script.src).origin : "";
  let memoryVisitorId = null;

  function randomUUID() {
    // crypto.randomUUID only exists on secure origins.
    if (crypto.randomUUID) return crypto.randomUUID();
    const bytes = crypto.getRandomValues(new Uint8Array(16));
    bytes[6] = (bytes[6] & 0x0f) | 0x40;
    bytes[8] = (bytes[8] & 0x3f) | 0x80;
    const hex = Array.from(bytes, (byte) => byte.toString(16).padStart(2, "0")).join("");
    return [hex.slice(0, 8), hex.slice(8, 12), hex.slice(12, 16), hex.slice(16, 20), hex.slice(20)].join("-");
  }

  function readStorage() {
    try {
      return localStorage.getItem(STORAGE_KEY);
    } catch (error) {
      return null;
    }
  }

  function writeStorage(visitorId) {
    try {
      localStorage.setItem(STORAGE_KEY, visitorId);
      return true;
    } catch (error) {
      return false;
    }
  }

  function readCookie() {
    try {
      const match = document.cookie.match(new RegExp("(?:^|; )" + STORAGE_KEY + "=([^;]+)"));
      return match ? match[1] : null;
    } catch (error) {
      return null;
    }
  }

  function writeCookie(value, maxAge) {
    try {
      const secure = location.protocol === "https:" ? "; Secure" : "";
      document.cookie = `${STORAGE_KEY}=${value}; Max-Age=${maxAge}; Path=/; SameSite=Lax${secure}`;
    } catch (error) {
      // No storage at all: the ID lasts for this page view.
    }
  }

  function visitorId() {
    if (memoryVisitorId) return memoryVisitorId;
    memoryVisitorId = readStorage() || readCookie() || randomUUID();
    if (!writeStorage(memoryVisitorId)) writeCookie(memoryVisitorId, COOKIE_MAX_AGE_SECONDS);
    return memoryVisitorId;
  }

  function resetVisitor() {
    memoryVisitorId = null;
    try {
      localStorage.removeItem(STORAGE_KEY);
    } catch (error) {
      // Storage is blocked, so there's nothing to clear.
    }
    writeCookie("", 0);
  }

  function beacon(path, event) {
    try {
      // A string body is sent as text/plain, which needs no CORS preflight.
      navigator.sendBeacon(endpoint + path, JSON.stringify(event));
    } catch (error) {
      // Tracking must never break the page.
    }
  }

  function allNull(experiments) {
    return Object.fromEntries(experiments.map((id) => [id, null]));
  }

  async function fetchAssignments(visitor, experiments) {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), ASSIGN_TIMEOUT_MS);
    try {
      const response = await fetch(endpoint + "/assign", {
        method: "POST",
        // A string body is sent as text/plain, so cross-origin pages skip the CORS preflight.
        body: JSON.stringify({ visitor_id: visitor, experiment_ids: experiments }),
        signal: controller.signal,
      });
      if (!response.ok) throw new Error(`assign returned ${response.status}`);
      const { assignments } = await response.json();
      return { assignments: { ...allNull(experiments), ...assignments }, error: null };
    } catch (error) {
      return { assignments: allNull(experiments), error: error.name === "AbortError" ? "timeout" : "unavailable" };
    } finally {
      clearTimeout(timer);
    }
  }

  async function run(options) {
    const experiments = options.experiments;
    if (options.endpoint !== undefined) endpoint = options.endpoint.replace(/\/$/, "");

    const visitor = visitorId();
    const started = performance.now();
    const { assignments, error } = await fetchAssignments(visitor, experiments);
    const latencyMs = performance.now() - started;

    // Outside fetchAssignments, so a throwing callback isn't treated as an assign failure
    // and called a second time. No exposure either way: it may not have rendered.
    let applied;
    try {
      applied = options.onAssign(assignments, { visitorId: visitor, latencyMs, error });
    } catch (callbackError) {
      console.error(callbackError);
      return;
    }
    if (applied === false) return;

    for (const id of experiments) {
      if (assignments[id]) beacon("/expose", { experiment_id: id, visitor_id: visitor });
    }
  }

  function convert(experimentId, goal) {
    beacon("/convert", { experiment_id: experimentId, visitor_id: visitorId(), goal });
  }

  window.variants = { run, convert, visitorId, resetVisitor };
})();
