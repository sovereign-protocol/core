/*
  Shared UI kit - loaded before each page's own inline <script>. Every page
  must provide:
    - <div id="toast" class="toast"></div>            (for showToast)
    - <dialog id="confirmModal"> with #confirmModalTitle, #confirmModalMessage,
      #confirmModalCancelBtn, #confirmModalConfirmBtn (for confirmAction)
  peerLabel() stays page-specific (each page's state shape differs) - these
  helpers just call the global peerLabel() the page itself defines.
*/

const ICON_CLOSE = '<path d="M18 6 6 18"></path><path d="M6 6l12 12"></path>';
const ICON_CHEVRON_DOWN = '<path d="M6 9l6 6 6-6"></path>';
const ICON_SETTINGS =
  '<circle cx="12" cy="12" r="3"></circle>' +
  '<path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 1 1-2.83 2.83l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 0 1-4 0v-.09A1.65 1.65 0 0 0 9 19.4a1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 1 1-2.83-2.83l.06-.06a1.65 1.65 0 0 0 .33-1.82 1.65 1.65 0 0 0-1.51-1H3a2 2 0 0 1 0-4h.09A1.65 1.65 0 0 0 4.6 9a1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 1 1 2.83-2.83l.06.06a1.65 1.65 0 0 0 1.82.33H9a1.65 1.65 0 0 0 1-1.51V3a2 2 0 0 1 4 0v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 1 1 2.83 2.83l-.06.06a1.65 1.65 0 0 0-.33 1.82V9a1.65 1.65 0 0 0 1.51 1H21a2 2 0 0 1 0 4h-.09a1.65 1.65 0 0 0-1.51 1z"></path>';
const ICON_EXPAND =
  '<path d="M8 3H5a2 2 0 0 0-2 2v3"></path><path d="M21 8V5a2 2 0 0 0-2-2h-3"></path>' +
  '<path d="M3 16v3a2 2 0 0 0 2 2h3"></path><path d="M16 21h3a2 2 0 0 0 2-2v-3"></path>';
const ICON_COLLAPSE =
  '<path d="M8 3v3a2 2 0 0 1-2 2H3"></path><path d="M21 8h-3a2 2 0 0 1-2-2V3"></path>' +
  '<path d="M3 16h3a2 2 0 0 1 2 2v3"></path><path d="M16 21v-3a2 2 0 0 1 2-2h3"></path>';
const ICON_DELETE =
  '<path d="M4 7h16"></path><path d="M10 11v6"></path><path d="M14 11v6"></path>' +
  '<path d="M6 7l1 12a2 2 0 0 0 2 2h6a2 2 0 0 0 2-2l1-12"></path>' +
  '<path d="M9 7V4a1 1 0 0 1 1-1h4a1 1 0 0 1 1 1v3"></path>';
const ICON_SHARE =
  '<circle cx="18" cy="5" r="3"></circle><circle cx="6" cy="12" r="3"></circle>' +
  '<circle cx="18" cy="19" r="3"></circle>' +
  '<path d="M8.59 13.51 15.42 17.49"></path><path d="M15.41 6.51 8.59 10.49"></path>';

function iconButton(svgInner, label, action) {
  const button = document.createElement("button");
  button.type = "button";
  button.className = "icon-btn";
  button.title = label;
  button.setAttribute("aria-label", label);
  button.innerHTML = `<svg viewBox="0 0 24 24" aria-hidden="true" class="icon-svg">${svgInner}</svg>`;
  button.onclick = (event) => {
    event.stopPropagation();
    action();
  };
  return button;
}

let toastTimer = null;
function showToast(message, isError = false) {
  const toast = document.getElementById("toast");
  if (!toast) return;
  toast.textContent = message;
  toast.classList.toggle("error", !!isError);
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => {
    toast.textContent = "";
  }, 3500);
}

function confirmAction(title, message, action) {
  document.getElementById("confirmModalTitle").textContent = title;
  document.getElementById("confirmModalMessage").textContent = message;
  const modal = document.getElementById("confirmModal");
  document.getElementById("confirmModalConfirmBtn").onclick = async () => {
    modal.close();
    await action();
  };
  modal.showModal();
}
document.getElementById("confirmModalCancelBtn").onclick = () =>
  document.getElementById("confirmModal").close();

