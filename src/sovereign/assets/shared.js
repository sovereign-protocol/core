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

// Pages that model people define peerLabel(); ones that do not - a minimal
// application, or any page before its first load - must still be able to
// render a transition rather than throwing a ReferenceError.
function safePeerLabel(addr) {
  if (typeof peerLabel === "function") return peerLabel(addr);
  return String(addr || "a peer");
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
  return safePeerLabel(info.peer_addr);
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
    const nav = this._buildHeader(options.container, options);
    for (const app of await this.applications()) {
      if (app.application_id === options.applicationId) {
        // The application you are in is named, not linked to itself.
        document.getElementById("shellAppName").textContent = app.display_name;
        continue;
      }
      const link = document.createElement("a");
      link.className = "shell-nav-link";
      link.href = app.asset_prefix;
      link.textContent = app.display_name;
      nav.append(link);
    }

    this._ensureDialogs();
    this.refresh();
    this.refreshAvatar();
  },

  refresh() {
    this.refreshDisagreements();
    const button = document.getElementById("shellConnectionBtn");
    const state = this._options.state ? this._options.state() : {};
    if (!button || !state) return;
    // Every session peer by default. An application whose view is scoped to
    // one topic - a board, an agreement - says which addresses belong to it,
    // so the cluster shows who is on *this* thing rather than everyone.
    const all = (state.network && state.network.peers) || {};
    const peers = this._options.peerAddresses
      ? this._options.peerAddresses().map((addr) => [addr, all[addr] || {}])
      : Object.entries(all);
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
      // An application that models people - names, avatars - can describe a
      // peer far better than an address allows. Core knows identities, not
      // faces, so it asks and falls back to initials of the address.
      const described = this._options.describePeer
        ? this._options.describePeer(addr) || {} : {};
      const avatar = document.createElement("span");
      const online = described.online !== undefined
        ? described.online
        : !info.status || info.status.state !== "offline";
      avatar.className = "header-avatar " + (online ? "status-online" : "status-offline");
      if (described.picture) {
        avatar.style.backgroundImage = 'url("' + described.picture + '")';
      } else {
        const source = described.label || addr.replace(/^relay:/, "");
        avatar.textContent = (source.slice(0, 2) || "?").toUpperCase();
      }
      const label = described.label || addr;
      avatar.title = info.channel ? label + " (" + info.channel + ")" : label;
      button.append(avatar);
    }
    if (peers.length > 4) {
      const more = document.createElement("span");
      more.className = "header-avatar more";
      more.textContent = "+" + (peers.length - 4);
      button.append(more);
    }
    button.title = peers
      .map((entry) => {
        const described = this._options.describePeer
          ? this._options.describePeer(entry[0]) || {} : {};
        const channel = entry[1] && entry[1].channel;
        return (described.label || entry[0]) + (channel ? " [" + channel + "]" : "");
      })
      .join("\n");
  },

  _ensureDialogs() {
    if (this._dialogs) return;
    const host = document.createElement("div");
    host.innerHTML = [
      '<dialog id="shellConnectionModal" class="shell-dialog">',
      '<form method="dialog" class="shell-panel">',
      "<h2>Connection</h2>",
      '<div id="shellConnectionContent"></div>',
      '<label for="shellTargetSelect">Relay target for this topic</label>',
      '<select id="shellTargetSelect"></select>',
      '<div class="shell-row"><button type="button" id="shellCopyTokenBtn">Copy share token</button></div>',
      '<label for="shellTokenInput">Paste a share token</label>',
      '<div class="shell-row"><input id="shellTokenInput" placeholder="Paste a share token"><button type="button" id="shellConnectBtn">Connect</button></div>',
      '<div class="shell-row"><button type="button" id="shellManageChannelsBtn">Add or edit channels</button></div>',
      '<div id="shellConnectionExtras" class="shell-row"></div>',
      '<p id="shellConnectionNote" class="shell-note"></p>',
      '<menu><button type="button" id="shellConnectionCloseBtn">Close</button></menu>',
      "</form></dialog>",
      '<dialog id="shellTargetsModal" class="shell-dialog">',
      '<form method="dialog" class="shell-panel">',
      "<h2>Relay targets</h2>",
      '<div id="shellTargetList" class="shell-target-list"></div>',
      '<fieldset class="shell-target-form">',
      '<legend id="shellTargetFormTitle">Add an SFTP target</legend>',
      '<input id="shellTargetName" placeholder="Name">',
      '<input id="shellTargetHost" placeholder="Host">',
      '<input id="shellTargetPort" type="number" value="22" min="1" max="65535">',
      '<input id="shellTargetUser" placeholder="Username">',
      '<input id="shellTargetPassword" type="password" placeholder="Password (optional)">',
      '<input id="shellTargetRoot" placeholder="Remote path" value="/">',
      '<input id="shellTargetPoll" type="number" value="3" min="1" max="300" title="Poll every (seconds)">',
      '<div class="shell-row">',
      '<button type="button" id="shellAddTargetBtn">Test &amp; save</button>',
      '<button type="button" id="shellCancelTargetBtn" hidden>Cancel edit</button>',
      "</div>",
      '<p class="shell-note">Leave the password blank to use key authentication or your SSH agent. A password entered here is stored in this client&#39;s local session data.</p>',
      "</fieldset>",
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
    // Channel management lives inside the connection pane, not the header:
    // adding a relay target is something you do while connecting, not a
    // standing top-level action.
    document.getElementById("shellManageChannelsBtn").onclick = () => {
      document.getElementById("shellConnectionModal").close();
      this.openRelayTargets();
    };
    document.getElementById("shellCopyTokenBtn").onclick = () => this._copyToken();
    document.getElementById("shellConnectBtn").onclick = () => this._connect();
    document.getElementById("shellAddTargetBtn").onclick = () => this._saveTarget();
    document.getElementById("shellCancelTargetBtn").onclick = () => this._resetTargetForm();
  },

  _editingTargetId: "",

  _resetTargetForm() {
    this._editingTargetId = "";
    document.getElementById("shellTargetFormTitle").textContent = "Add an SFTP target";
    document.getElementById("shellCancelTargetBtn").hidden = true;
    const values = {
      shellTargetName: "", shellTargetHost: "", shellTargetPort: "22",
      shellTargetUser: "", shellTargetPassword: "", shellTargetRoot: "/",
      shellTargetPoll: "3",
    };
    for (const id of Object.keys(values)) {
      document.getElementById(id).value = values[id];
    }
  },

  _loadTargetIntoForm(target) {
    this._editingTargetId = target.id;
    document.getElementById("shellTargetFormTitle").textContent =
      "Edit " + target.name;
    document.getElementById("shellCancelTargetBtn").hidden = false;
    document.getElementById("shellTargetName").value = target.name || "";
    document.getElementById("shellTargetHost").value = target.host || "";
    document.getElementById("shellTargetPort").value = target.port || 22;
    document.getElementById("shellTargetUser").value = target.username || "";
    // The stored password is never sent back to the browser. Leaving this
    // blank keeps whatever is already saved; typing replaces it.
    document.getElementById("shellTargetPassword").value = "";
    document.getElementById("shellTargetRoot").value = target.root || "/";
    document.getElementById("shellTargetPoll").value =
      target.poll_interval_seconds || 3;
  },

  _note(id, message) {
    document.getElementById(id).textContent = message;
  },

  _panelTopic: "",

  _topic() {
    if (this._panelTopic) return this._panelTopic;
    return this._options.topicUuid ? this._options.topicUuid() : "";
  },

  // topicUuid overrides the mounted default, so a multi-topic view such as a
  // board overview can share one specific topic. extras carries actions the
  // shell has no business knowing about - "stop discussing" belongs to the
  // application that owns the topic type, not to Core.
  openConnectionPanel(panel) {
    const request = panel || {};
    this._ensureDialogs();
    this._panelTopic = request.topicUuid || "";
    const state = this._options.state ? this._options.state() : {};
    // An application can put its own view of the topic at the top of the
    // panel - Kanban shows who is on the board, with faces Core cannot know.
    const content = document.getElementById("shellConnectionContent");
    content.replaceChildren();
    const supplied = request.content
      || (this._options.panelContent && this._options.panelContent(this._panelTopic));
    if (supplied) content.append(supplied);

    const extraRow = document.getElementById("shellConnectionExtras");
    extraRow.replaceChildren();
    // Opening the panel from the shell's own button carries no request, so
    // fall back to the actions the application registered at mount.
    const extras = request.extras
      || (this._options.extras ? this._options.extras() : []);
    for (const extra of extras || []) {
      const button = document.createElement("button");
      button.type = "button";
      if (extra.className) button.className = extra.className;
      button.textContent = extra.label;
      button.onclick = () => {
        document.getElementById("shellConnectionModal").close();
        extra.onClick();
      };
      extraRow.append(button);
    }
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
    select.value = (request.topicUuid
      ? request.targetId : state.channel_target_id) || "";
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
      const location = target.host
        ? target.username + "@" + target.host + ":" + target.port + target.root
        : target.root;
      const timing = target.timing || {};
      const timingText = timing.roundtrip_ms == null
        ? "timing pending"
        : "RTT " + Math.round(timing.roundtrip_ms) + " ms, relay "
          + Math.round(timing.relay_cycle_ms || 0) + " ms";
      where.textContent = location + " - " + timingText;
      if (timing.server_clock_offset_ms != null) {
        where.title = "Relay clock offset "
          + Math.round(timing.server_clock_offset_ms) + " ms (+/- "
          + Math.round(timing.clock_uncertainty_ms || 0) + " ms)";
      }
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
      const edit = document.createElement("button");
      edit.type = "button";
      edit.textContent = "Edit";
      // The form describes an SFTP target. A local-folder target has no
      // host, so offering Edit would round-trip it through an SFTP payload
      // and reject it for a missing host.
      edit.disabled = !target.host;
      edit.title = target.host
        ? "Edit this target" : "Only SFTP targets are editable here";
      edit.onclick = () => {
        this._loadTargetIntoForm(target);
        this._note("shellTargetsNote", "Editing " + target.name + ".");
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
      row.append(name, where, test, edit, remove);
      list.append(row);
    }
  },

  async _saveTarget() {
    const password = document.getElementById("shellTargetPassword").value;
    const values = {
      name: document.getElementById("shellTargetName").value.trim(),
      backend: "sftp",
      host: document.getElementById("shellTargetHost").value.trim(),
      port: Number(document.getElementById("shellTargetPort").value) || 22,
      username: document.getElementById("shellTargetUser").value.trim(),
      root: document.getElementById("shellTargetRoot").value.trim() || "/",
      poll_interval_seconds:
        Number(document.getElementById("shellTargetPoll").value) || 3,
    };
    // Only send a password when one was typed, so saving an edit without
    // retyping it keeps the stored secret instead of blanking it.
    if (password) values.password = password;
    if (this._editingTargetId) values.target_id = this._editingTargetId;
    if (!values.name || !values.host || !values.username) {
      this._note("shellTargetsNote", "Name, host, and username are required.");
      return;
    }
    const editing = Boolean(this._editingTargetId);
    this._note("shellTargetsNote", "Verifying the target...");
    try {
      await this._post("/api/channels/mailbox/targets", values);
      this._resetTargetForm();
      await this._renderTargets();
      this._note(
        "shellTargetsNote", values.name + (editing ? " saved." : " added."),
      );
      await this._changed();
    } catch (error) {
      this._note("shellTargetsNote", error.message);
    }
  },

  async _changed() {
    if (this._options.onChanged) await this._options.onChanged();
  },
};

