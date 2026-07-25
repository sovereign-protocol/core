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

// Two people in conversation - the collaboration pane's mark, kept as an icon
// rather than a word so the header reads at a glance in every application.
const ICON_COLLABORATION =
  '<path d="M8 11a3 3 0 1 0 0-6 3 3 0 0 0 0 6Z"></path>' +
  '<path d="M16 10a2.5 2.5 0 1 0 0-5 2.5 2.5 0 0 0 0 5Z"></path>' +
  '<path d="M3.5 19c.6-3 2.3-5 4.5-5s3.9 2 4.5 5"></path>' +
  '<path d="M12.5 18c.5-2.3 1.8-3.8 3.5-3.8s3 1.5 3.5 3.8"></path>';

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
    const applications = await this.applications();
    const current = applications.find(
      (app) => app.application_id === options.applicationId,
    );
    if (current) {
      // The application you are in is named, not linked to itself.
      document.getElementById("shellAppName").textContent = current.display_name;
      if (current.icon) {
        const mark = document.getElementById("shellAppMark");
        mark.textContent = "";
        mark.innerHTML =
          '<svg viewBox="0 0 24 24" aria-hidden="true" class="icon-svg">'
          + current.icon + "</svg>";
      }
    }
    // An aggregator reaches other applications through its own content - its
    // tiles are the navigation - so it is given none, and nothing links to a
    // second way of doing the same thing.
    if (!current || current.role !== "aggregator") {
      for (const app of applications) {
        if (app.application_id === options.applicationId) continue;
        const link = document.createElement("a");
        link.className = "shell-nav-link icon-btn";
        link.href = app.asset_prefix;
        link.title = app.display_name;
        link.setAttribute("aria-label", app.display_name);
        if (app.icon) {
          link.innerHTML =
            '<svg viewBox="0 0 24 24" aria-hidden="true" class="icon-svg">'
            + app.icon + "</svg>";
        } else {
          link.textContent = app.display_name.slice(0, 1).toUpperCase();
        }
        nav.append(link);
      }
    }

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

    const collaboration = iconButton(
      ICON_COLLABORATION, "Collaboration", () => this.openCollab(),
    );
    collaboration.id = "shellDisagreementBtn";
    collaboration.classList.add("shell-collab-btn");

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
    mark.id = "shellAppMark";
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
    create.hidden = !options.onCreateTopic;
    create.onclick = () => options.onCreateTopic && options.onCreateTopic();

    middle.append(brand, nav, create);

    // ---- right: who is here ------------------------------------------
    const actions = document.createElement("div");
    actions.className = "shell-actions";

    // An application's own header controls go here rather than beside the
    // shell, so the bar has one owner and one order everywhere.
    const appActions = document.createElement("div");
    appActions.className = "shell-app-actions";
    appActions.id = "shellAppActions";

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

    actions.append(appActions, connection, avatar);
    container.append(left, middle, actions);
    return nav;
  },

  // The topic region and the application-actions slot are the two places an
  // application puts its own controls. Everything else in the bar is Core's.
  setTopicRegion(node) {
    const region = document.getElementById("shellTopicRegion");
    if (!region) return;
    region.replaceChildren();
    if (node) region.append(node);
  },

  setAppActions(...nodes) {
    const region = document.getElementById("shellAppActions");
    if (!region) return;
    region.replaceChildren();
    for (const node of nodes) if (node) region.append(node);
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
      ? "Open current divergences"
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
      empty.textContent = "No current divergences.";
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

  // One merged agenda, not two. The list is a topic's whole agenda; splitting
  // "mine" from "everyone's" duplicated every row you authored and made the
  // pane twice as tall for no information gain. The add-topic form moves to
  // the bottom, after what already exists, not before it.
  _ensureCollabPane() {
    if (this._collabReady) return;
    const host = document.createElement("div");
    host.innerHTML = [
      '<div id="shellCollabOverlay" class="shell-pane-overlay" hidden></div>',
      '<aside id="shellCollabPane" class="shell-pane shell-pane-left" hidden>',
      '<div class="shell-pane-header">',
      "<strong>Collaboration</strong>",
      '<button type="button" id="shellCollabCloseBtn" class="shell-pane-close" aria-label="Close">&times;</button>',
      "</div>",
      '<div class="shell-pane-section">',
      "<h3>Agenda</h3>",
      '<div id="shellAgendaList" class="shell-agenda-list"></div>',
      '<form id="shellAgendaForm" class="shell-row">',
      '<input id="shellAgendaText" placeholder="Add a discussion topic">',
      '<button type="submit">Add</button>',
      "</form>",
      "</div>",
      '<div class="shell-pane-section">',
      '<h3 id="shellNotAlignedTitle">Current divergences</h3>',
      '<div id="shellDisagreementList" class="shell-disagreement-list"></div>',
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
    document.body.classList.remove("shell-pane-open-left");
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

  // Identity is Core's. known_identities (Session.known_identities) is the
  // one place every application - even one with no user model of its own,
  // like S-Agreement - can resolve an author uuid to a name and a picture.
  _identityFor(uuid) {
    const state = this._options.state ? this._options.state() : {};
    const known = state.known_identities || [];
    return known.find((entry) => entry.uuid === uuid) || null;
  },

  _identityAvatar(identity, addr) {
    const avatar = document.createElement("span");
    avatar.className = "header-avatar shell-peer-avatar status-online";
    if (identity && identity.picture) {
      avatar.style.backgroundImage = 'url("' + identity.picture + '")';
    } else {
      const source = (identity && identity.name) || addr || "?";
      avatar.textContent = source.slice(0, 2).toUpperCase();
    }
    avatar.title = (identity && identity.name) || addr || "Unknown";
    return avatar;
  },

  _agendaRow(item) {
    const state = this._options.state ? this._options.state() : {};
    const me = state.identity_uuid
      || (state.user_profile && state.user_profile.uuid) || "";
    const mine = item.data.author === me;
    const routes = this._agendaRoutes();

    const row = document.createElement("div");
    row.className = "shell-agenda-item";
    row.dataset.priority = item.data.priority || "";
    row.dataset.itemUuid = item.uuid;

    if (routes && routes.move) {
      row.classList.add("has-drag");
      row.draggable = true;
      const handle = document.createElement("button");
      handle.type = "button";
      handle.className = "shell-agenda-drag";
      handle.textContent = "⋮⋮";
      handle.title = "Drag to rearrange";
      handle.setAttribute("aria-label", "Drag to rearrange");
      const clearDrag = () => {
        this._dragAgendaUuid = "";
        row.classList.remove("is-dragging");
        document.querySelectorAll(".shell-agenda-item").forEach((entry) => {
          entry.classList.remove("drop-before", "drop-after");
        });
      };
      const targetAt = (clientX, clientY) => {
        const target = document.elementFromPoint(
          clientX, clientY,
        )?.closest(".shell-agenda-item");
        document.querySelectorAll(".shell-agenda-item").forEach((entry) => {
          entry.classList.remove("drop-before", "drop-after");
        });
        if (!target || target.dataset.itemUuid === item.uuid) return null;
        const bounds = target.getBoundingClientRect();
        const after = clientY > bounds.top + bounds.height / 2;
        target.classList.add(after ? "drop-after" : "drop-before");
        return { target, after };
      };
      const finishDrag = async (clientX, clientY) => {
        if (this._dragAgendaUuid !== item.uuid) return;
        const placement = targetAt(clientX, clientY);
        clearDrag();
        if (!placement) return;
        const targetUuid = placement.target.dataset.itemUuid || "";
        if (!targetUuid || targetUuid === item.uuid) return;
        const items = (
          (this._options.state ? this._options.state() : {}).agenda_items || []
        ).filter((entry) => entry.uuid !== item.uuid);
        let index = items.findIndex((entry) => entry.uuid === targetUuid);
        if (index < 0) return;
        if (placement.after) index += 1;
        try {
          await this._post(routes.move, { item_uuid: item.uuid, index });
          await this._changed();
          this.openCollab();
        } catch (error) { showToast(error.message, true); }
      };
      handle.onmousedown = (event) => {
        if (event.button !== 0) return;
        event.preventDefault();
        this._dragAgendaUuid = item.uuid;
        row.classList.add("is-dragging");
        const move = (moveEvent) => {
          if (this._dragAgendaUuid === item.uuid) {
            targetAt(moveEvent.clientX, moveEvent.clientY);
          }
        };
        const up = (upEvent) => {
          document.removeEventListener("mousemove", move);
          document.removeEventListener("mouseup", up);
          finishDrag(upEvent.clientX, upEvent.clientY);
        };
        document.addEventListener("mousemove", move);
        document.addEventListener("mouseup", up);
      };
      row.ondragstart = (event) => {
        if (this._dragAgendaUuid !== item.uuid) {
          event.preventDefault();
          return;
        }
        if (event.dataTransfer) {
          event.dataTransfer.effectAllowed = "move";
          event.dataTransfer.setData("text/plain", item.uuid);
        }
      };
      row.ondragover = (event) => {
        if (!this._dragAgendaUuid || this._dragAgendaUuid === item.uuid) return;
        event.preventDefault();
        targetAt(event.clientX, event.clientY);
      };
      row.ondrop = (event) => {
        event.preventDefault();
        finishDrag(event.clientX, event.clientY);
      };
      row.ondragend = () => {
        if (this._dragAgendaUuid === item.uuid) clearDrag();
      };
      row.append(handle);
    }

    const text = document.createElement("span");
    text.className = "shell-agenda-text";
    text.textContent = item.data.text || "";
    row.append(text);

    const actions = document.createElement("span");
    actions.className = "shell-agenda-actions";

    // Only the originator may steer their own item; everyone else reads it.
    // That rule is Session's, and the view simply reflects it. Priority and
    // delete stay out of the way until you are looking at your own row.
    if (mine && routes) {
      const priority = document.createElement("select");
      priority.className = "shell-agenda-priority-control";
      priority.setAttribute(
        "aria-label", "Priority for " + (item.data.text || "agenda topic"),
      );
      for (const [value, label] of [["", "No priority"], ["high", "High"],
        ["medium", "Medium"], ["low", "Low"]]) {
        const option = document.createElement("option");
        option.value = value;
        option.textContent = label;
        priority.append(option);
      }
      priority.value = item.data.priority || "";
      priority.onchange = async () => {
        row.dataset.priority = priority.value || "";
        try {
          await this._post(routes.setPriority, {
            item_uuid: item.uuid, priority: priority.value || null,
          });
          await this._changed();
          this.openCollab();
        } catch (error) { showToast(error.message, true); }
      };
      const remove = document.createElement("button");
      remove.type = "button";
      remove.className = "shell-agenda-delete shell-agenda-hover";
      remove.textContent = "Delete";
      remove.onclick = async () => {
        try {
          await this._post(routes.delete, { item_uuid: item.uuid });
          await this._changed();
          this.openCollab();
        } catch (error) { showToast(error.message, true); }
      };
      actions.append(priority, remove);
    } else {
      const priority = document.createElement("span");
      priority.className = "shell-agenda-priority-label";
      priority.textContent = {
        high: "High", medium: "Medium", low: "Low",
      }[item.data.priority] || "No priority";
      actions.append(priority);
    }

    const identity = this._identityFor(item.data.author);
    actions.append(this._identityAvatar(identity, item.data.author));
    row.append(actions);
    return row;
  },

  _renderAgenda() {
    const state = this._options.state ? this._options.state() : {};
    const items = state.agenda_items || [];
    const list = document.getElementById("shellAgendaList");
    list.replaceChildren();
    if (!items.length) {
      const empty = document.createElement("p");
      empty.className = "shell-note";
      empty.textContent = "No discussion topics on this topic yet.";
      list.append(empty);
    }
    for (const item of items) list.append(this._agendaRow(item));
    document.getElementById("shellAgendaForm").hidden = !this._agendaRoutes();
  },

  // Labels for the two universal modes. An application offering more supplies
  // its own labels; Core shows the raw mode rather than inventing wording for
  // a policy it does not interpret.
  AUTO_ADOPT_LABELS: { always: "Adopt automatically", never: "Hold for me to decide" },
  AUTO_ADOPT_DESCRIPTIONS: {
    always: "Peer changes are adopted automatically.",
    never: "Peer changes wait for you to review and adopt them.",
  },

  _autoAdoptControl() {
    const route = this._options.autoAdoptRoute;
    const topic = this._topic();
    if (!route || !topic) return null;
    const state = this._options.state ? this._options.state() : {};
    const modes = state.auto_adopt_modes || ["always", "never"];
    const labels = Object.assign(
      {}, this.AUTO_ADOPT_LABELS, this._options.autoAdoptLabels || {},
    );
    const descriptions = Object.assign(
      {}, this.AUTO_ADOPT_DESCRIPTIONS,
      this._options.autoAdoptDescriptions || {},
    );

    const wrap = document.createElement("div");
    wrap.className = "shell-auto-adopt-setting";
    const row = document.createElement("div");
    row.className = "shell-auto-adopt-row";
    const indicator = document.createElement("span");
    indicator.className = "shell-auto-adopt-indicator";
    indicator.setAttribute("aria-hidden", "true");
    const select = document.createElement("select");
    select.setAttribute("aria-label", "Automatic adoption");
    for (const mode of modes) {
      const option = document.createElement("option");
      option.value = mode;
      option.textContent = labels[mode] || mode;
      select.append(option);
    }
    select.value = state.auto_adopt_mode || "always";
    const description = document.createElement("p");
    description.className = "shell-note shell-auto-adopt-description";
    const renderSelection = () => {
      indicator.replaceChildren();
      if (this._options.renderAutoAdoptIndicator) {
        this._options.renderAutoAdoptIndicator(indicator, select.value);
      } else {
        const filled = select.value === "always" ? 4 : 0;
        for (let index = 0; index < 4; index += 1) {
          const cell = document.createElement("span");
          cell.className = "shell-auto-adopt-cell";
          if (index < filled) cell.classList.add("filled");
          indicator.append(cell);
        }
      }
      description.textContent = descriptions[select.value] || "";
    };
    select.onchange = async () => {
      renderSelection();
      try {
        await this._post(route.path, {
          [route.topicKey]: topic, mode: select.value,
        });
        await this._changed();
      } catch (error) { showToast(error.message, true); }
    };
    row.append(indicator, select);
    wrap.append(row, description);
    renderSelection();
    return wrap;
  },

  openCollab() {
    this._ensureCollabPane();
    this._renderAgenda();
    this._renderDisagreementList(
      document.getElementById("shellDisagreementList"),
    );
    document.getElementById("shellCollabOverlay").hidden = false;
    document.getElementById("shellCollabPane").hidden = false;
    // The page insets beside the pane instead of being covered by it.
    document.body.classList.add("shell-pane-open-left");
  },

  // ---- connections: the right-hand pane -----------------------------

  _connReady: false,
  _panelTopic: "",
  _panelAssignedTarget: "",

  _topic() {
    if (this._panelTopic) return this._panelTopic;
    return this._options.topicUuid ? this._options.topicUuid() : "";
  },

  _ensureConnectionsPane() {
    if (this._connReady) return;
    const host = document.createElement("div");
    host.innerHTML = [
      '<div id="shellConnOverlay" class="shell-pane-overlay" hidden></div>',
      '<aside id="shellConnPane" class="shell-pane shell-pane-right" hidden>',
      '<div class="shell-pane-header">',
      "<strong>Sharing &amp; Sync</strong>",
      '<button type="button" id="shellConnCloseBtn" class="shell-pane-close" aria-label="Close">&times;</button>',
      "</div>",
      '<div class="shell-pane-section">',
      "<h3>People involved</h3>",
      '<div id="shellPeersList" class="shell-peers-list"></div>',
      "</div>",
      '<div class="shell-pane-section" id="shellConnAutoAdopt">',
      "<h3>Automatic adoption</h3>",
      '<div id="shellConnAutoAdoptControl"></div>',
      "</div>",
      '<div class="shell-pane-section">',
      "<h3>Channels</h3>",
      '<div id="shellConnTargetList" class="shell-target-list"></div>',
      '<div id="shellChannelActions" class="shell-row shell-channel-actions">',
      '<button type="button" id="shellUseTokenBtn">Use invite token</button>',
      '<button type="button" id="shellNewTargetBtn">+ New target</button>',
      "</div>",
      '<fieldset id="shellTokenFieldset" class="shell-token-form" hidden>',
      "<legend>Use an invite token</legend>",
      '<label for="shellTokenInput">Invite token</label>',
      '<input id="shellTokenInput" placeholder="Paste an invite token">',
      '<div class="shell-row">',
      '<button type="button" id="shellConnectBtn" class="primary">Connect</button>',
      '<button type="button" id="shellCancelTokenBtn">Cancel</button>',
      "</div>",
      '<p id="shellTokenNote" class="shell-note"></p>',
      "</fieldset>",
      '<fieldset id="shellTargetFieldset" class="shell-target-form" hidden>',
      '<legend id="shellTargetFormTitle">Add an SFTP target</legend>',
      '<input id="shellTargetName" placeholder="Name">',
      '<input id="shellTargetHost" placeholder="Host">',
      '<input id="shellTargetPort" type="number" value="22" min="1" max="65535">',
      '<input id="shellTargetUser" placeholder="Username">',
      '<input id="shellTargetPassword" type="password" placeholder="Password (optional)">',
      '<input id="shellTargetRoot" placeholder="Remote path" value="/">',
      '<input id="shellTargetPoll" type="number" value="3" min="1" max="300" title="Poll every (seconds)">',
      '<p class="shell-note shell-target-form-full">Leave the password blank to use key authentication or your SSH agent. A password entered here is stored in this client&#39;s local session data.</p>',
      '<div class="shell-row shell-target-form-full">',
      '<button type="button" id="shellTestTargetBtn">Test</button>',
      '<button type="button" id="shellSaveTargetBtn" class="primary">Save</button>',
      '<button type="button" id="shellCancelTargetBtn">Cancel</button>',
      "</div>",
      "</fieldset>",
      '<p id="shellTargetsNote" class="shell-note"></p>',
      "</div>",
      "</aside>",
    ].join("");
    document.body.append(...host.children);
    this._connReady = true;

    document.getElementById("shellConnCloseBtn").onclick = () => this.closeConnections();
    document.getElementById("shellConnOverlay").onclick = () => this.closeConnections();
    document.getElementById("shellUseTokenBtn").onclick = () => this._toggleTokenForm(true);
    document.getElementById("shellCancelTokenBtn").onclick = () => this._toggleTokenForm(false);
    document.getElementById("shellNewTargetBtn").onclick = () => this._toggleTargetForm(true);
    document.getElementById("shellCancelTargetBtn").onclick = () => this._toggleTargetForm(false);
    document.getElementById("shellTestTargetBtn").onclick = () => this._testFormTarget();
    document.getElementById("shellSaveTargetBtn").onclick = () => this._saveTarget();
    document.getElementById("shellConnectBtn").onclick = () => this._connect();
  },

  closeConnections() {
    const pane = document.getElementById("shellConnPane");
    const overlay = document.getElementById("shellConnOverlay");
    if (pane) pane.hidden = true;
    if (overlay) overlay.hidden = true;
    document.body.classList.remove("shell-pane-open-right");
  },

  _note(id, message) {
    document.getElementById(id).textContent = message;
  },

  // topicUuid overrides the mounted default, so a multi-topic view such as a
  // board overview can open the pane for one specific topic; targetId is
  // that topic's already-assigned target, since a multi-topic view has no
  // single state().channel_target_id to fall back on.
  openConnectionPanel(panel) {
    const request = panel || {};
    this._ensureConnectionsPane();
    this._panelTopic = request.topicUuid || "";
    const state = this._options.state ? this._options.state() : {};
    this._panelAssignedTarget = request.topicUuid
      ? (request.targetId || "") : (state.channel_target_id || "");

    this._renderPeersList();

    const autoAdoptSection = document.getElementById("shellConnAutoAdopt");
    const autoAdoptControl = document.getElementById(
      "shellConnAutoAdoptControl",
    );
    autoAdoptControl.replaceChildren();
    const adopt = this._autoAdoptControl();
    if (adopt) autoAdoptControl.append(adopt);
    autoAdoptSection.hidden = !adopt;

    this._toggleTokenForm(false);
    this._toggleTargetForm(false);
    this._renderConnTargets();

    document.getElementById("shellConnOverlay").hidden = false;
    document.getElementById("shellConnPane").hidden = false;
    document.body.classList.add("shell-pane-open-right");
  },

  // Every session peer by default. An application whose view is scoped to
  // one topic says which addresses belong to it, so the list shows who is on
  // *this* thing rather than everyone the identity has ever talked to.
  _renderPeersList() {
    const list = document.getElementById("shellPeersList");
    if (!list) return;
    list.replaceChildren();
    const state = this._options.state ? this._options.state() : {};
    const all = (state.network && state.network.peers) || {};
    const peers = this._options.peerAddresses
      ? this._options.peerAddresses().map((addr) => [addr, all[addr] || {}])
      : Object.entries(all);
    if (!peers.length) {
      const empty = document.createElement("p");
      empty.className = "shell-note";
      empty.textContent = "No one else is on this topic yet.";
      list.append(empty);
      return;
    }
    for (const [addr, info] of peers) {
      const described = this._options.describePeer
        ? this._options.describePeer(addr) || {} : {};
      const online = described.online !== undefined
        ? described.online
        : !info.status || info.status.state !== "offline";
      const row = document.createElement("div");
      row.className = "shell-peer-row";
      const avatar = document.createElement("span");
      avatar.className = "header-avatar shell-peer-avatar "
        + (online ? "status-online" : "status-offline");
      if (described.picture) {
        avatar.style.backgroundImage = 'url("' + described.picture + '")';
      } else {
        const source = described.label || addr.replace(/^relay:/, "");
        avatar.textContent = (source.slice(0, 2) || "?").toUpperCase();
      }
      const name = document.createElement("span");
      name.className = "shell-peer-name";
      name.textContent = described.label || addr;
      const status = document.createElement("span");
      status.className = "shell-note shell-peer-status";
      status.textContent = described.status
        || (online ? "Online" : "Offline")
        + (info.channel ? " (" + info.channel + ")" : "");
      row.append(avatar, name, status);
      list.append(row);
    }
  },

  async _assignTopicTarget(targetId) {
    const topic = this._topic();
    if (!topic) throw new Error("Select a topic first.");
    await this._post("/api/channels/mailbox/topics/assign", {
      topic_uuid: topic, target_id: targetId || null,
    });
    this._panelAssignedTarget = targetId || "";
    await this._changed();
  },

  async _copyToken(targetId = "", targetName = "Direct") {
    const topic = this._topic();
    if (!topic) {
      this._note("shellTargetsNote", "Select a topic before creating a token.");
      return;
    }
    try {
      if (targetId && this._panelAssignedTarget !== targetId) {
        await this._assignTopicTarget(targetId);
      }
      const token = await this._post("/api/connect_token", {
        topic_uuids: [topic],
        channel_options: targetId ? { mailbox: { target_id: targetId } } : {},
      });
      await navigator.clipboard.writeText(btoa(JSON.stringify(token)));
      this._note(
        "shellTargetsNote",
        targetId
          ? `Invite token copied. ${targetName} is now in use for this topic.`
          : "Direct invite token copied.",
      );
      await this._renderConnTargets();
    } catch (error) {
      this._note("shellTargetsNote", error.message);
    }
  },

  async _connect() {
    const field = document.getElementById("shellTokenInput");
    let token;
    try {
      token = JSON.parse(atob(field.value.trim()));
    } catch (error) {
      this._note("shellTokenNote", "That is not a share token.");
      return;
    }
    // Core serializes token_version; the channel descriptor carries its own
    // descriptor_version. Testing the wrong one rejects every valid token.
    if (
      token.token_version !== 1 ||
      !Array.isArray(token.topic_uuids) ||
      !token.topic_uuids.length
    ) {
      this._note("shellTokenNote", "Unrecognized token version.");
      return;
    }
    try {
      await this._post("/api/connect", { token });
      field.value = "";
      this._note("shellTokenNote", "Connected.");
      await this._changed();
      this._toggleTokenForm(false);
      await this._renderConnTargets();
    } catch (error) {
      this._note("shellTokenNote", error.message);
    }
  },

  _targetStatus(label, className = "") {
    const badge = document.createElement("span");
    badge.className = "shell-channel-badge " + className;
    badge.textContent = label;
    return badge;
  },

  _channelAction(label, action, className = "") {
    const button = document.createElement("button");
    button.type = "button";
    button.textContent = label;
    if (className) button.className = className;
    button.onclick = action;
    return button;
  },

  async _renderConnTargets() {
    const list = document.getElementById("shellConnTargetList");
    if (!list) return;
    list.replaceChildren();
    let targets = [];
    try {
      const response = await fetch("/api/channels/mailbox/targets");
      targets = (await response.json()).targets || [];
    } catch (error) {
      this._note("shellTargetsNote", "Could not read channel targets.");
      return;
    }

    const direct = document.createElement("div");
    direct.className = "shell-target-row";
    const directName = document.createElement("div");
    directName.className = "shell-target-identity";
    directName.innerHTML = (
      '<span class="shell-target-name">Direct</span>'
      + '<span class="shell-note">Live device-to-device channel</span>'
    );
    const directStatuses = document.createElement("div");
    directStatuses.className = "shell-channel-statuses";
    directStatuses.append(this._targetStatus("Available", "is-available"));
    const state = this._options.state ? this._options.state() : {};
    const allPeers = (state.network && state.network.peers) || {};
    const panelPeers = this._options.peerAddresses
      ? this._options.peerAddresses().map((addr) => allPeers[addr] || {})
      : Object.values(allPeers);
    if (panelPeers.some((peer) => peer.channel === "http")) {
      directStatuses.append(this._targetStatus("In use", "is-in-use"));
    }
    const directActions = document.createElement("div");
    directActions.className = "shell-target-actions";
    directActions.append(this._channelAction(
      "Get token", () => this._copyToken(),
    ));
    direct.append(directName, directStatuses, directActions);
    list.append(direct);

    for (const target of targets) {
      const row = document.createElement("div");
      row.className = "shell-target-row";
      const identity = document.createElement("div");
      identity.className = "shell-target-identity";
      const name = document.createElement("span");
      name.className = "shell-target-name";
      name.textContent = target.name;
      const kind = document.createElement("span");
      kind.className = "shell-note";
      kind.textContent = "Mailbox channel";
      identity.append(name, kind);
      const inUse = (
        this._panelAssignedTarget === target.id
        || (target.topic_uuids || []).includes(this._topic())
      );
      const statuses = document.createElement("div");
      statuses.className = "shell-channel-statuses";
      statuses.append(this._targetStatus("Available", "is-available"));
      if (inUse) {
        statuses.append(this._targetStatus("In use", "is-in-use"));
      }
      const actions = document.createElement("div");
      actions.className = "shell-target-actions";
      if (inUse) {
        actions.append(this._channelAction(
          "Stop using",
          async () => {
            try {
              await this._assignTopicTarget("");
              await this._renderConnTargets();
              this._note(
                "shellTargetsNote",
                `${target.name} is no longer used for this topic.`,
              );
            } catch (error) {
              this._note("shellTargetsNote", error.message);
            }
          },
        ));
      } else {
        actions.append(this._channelAction(
          "Use for this topic",
          async () => {
            try {
              await this._assignTopicTarget(target.id);
              await this._renderConnTargets();
              this._note(
                "shellTargetsNote",
                `${target.name} is now in use for this topic.`,
              );
            } catch (error) {
              this._note("shellTargetsNote", error.message);
            }
          },
        ));
      }
      actions.append(this._channelAction(
        "Get token", () => this._copyToken(target.id, target.name), "primary",
      ));
      const discard = this._channelAction("Discard", () => {}, "danger");
      discard.title = "Stop using this connection and remove it";
      discard.onclick = () => confirmAction(
        "Discard " + target.name + "?",
        "It is removed from your connections; any topic shared through it "
          + "will need another.",
        async () => {
          try {
            await this._post("/api/channels/mailbox/targets/delete", { target_id: target.id });
            if (this._panelAssignedTarget === target.id) this._panelAssignedTarget = "";
            await this._renderConnTargets();
            this._note("shellTargetsNote", target.name + " discarded.");
            await this._changed();
          } catch (error) {
            this._note("shellTargetsNote", error.message);
          }
        },
      );
      actions.append(discard);
      row.append(identity, statuses, actions);
      list.append(row);
    }
  },

  _toggleTokenForm(show) {
    const fieldset = document.getElementById("shellTokenFieldset");
    fieldset.hidden = !show;
    document.getElementById("shellUseTokenBtn").hidden = !!show;
    if (show) {
      this._toggleTargetForm(false);
      document.getElementById("shellTokenInput").value = "";
      this._note("shellTokenNote", "");
      document.getElementById("shellTokenInput").focus();
    }
  },

  _toggleTargetForm(show) {
    document.getElementById("shellTargetFieldset").hidden = !show;
    document.getElementById("shellNewTargetBtn").hidden = !!show;
    if (show) {
      this._toggleTokenForm(false);
      this._resetTargetForm();
      document.getElementById("shellTargetName").focus();
    }
  },

  _resetTargetForm() {
    const values = {
      shellTargetName: "", shellTargetHost: "", shellTargetPort: "22",
      shellTargetUser: "", shellTargetPassword: "", shellTargetRoot: "/",
      shellTargetPoll: "3",
    };
    for (const id of Object.keys(values)) {
      document.getElementById(id).value = values[id];
    }
  },

  _targetFormValues() {
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
    if (password) values.password = password;
    return values;
  },

  async _testFormTarget() {
    const values = this._targetFormValues();
    if (!values.name || !values.host || !values.username) {
      this._note("shellTargetsNote", "Name, host, and username are required.");
      return;
    }
    this._note("shellTargetsNote", "Testing " + values.name + "...");
    try {
      await this._post("/api/channels/mailbox/targets/test-values", values);
      this._note("shellTargetsNote", values.name + " is reachable.");
    } catch (error) {
      this._note("shellTargetsNote", values.name + ": " + error.message);
    }
  },

  async _saveTarget() {
    const values = this._targetFormValues();
    if (!values.name || !values.host || !values.username) {
      this._note("shellTargetsNote", "Name, host, and username are required.");
      return;
    }
    this._note("shellTargetsNote", "Verifying the target...");
    try {
      await this._post("/api/channels/mailbox/targets", values);
      this._toggleTargetForm(false);
      await this._renderConnTargets();
      this._note("shellTargetsNote", values.name + " added.");
      await this._changed();
    } catch (error) {
      this._note("shellTargetsNote", error.message);
    }
  },

  async _changed() {
    if (this._options.onChanged) await this._options.onChanged();
  },
});