// Plain "+"/"-" read as barely-visible specks at badge size - wider, bolder
// Unicode variants keep the same at-a-glance meaning but are legible.
function transitionSymbol(type) {
  return (
    {
      peer_made_changes: "＋", // fullwidth plus sign
      local_missing_node: "＋",
      local_made_changes: "≈", // almost-equal sign
      peer_missing_node: "−", // minus sign
      divergence: "!",
      in_transition: ".",
    }[type] || "*"
  );
}

function dedupe(items) {
  return [...new Set(items)];
}

function transitionActorLabel(info) {
  const sourceType = info.original_type || info.type;
  const originDescribesIncomingRevision = [
    "peer_made_changes",
    "local_missing_node",
    "divergence",
  ].includes(sourceType);
  if (
    originDescribesIncomingRevision &&
    info.origin_identity &&
    typeof userForParticipant === "function"
  ) {
    const user = userForParticipant(info.origin_identity);
    if (user && user.name && user.name !== "?") return user.name;
  }
  return peerLabel(info.peer_addr);
}

function eventChangeSummary(event, limit = 3) {
  const summaries = (event?.changes || [])
    .map((change) => change.summary)
    .filter(Boolean);
  if (summaries.length <= limit) return summaries.join("; ");
  return `${summaries.slice(0, limit).join("; ")}; +${summaries.length - limit} more`;
}

function localChangeSummary(info, limit = 3) {
  const summaries = dedupe(
    (info?.events || [info])
      .flatMap((event) => event?.changes || [])
      .map((change) => change.local_summary)
      .filter(Boolean),
  );
  if (summaries.length <= limit) return summaries.join("; ");
  return `${summaries.slice(0, limit).join("; ")}; +${summaries.length - limit} more`;
}

function transitionChangeDetails(info) {
  return (info.events || [info])
    .filter((event) => event && event.type !== "in_agreement")
    .map((event) => {
      const summary = eventChangeSummary(event, 6);
      if (!summary) return "";
      return `${transitionActorLabel(event)}: ${summary}`;
    })
    .filter(Boolean)
    .join("\n");
}

function groupedTransitionLabel(events) {
  const order = [
    "divergence",
    "peer_made_changes",
    "local_missing_node",
    "local_made_changes",
    "peer_missing_node",
    "in_transition",
  ];
  const labels = {
    divergence: "Diverged from",
    peer_made_changes: "Changes from",
    local_missing_node: "Only in",
    local_made_changes: "My changes not in",
    peer_missing_node: "Missing in",
    in_transition: "Waiting for",
  };
  return order
    .filter((type) => events.some((event) => event.type === type))
    .map((type) => {
      const peers = events
        .filter((event) => event.type === type)
        .map((event) => transitionActorLabel(event));
      return `${labels[type]} ${dedupe(peers).join(", ")}`;
    })
    .join("; ");
}

function transitionLabel(info) {
  const events = (info.events || [info]).filter((event) => event.type !== "in_agreement");
  const details = transitionChangeDetails(info);
  if (events.length > 1) {
    const grouped = groupedTransitionLabel(events);
    return details ? `${grouped}\n${details}` : grouped;
  }
  const peer = transitionActorLabel(info);
  const labels = {
    in_agreement: "In agreement",
    peer_made_changes: `Changes from ${peer}`,
    local_made_changes: `My changes not in ${peer}`,
    local_missing_node: `Only in ${peer}`,
    peer_missing_node: `Missing in ${peer}`,
    divergence: `Diverged from ${peer}`,
    in_transition: `Waiting for ${peer} to process this change`,
  };
  const label = labels[info.type] || "Difference";
  return details ? `${label}\n${details}` : label;
}

