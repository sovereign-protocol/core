/* Framework-neutral JSON client shared by every hosted application. */
window.SovereignApi = Object.freeze({
  async request(path, body) {
    const hasBody = body !== undefined;
    const response = await fetch(path, {
      method: hasBody ? "POST" : "GET",
      headers: hasBody ? { "Content-Type": "application/json" } : {},
      body: hasBody ? JSON.stringify(body) : undefined,
    });
    const payload = await response.json().catch(() => ({}));
    if (!response.ok || payload.status === "error") {
      throw new Error(payload.reason || "Request failed");
    }
    return payload;
  },
});
