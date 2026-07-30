/* Framework-neutral JSON client shared by every hosted application. */
(function installSovereignApi(global) {
  global.SovereignApi = Object.freeze({
    async request(path, body, options = {}) {
      const hasBody = body !== undefined;
      const response = await fetch(path, {
        method: hasBody ? "POST" : "GET",
        headers: hasBody ? { "Content-Type": "application/json" } : {},
        body: hasBody ? JSON.stringify(body) : undefined,
        signal: options.signal,
      });
      const payload = await response.json().catch(() => ({}));
      if (!response.ok || payload.status === "error") {
        const error = new Error(payload.reason || "Request failed");
        error.status = response.status;
        error.payload = payload;
        error.definitive = (
          (response.ok && payload.status === "error")
          || (
            response.status >= 400
            && response.status < 500
            && ![408, 425, 429].includes(response.status)
          )
        );
        throw error;
      }
      return payload;
    },
  });
})(typeof window === "undefined" ? globalThis : window);