/*
  Shell additions: a fixed header layout, the Core profile editor, and the
  disagreement pane.

  The header has stable regions so every application looks the same and
  nothing jumps as state changes:

    LEFT    collaboration, topic name, topic switcher, topic status
    MIDDLE  application icon and name, navigation, [+] new topic
    RIGHT   connect area, own avatar

  The topic comes first because the topic is what you are working on; the
  application is only where you are. Topic status sits beside the topic name
  rather than off in the actions, so agreement state is read where the topic
  is read.

  The topic region is filled by the application until Core owns topic
  selection. When Core owns it, the shell fills the same region and nothing
  else moves.
*/
Object.assign(SovereignShell, {
  _profileReady: false,

  _buildHeader(container, options) {
    container.classList.add("shell-bar");
    container.replaceChildren();

    // ---- left: this topic --------------------------------------------
    const left = document.createElement("div");
    left.className = "shell-left";

    const collaboration = document.createElement("button");
    collaboration.type = "button";
    collaboration.id = "shellDisagreementBtn";
    collaboration.className = "shell-collab-btn";
    collaboration.textContent = "Collaboration";
    collaboration.onclick = () => this.openDisagreements();

    const topic = document.createElement("div");
    topic.className = "shell-topic";
    topic.id = "shellTopicRegion";

    const status = document.createElement("span");
    status.id = "shellTopicStatus";
    status.className = "shell-topic-status";

    left.append(collaboration, topic, status);

    // ---- middle: which application -----------------------------------
    const middle = document.createElement("div");
    middle.className = "shell-middle";

    const brand = document.createElement("span");
    brand.className = "shell-brand";
    const mark = document.createElement("span");
    mark.className = "shell-app-mark";
    mark.textContent = (options.applicationId || "?").slice(0, 1).toUpperCase();
    const name = document.createElement("span");
    name.className = "shell-app-name";
    name.id = "shellAppName";
    brand.append(mark, name);

    const nav = document.createElement("nav");
    nav.className = "shell-nav";

    const create = document.createElement("button");
    create.type = "button";
    create.id = "shellCreateTopicBtn";
    create.className = "shell-create-btn";
    create.textContent = "+";
    create.title = "New topic";
    create.setAttribute("aria-label", "New topic");
    // Creating a topic is application-specific - a board needs default
    // columns, an agreement a title - so the shell only offers the button.
    create.hidden = !options.onCreateTopic;
    create.onclick = () => options.onCreateTopic && options.onCreateTopic();

    middle.append(brand, nav, create);

    // ---- right: who is here ------------------------------------------
    const actions = document.createElement("div");
    actions.className = "shell-actions";

    const connection = document.createElement("button");
    connection.type = "button";
    connection.id = "shellConnectionBtn";
    connection.className = "peer-cluster local";
    connection.textContent = "local";
    connection.onclick = () => this.openConnectionPanel();

    const avatar = document.createElement("button");
    avatar.type = "button";
    avatar.id = "shellAvatarBtn";
    avatar.className = "header-avatar-btn";
    avatar.title = "Edit your profile";
    avatar.onclick = () => this.openProfile();

    actions.append(connection, avatar);
    container.append(left, middle, actions);
    return nav;
  },

  // The application slot is gone from the header; an application that needs
  // its own control puts it in the topic region beside the switcher.
  slot() {
    return document.getElementById("shellTopicRegion");
  },

  // The topic region belongs to the application until Core owns selection.
  setTopicRegion(node) {
    const region = document.getElementById("shellTopicRegion");
    if (!region) return;
    region.replaceChildren();
    if (node) region.append(node);
  },

  // ---- profile -----------------------------------------------------------

  _ensureProfileDialog() {
    if (this._profileReady) return;
    const host = document.createElement("div");
    host.innerHTML = [
      '<dialog id="shellProfileModal" class="shell-dialog">',
      '<form method="dialog" class="shell-panel" id="shellProfileForm">',
      "<h2>Profile</h2>",
      '<label for="shellProfileName">Name</label>',
      '<input id="shellProfileName">',
      '<label for="shellProfilePicture">Profile picture</label>',
      '<input id="shellProfilePicture" type="file" accept="image/png,image/jpeg,image/gif,image/webp">',
      '<img id="shellProfilePreview" class="shell-avatar-preview" alt="">',
      '<p class="shell-note">PNG, JPEG, GIF or WebP. The picture is shared with everyone you collaborate with.</p>',
      '<p id="shellProfileNote" class="shell-note"></p>',
      "<menu>",
      '<button type="button" id="shellRemoveAvatarBtn" class="danger">Remove picture</button>',
      '<button type="button" id="shellProfileCancelBtn">Cancel</button>',
      '<button type="submit" class="primary">Save</button>',
      "</menu></form></dialog>",
    ].join("");
    document.body.append(...host.children);
    this._profileReady = true;

    document.getElementById("shellProfileCancelBtn").onclick = () =>
      document.getElementById("shellProfileModal").close();
    document.getElementById("shellProfilePicture").onchange = () => {
      const file = document.getElementById("shellProfilePicture").files[0];
      if (!file) return;
      const preview = document.getElementById("shellProfilePreview");
      preview.src = URL.createObjectURL(file);
      preview.style.display = "block";
    };
    document.getElementById("shellProfileForm").onsubmit = (event) => {
      event.preventDefault();
      this._saveProfile();
    };
    document.getElementById("shellRemoveAvatarBtn").onclick = () =>
      this._saveProfile({ removePicture: true });
  },

  async _profileView() {
    const response = await fetch("/api/core/profile");
    return response.json();
  },

  async openProfile() {
    this._ensureProfileDialog();
    this._note("shellProfileNote", "");
    let view = {};
    try {
      view = await this._profileView();
    } catch (error) {
      this._note("shellProfileNote", "Could not read your profile.");
    }
    document.getElementById("shellProfileName").value = view.display_name || "";
    document.getElementById("shellProfilePicture").value = "";
    const preview = document.getElementById("shellProfilePreview");
    preview.src = view.picture || "";
    preview.style.display = view.picture ? "block" : "none";
    document.getElementById("shellRemoveAvatarBtn").hidden = !view.picture;
    document.getElementById("shellProfileModal").showModal();
  },

  async _uploadBlob(file) {
    const response = await fetch("/api/blob", {
      method: "POST",
      headers: {
        "Content-Type": file.type || "application/octet-stream",
        "X-Filename": file.name,
      },
      body: file,
    });
    const payload = await response.json();
    if (!response.ok || payload.status === "error") {
      throw new Error(payload.reason || "Upload failed");
    }
    return payload;
  },

  async _saveProfile(options) {
    const request = options || {};
    const name = document.getElementById("shellProfileName").value;
    const file = document.getElementById("shellProfilePicture").files[0];
    try {
      await this._post("/api/core/profile", { name });
      if (request.removePicture) {
        await this._post("/api/core/profile/avatar", { remove: true });
      } else if (file) {
        const uploaded = await this._uploadBlob(file);
        await this._post("/api/core/profile/avatar", {
          attachment: {
            id: (globalThis.crypto && globalThis.crypto.randomUUID)
              ? globalThis.crypto.randomUUID()
              : String(Date.now()) + "-" + file.name,
            role: "avatar",
            blob_id: uploaded.blob_id,
            name: file.name,
            size: uploaded.size,
            mime: uploaded.mime,
          },
        });
      }
      document.getElementById("shellProfileModal").close();
      await this._changed();
      this.refreshAvatar();
    } catch (error) {
      this._note("shellProfileNote", error.message);
    }
  },

  async refreshAvatar() {
    const button = document.getElementById("shellAvatarBtn");
    if (!button) return;
    let view = {};
    try {
      view = await this._profileView();
    } catch (error) {
      return;
    }
    button.replaceChildren();
    const avatar = document.createElement("span");
    avatar.className = "header-avatar status-online";
    if (view.picture) {
      avatar.style.backgroundImage = 'url("' + view.picture + '")';
    } else {
      const source = view.display_name || "?";
      avatar.textContent = source.slice(0, 2).toUpperCase();
    }
    button.title = (view.display_name || "You") + " - edit your profile";
    button.append(avatar);
  },

  // ---- disagreements -----------------------------------------------------

  // Both applications already publish transition_events and
  // transition_by_node in the same shape, because Session decides what a
  // transition is. Reading them here means neither has to render agreement
  // state itself.
  _disagreements() {
    const state = this._options.state ? this._options.state() : {};
    const grouped = state.transition_by_node || {};
    return Object.keys(grouped)
      .map((uuid) => Object.assign({ node_uuid: uuid }, grouped[uuid]))
      .filter((item) => item.type && item.type !== "in_agreement");
  },

  refreshDisagreements() {
    const button = document.getElementById("shellDisagreementBtn");
    const status = document.getElementById("shellTopicStatus");
    if (!button) return;
    // Agreement state is a property of one topic. An application that shows
    // many at once - an overview - has no single answer, so the whole left
    // region collapses rather than claiming one.
    if (!this._options.topicUuid) {
      button.hidden = true;
      if (status) status.hidden = true;
      return;
    }
    button.hidden = false;
    const items = this._disagreements();
    const diverged = items.filter((item) => item.type === "divergence").length;
    button.classList.toggle("has-divergence", diverged > 0);
    button.classList.toggle("has-items", items.length > 0);
    button.title = items.length
      ? "Open what is not yet in agreement"
      : "Everything on this topic is in agreement";
    if (!status) return;
    status.hidden = false;
    status.textContent = items.length
      ? items.length + (diverged ? " to resolve" : " in transition")
      : "In agreement";
    status.dataset.state = diverged
      ? "divergence" : (items.length ? "in_transition" : "in_agreement");
    status.title = button.title;
  },

  // Rendering the unsettled list is separate from where it is shown, so the
  // collaboration pane and the standalone dialog draw the same thing.
  _renderDisagreementList(list) {
    if (!list) return;
    list.replaceChildren();
    const items = this._disagreements();
    if (!items.length) {
      const empty = document.createElement("p");
      empty.className = "shell-note";
      empty.textContent = "Everything on this topic is in agreement.";
      list.append(empty);
      return;
    }
    for (const item of items) {
      const row = document.createElement("div");
      row.className = "shell-disagreement-row";
      row.dataset.status = item.type;
      const label = document.createElement("span");
      label.className = "shell-disagreement-label";
      label.textContent = transitionLabel(item);
      const where = document.createElement("span");
      where.className = "shell-note";
      const describe = this._options.describeNode;
      where.textContent = describe ? describe(item.node_uuid) || "" : "";
      row.append(label, where);
      if (this._options.revealNode) {
        const reveal = document.createElement("button");
        reveal.type = "button";
        reveal.textContent = "Show";
        reveal.onclick = () => {
          this.closeCollab();
          this._options.revealNode(item.node_uuid);
        };
        row.append(reveal);
      }
      list.append(row);
    }
  },

  openDisagreements() {
    // The header button opens the whole collaboration pane; what is not in
    // agreement is one section of it, beside the agenda it belongs with.
    this.openCollab();
  },
});