/*
  Sovereign host shell - the collaboration surface every application shares.

  Core owns this because everything in it is a Core concept: identities,
  peers, channels, connect tokens, and mailbox targets. An application that
  built its own would be reimplementing Core's vocabulary, and the copies
  would drift - which is exactly how a renamed token field broke pasting in
  two applications at once.

  Navigation is host-driven. The shell asks GET /api/core/applications, so
  no application ever names another application's identifier or URL, and
  deactivating one removes a link instead of breaking it.

  Mount with SovereignShell.mount({...}); the shell injects its own dialogs,
  so a page needs no markup contract beyond the container it passes in.
*/
const SovereignShell = {
  _applications: null,
  _dialogs: false,
  _options: {},

  async applications() {
    if (this._applications) return this._applications;
    try {
      const response = await fetch("/api/core/applications");
      const payload = await response.json();
      this._applications = payload.applications || [];
    } catch (error) {
      this._applications = [];
    }
    return this._applications;
  },

  async _post(path, body) {
    const response = await fetch(path, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body || {}),
    });
    const value = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(value.reason || "Request failed");
    return value;
  },

  // options: { container, applicationId, topicUuid(), state(), onChanged() }
  // state() returns { network, channel_targets, channel_target_id } so the
  // shell never reaches into an application's own payload shape.
  async mount(options) {
    this._options = options;
    const container = options.container;
    container.classList.add("shell-bar");
    container.replaceChildren();

    const nav = document.createElement("nav");
    nav.className = "shell-nav";
    for (const app of await this.applications()) {
      const link = document.createElement("a");
      link.className = "shell-nav-link";
      link.href = app.asset_prefix;
      link.textContent = app.display_name;
      if (app.application_id === options.applicationId) {
        link.classList.add("current");
        link.setAttribute("aria-current", "page");
      }
      nav.append(link);
    }
    container.append(nav);

    const actions = document.createElement("div");
    actions.className = "shell-actions";
    const relay = document.createElement("button");
    relay.type = "button";
    relay.className = "shell-relay-btn";
    relay.textContent = "Relay targets";
    relay.onclick = () => this.openRelayTargets();
    actions.append(relay);

    const connection = document.createElement("button");
    connection.type = "button";
    connection.id = "shellConnectionBtn";
    connection.className = "peer-cluster local";
    connection.textContent = "local";
    connection.onclick = () => this.openConnectionPanel();
    actions.append(connection);
    container.append(actions);

    this._ensureDialogs();
    this.refresh();
  },

  refresh() {
    const button = document.getElementById("shellConnectionBtn");
    const state = this._options.state ? this._options.state() : {};
    if (!button || !state) return;
    const peers = Object.entries((state.network && state.network.peers) || {});
    button.replaceChildren();
    button.className = peers.length ? "peer-cluster" : "peer-cluster local";
    if (!peers.length) {
      button.textContent = "local";
      button.title = "No peers on this topic";
      return;
    }
    for (const entry of peers.slice(0, 4)) {
      const addr = entry[0];
      const info = entry[1] || {};
      const avatar = document.createElement("span");
      const online = !info.status || info.status.state !== "offline";
      avatar.className = "header-avatar " + (online ? "status-online" : "status-offline");
      avatar.textContent = (addr.replace(/^relay:/, "").slice(0, 2) || "?").toUpperCase();
      avatar.title = info.channel ? addr + " (" + info.channel + ")" : addr;
      button.append(avatar);
    }
    if (peers.length > 4) {
      const more = document.createElement("span");
      more.className = "header-avatar more";
      more.textContent = "+" + (peers.length - 4);
      button.append(more);
    }
    button.title = peers
      .map((entry) => entry[0] + (entry[1] && entry[1].channel ? " [" + entry[1].channel + "]" : ""))
      .join("\n");
  },

  _ensureDialogs() {
    if (this._dialogs) return;
    const host = document.createElement("div");
    host.innerHTML = [
      '<dialog id="shellConnectionModal" class="shell-dialog">',
      '<form method="dialog" class="shell-panel">',
      "<h2>Connection</h2>",
      '<label for="shellTargetSelect">Relay target for this topic</label>',
      '<select id="shellTargetSelect"></select>',
      '<div class="shell-row"><button type="button" id="shellCopyTokenBtn">Copy share token</button></div>',
      '<label for="shellTokenInput">Paste a share token</label>',
      '<div class="shell-row"><input id="shellTokenInput" placeholder="Paste a share token"><button type="button" id="shellConnectBtn">Connect</button></div>',
      '<p id="shellConnectionNote" class="shell-note"></p>',
      '<menu><button type="button" id="shellConnectionCloseBtn">Close</button></menu>',
      "</form></dialog>",
      '<dialog id="shellTargetsModal" class="shell-dialog">',
      '<form method="dialog" class="shell-panel">',
      "<h2>Relay targets</h2>",
      '<div id="shellTargetList" class="shell-target-list"></div>',
      '<fieldset class="shell-target-form"><legend>Add an SFTP target</legend>',
      '<input id="shellTargetName" placeholder="Name">',
      '<input id="shellTargetHost" placeholder="Host">',
      '<input id="shellTargetPort" type="number" value="22" min="1" max="65535">',
      '<input id="shellTargetUser" placeholder="Username">',
      '<input id="shellTargetRoot" placeholder="Remote path" value="/">',
      '<button type="button" id="shellAddTargetBtn">Add target</button></fieldset>',
      '<p id="shellTargetsNote" class="shell-note"></p>',
      '<menu><button type="button" id="shellTargetsCloseBtn">Close</button></menu>',
      "</form></dialog>",
    ].join("");
    document.body.append(...host.children);
    this._dialogs = true;

    document.getElementById("shellConnectionCloseBtn").onclick = () =>
      document.getElementById("shellConnectionModal").close();
    document.getElementById("shellTargetsCloseBtn").onclick = () =>
      document.getElementById("shellTargetsModal").close();
    document.getElementById("shellTargetSelect").onchange = (event) =>
      this._assignTarget(event.target.value);
    document.getElementById("shellCopyTokenBtn").onclick = () => this._copyToken();
    document.getElementById("shellConnectBtn").onclick = () => this._connect();
    document.getElementById("shellAddTargetBtn").onclick = () => this._addTarget();
  },

  _note(id, message) {
    document.getElementById(id).textContent = message;
  },

  _topic() {
    return this._options.topicUuid ? this._options.topicUuid() : "";
  },

  openConnectionPanel() {
    this._ensureDialogs();
    const state = this._options.state ? this._options.state() : {};
    const select = document.getElementById("shellTargetSelect");
    const wanted = [["", "Not assigned"]];
    for (const item of state.channel_targets || []) wanted.push([item.id, item.name]);
    select.replaceChildren();
    for (const pair of wanted) {
      const option = document.createElement("option");
      option.value = pair[0];
      option.textContent = pair[1];
      select.append(option);
    }
    select.value = state.channel_target_id || "";
    const topic = this._topic();
    select.disabled = !topic;
    document.getElementById("shellCopyTokenBtn").disabled = !topic || !select.value;
    this._note(
      "shellConnectionNote",
      topic ? "" : "Select or create a topic before sharing it.",
    );
    document.getElementById("shellConnectionModal").showModal();
  },

  async _assignTarget(targetId) {
    const topic = this._topic();
    if (!topic) return;
    try {
      await this._post("/api/channels/mailbox/topics/assign", {
        topic_uuid: topic,
        target_id: targetId || null,
      });
      document.getElementById("shellCopyTokenBtn").disabled = !targetId;
      this._note(
        "shellConnectionNote",
        targetId ? "Relay target assigned." : "Relay target removed.",
      );
      await this._changed();
    } catch (error) {
      this._note("shellConnectionNote", error.message);
    }
  },

  async _copyToken() {
    const topic = this._topic();
    const targetId = document.getElementById("shellTargetSelect").value;
    if (!topic) return;
    try {
      const token = await this._post("/api/connect_token", {
        topic_uuids: [topic],
        channel_options: targetId ? { mailbox: { target_id: targetId } } : {},
      });
      await navigator.clipboard.writeText(btoa(JSON.stringify(token)));
      this._note("shellConnectionNote", "Token copied to the clipboard.");
    } catch (error) {
      this._note("shellConnectionNote", error.message);
    }
  },

  async _connect() {
    const field = document.getElementById("shellTokenInput");
    let token;
    try {
      token = JSON.parse(atob(field.value.trim()));
    } catch (error) {
      this._note("shellConnectionNote", "That is not a share token.");
      return;
    }
    // Core serializes token_version; the channel descriptor carries its own
    // descriptor_version. Testing the wrong one rejects every valid token.
    if (
      token.token_version !== 1 ||
      !Array.isArray(token.topic_uuids) ||
      !token.topic_uuids.length
    ) {
      this._note("shellConnectionNote", "Unrecognized token version.");
      return;
    }
    try {
      await this._post("/api/connect", { token });
      field.value = "";
      this._note("shellConnectionNote", "Connected.");
      await this._changed();
    } catch (error) {
      this._note("shellConnectionNote", error.message);
    }
  },

  async openRelayTargets() {
    this._ensureDialogs();
    await this._renderTargets();
    this._note("shellTargetsNote", "");
    document.getElementById("shellTargetsModal").showModal();
  },

  async _renderTargets() {
    const list = document.getElementById("shellTargetList");
    list.replaceChildren();
    let targets = [];
    try {
      const response = await fetch("/api/channels/mailbox/targets");
      targets = (await response.json()).targets || [];
    } catch (error) {
      this._note("shellTargetsNote", "Could not read relay targets.");
      return;
    }
    if (!targets.length) {
      const empty = document.createElement("p");
      empty.className = "shell-note";
      empty.textContent = "No relay targets yet.";
      list.append(empty);
      return;
    }
    for (const target of targets) {
      const row = document.createElement("div");
      row.className = "shell-target-row";
      const name = document.createElement("span");
      name.textContent = target.name;
      const where = document.createElement("span");
      where.className = "shell-note";
      where.textContent = target.host ? target.host + ":" + target.port : target.root;
      const test = document.createElement("button");
      test.type = "button";
      test.textContent = "Test";
      test.onclick = async () => {
        this._note("shellTargetsNote", "Testing " + target.name + "...");
        try {
          await this._post("/api/channels/mailbox/targets/test", { target_id: target.id });
          this._note("shellTargetsNote", target.name + " is reachable.");
        } catch (error) {
          this._note("shellTargetsNote", target.name + ": " + error.message);
        }
      };
      const remove = document.createElement("button");
      remove.type = "button";
      remove.className = "danger";
      remove.textContent = "Delete";
      remove.onclick = async () => {
        try {
          await this._post("/api/channels/mailbox/targets/delete", { target_id: target.id });
          await this._renderTargets();
          this._note("shellTargetsNote", target.name + " deleted.");
          await this._changed();
        } catch (error) {
          this._note("shellTargetsNote", error.message);
        }
      };
      row.append(name, where, test, remove);
      list.append(row);
    }
  },

  async _addTarget() {
    const values = {
      name: document.getElementById("shellTargetName").value.trim(),
      backend: "sftp",
      host: document.getElementById("shellTargetHost").value.trim(),
      port: Number(document.getElementById("shellTargetPort").value) || 22,
      username: document.getElementById("shellTargetUser").value.trim(),
      root: document.getElementById("shellTargetRoot").value.trim() || "/",
    };
    if (!values.name || !values.host || !values.username) {
      this._note("shellTargetsNote", "Name, host, and username are required.");
      return;
    }
    this._note("shellTargetsNote", "Verifying the target...");
    try {
      await this._post("/api/channels/mailbox/targets", values);
      for (const id of ["shellTargetName", "shellTargetHost", "shellTargetUser"]) {
        document.getElementById(id).value = "";
      }
      await this._renderTargets();
      this._note("shellTargetsNote", values.name + " added.");
      await this._changed();
    } catch (error) {
      this._note("shellTargetsNote", error.message);
    }
  },

  async _changed() {
    if (this._options.onChanged) await this._options.onChanged();
  },
};