/*
  The collaboration pane - the same surface in every application.

  Sections, in the order S-Kanban established: what I want to discuss, what
  everyone wants to discuss, what is not yet in agreement, and the settings
  that govern adoption. All four are Core concepts, so all four live here and
  no application renders them itself.

  Agendas are Session's: an agenda item is a child of the topic root, and
  every application's topic is a root. An application supplies only the API
  paths it exposes them on, because route namespacing is per application.
*/
Object.assign(SovereignShell, {
  _collabReady: false,

  _ensureCollabPane() {
    if (this._collabReady) return;
    const host = document.createElement("div");
    host.innerHTML = [
      '<div id="shellCollabOverlay" class="shell-pane-overlay" hidden></div>',
      '<aside id="shellCollabPane" class="shell-pane" hidden>',
      '<div class="shell-pane-header">',
      "<strong>Collaboration</strong>",
      '<button type="button" id="shellCollabCloseBtn" class="shell-pane-close" aria-label="Close">&times;</button>',
      "</div>",
      '<div class="shell-pane-section">',
      "<h3>My agenda</h3>",
      '<div id="shellMyAgenda" class="shell-agenda-list"></div>',
      '<form id="shellAgendaForm" class="shell-row">',
      '<input id="shellAgendaText" placeholder="Add a discussion topic">',
      '<button type="submit">Add</button>',
      "</form>",
      "</div>",
      '<div class="shell-pane-section">',
      "<h3>Integrated agenda</h3>",
      '<div id="shellMergedAgenda" class="shell-agenda-list"></div>',
      "</div>",
      '<div class="shell-pane-section">',
      '<h3 id="shellNotAlignedTitle">Not yet in agreement</h3>',
      '<div id="shellDisagreementList" class="shell-disagreement-list"></div>',
      "</div>",
      '<div class="shell-pane-section" id="shellCollabSettings" hidden>',
      "<h3>Settings</h3>",
      '<div id="shellCollabSettingsBody"></div>',
      "</div>",
      "</aside>",
    ].join("");
    document.body.append(...host.children);
    this._collabReady = true;

    document.getElementById("shellCollabCloseBtn").onclick = () => this.closeCollab();
    document.getElementById("shellCollabOverlay").onclick = () => this.closeCollab();
    document.getElementById("shellAgendaForm").onsubmit = (event) => {
      event.preventDefault();
      this._addAgendaItem();
    };
  },

  closeCollab() {
    const pane = document.getElementById("shellCollabPane");
    const overlay = document.getElementById("shellCollabOverlay");
    if (pane) pane.hidden = true;
    if (overlay) overlay.hidden = true;
  },

  _agendaRoutes() {
    return this._options.agendaRoutes || null;
  },

  async _addAgendaItem() {
    const routes = this._agendaRoutes();
    const field = document.getElementById("shellAgendaText");
    const text = field.value.trim();
    const topic = this._topic();
    if (!routes || !text || !topic) return;
    try {
      await this._post(routes.create, { [routes.topicKey]: topic, text });
      field.value = "";
      await this._changed();
      this.openCollab();
    } catch (error) {
      showToast(error.message, true);
    }
  },

  _agendaRow(item, mine) {
    const routes = this._agendaRoutes();
    const row = document.createElement("div");
    row.className = "shell-agenda-item";
    row.dataset.priority = item.data.priority || "";

    const text = document.createElement("span");
    text.className = "shell-agenda-text";
    text.textContent = item.data.text || "";
    row.append(text);

    // Only the originator may steer their own item; everyone else reads it.
    // That rule is Session's, and the view simply reflects it.
    if (mine && routes) {
      const priority = document.createElement("select");
      for (const [value, label] of [["", "No priority"], ["high", "High"],
        ["medium", "Medium"], ["low", "Low"]]) {
        const option = document.createElement("option");
        option.value = value;
        option.textContent = label;
        priority.append(option);
      }
      priority.value = item.data.priority || "";
      priority.onchange = async () => {
        try {
          await this._post(routes.setPriority, {
            item_uuid: item.uuid, priority: priority.value || null,
          });
          await this._changed();
        } catch (error) { showToast(error.message, true); }
      };
      const remove = document.createElement("button");
      remove.type = "button";
      remove.className = "shell-agenda-delete";
      remove.textContent = "Delete";
      remove.onclick = async () => {
        try {
          await this._post(routes.delete, { item_uuid: item.uuid });
          await this._changed();
          this.openCollab();
        } catch (error) { showToast(error.message, true); }
      };
      row.append(priority, remove);
    } else {
      const author = document.createElement("span");
      author.className = "shell-note";
      const described = this._options.describeAuthor
        ? this._options.describeAuthor(item.data.author) : "";
      author.textContent = described || "";
      row.append(author);
    }
    return row;
  },

  _renderAgenda() {
    const state = this._options.state ? this._options.state() : {};
    const items = state.agenda_items || [];
    const me = state.identity_uuid
      || (state.user_profile && state.user_profile.uuid) || "";
    const mine = document.getElementById("shellMyAgenda");
    const merged = document.getElementById("shellMergedAgenda");
    mine.replaceChildren();
    merged.replaceChildren();

    const own = items.filter((item) => item.data.author === me);
    if (!own.length) {
      const empty = document.createElement("p");
      empty.className = "shell-note";
      empty.textContent = "Nothing from you yet.";
      mine.append(empty);
    }
    for (const item of own) mine.append(this._agendaRow(item, true));

    if (!items.length) {
      const empty = document.createElement("p");
      empty.className = "shell-note";
      empty.textContent = "No discussion topics on this topic yet.";
      merged.append(empty);
    }
    for (const item of items) {
      merged.append(this._agendaRow(item, false));
    }

    document.getElementById("shellAgendaForm").hidden = !this._agendaRoutes();
  },

  openCollab() {
    this._ensureCollabPane();
    this._renderAgenda();
    this._renderDisagreementList(
      document.getElementById("shellDisagreementList"),
    );

    // An application may hang its own topic settings here - auto-adopt is
    // Kanban's today - without the shell knowing what they are.
    const settings = document.getElementById("shellCollabSettings");
    const body = document.getElementById("shellCollabSettingsBody");
    body.replaceChildren();
    const supplied = this._options.collabSettings
      && this._options.collabSettings();
    settings.hidden = !supplied;
    if (supplied) body.append(supplied);

    document.getElementById("shellCollabOverlay").hidden = false;
    document.getElementById("shellCollabPane").hidden = false;
  },
});
