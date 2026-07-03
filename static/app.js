/* ComfyUI Queue Manager — Dashboard-Logik
 *
 * Pollt /api/state, rendert pro Host eine Karte mit Running/Pending-Queue,
 * macht die Pending-Liste per SortableJS drag-and-drop-sortierbar und
 * verwaltet die Priority-IP-Regeln.
 *
 * Performance bei langen Queues: Jede Host-Karte hat einen Fingerprint über
 * ihren Inhalt; das DOM (und die Sortable-Instanz) wird nur neu aufgebaut,
 * wenn sich der Fingerprint ändert. Idle-Polls kosten damit keine DOM-Arbeit,
 * und während eines Drags wird das Rendern komplett pausiert.
 */
"use strict";

const state = {
  hosts: [],
  rules: [],
  pollInterval: 3000,
  hostFilter: "",      // "" = alle Hosts
  dragging: false,     // während Drag kein Re-Render
  busy: false,         // während einer Mutation kein Poll-Render
};

const sortables = new Map(); // hostName -> Sortable-Instanz
const cardCache = new Map(); // hostName -> { fp, el }
let rulesFp = "";

// ---------------------------------------------------------------------------
// API-Helfer
// ---------------------------------------------------------------------------

async function api(path, options = {}) {
  const resp = await fetch(path, {
    headers: { "Content-Type": "application/json" },
    ...options,
  });
  if (!resp.ok) {
    let detail = resp.statusText;
    try { detail = (await resp.json()).detail || detail; } catch (_) {}
    throw new Error(detail);
  }
  return resp.json();
}

function toast(message, ok = false) {
  const el = document.getElementById("toast");
  el.textContent = message;
  el.className = ok ? "ok" : "";
  el.hidden = false;
  clearTimeout(el._timer);
  el._timer = setTimeout(() => { el.hidden = true; }, 4000);
}

// ---------------------------------------------------------------------------
// Polling
// ---------------------------------------------------------------------------

async function poll(force = false) {
  try {
    const data = await api(force ? "/api/refresh" : "/api/state", force ? { method: "POST" } : {});
    state.hosts = data.hosts;
    state.rules = data.priority_ips;
    state.pollInterval = Math.max(1000, data.poll_interval_seconds * 1000);
    if (!state.dragging && !state.busy) render();
  } catch (e) {
    toast("Backend nicht erreichbar: " + e.message);
  }
}

function schedulePolling() {
  poll();
  setInterval(poll, state.pollInterval);
}

// ---------------------------------------------------------------------------
// Rendering
// ---------------------------------------------------------------------------

function esc(s) {
  const div = document.createElement("div");
  div.textContent = s == null ? "" : String(s);
  return div.innerHTML;
}

function fmtTime(ts) {
  if (!ts) return "–";
  return new Date(ts * 1000).toLocaleTimeString("de-DE");
}

function ipBadge(item) {
  const ip = item.ip || "unbekannt";
  const cls = item.priority_rule === "front" ? " prio-front"
            : item.priority_rule === "back" ? " prio-back" : "";
  const marker = item.priority_rule === "front" ? " ↑"
               : item.priority_rule === "back" ? " ↓" : "";
  return `<span class="ip-badge${cls}" title="Client-IP">${esc(ip)}${marker}</span>`;
}

function itemHtml(item, host) {
  const running = item.status === "running";
  return `
    <li class="queue-item ${item.status}" data-id="${esc(item.prompt_id)}">
      ${running ? '<div class="spinner" title="läuft"></div>'
                : '<span class="drag-handle" title="Ziehen zum Umsortieren">⠿</span><span class="pos">${POS}</span>'.replace("${POS}", item.position)}
      <div class="item-body">
        <div class="item-name" title="${esc(item.name)}">${esc(item.name)}</div>
        <div class="item-meta">
          ${ipBadge(item)}
          <span title="Einreihungszeit">🕒 ${fmtTime(item.submitted_at)}</span>
          <span title="Anzahl Nodes">${item.node_count} Nodes</span>
          <span title="prompt_id">${esc(item.prompt_id.slice(0, 8))}</span>
        </div>
      </div>
      ${running
        ? `<button class="item-del" title="Job unterbrechen" onclick="interruptJob('${esc(host)}','${esc(item.prompt_id)}')">⏹</button>`
        : `<button class="item-del" title="Aus Queue entfernen" onclick="deleteJob('${esc(host)}','${esc(item.prompt_id)}')">✕</button>`}
    </li>`;
}

function hostCardHtml(h) {
  const badge = h.online
    ? '<span class="badge online">online</span>'
    : '<span class="badge offline">offline</span>';
  const errorHtml = h.online ? "" : `<div class="host-error">⚠ ${esc(h.error)}</div>`;

  const runningHtml = h.running.length
    ? h.running.map((it) => itemHtml(it, h.name)).join("")
    : '<div class="empty">Kein laufender Job</div>';

  const pendingHtml = h.pending.length
    ? h.pending.map((it) => itemHtml(it, h.name)).join("")
    : '<div class="empty">Queue ist leer</div>';

  return `
    <div class="host-card" data-host="${esc(h.name)}">
      <div class="host-header">
        <span class="host-name">${esc(h.name)}</span>
        ${badge}
        <span class="host-url">${esc(h.url)}</span>
        <div class="host-actions">
          <button onclick="clearQueue('${esc(h.name)}')" title="Alle pending Jobs entfernen">Queue leeren</button>
        </div>
      </div>
      ${errorHtml}
      <div class="queue-section">
        <h3>Running (${h.running.length})</h3>
        <ul class="queue-list">${runningHtml}</ul>
        <h3>Pending (${h.pending.length})</h3>
        <ul class="queue-list pending-list" data-host="${esc(h.name)}">${pendingHtml}</ul>
      </div>
    </div>`;
}

function hostFingerprint(h) {
  // updated_at bewusst ausgenommen: Die Karte wird nur neu gebaut, wenn
  // sich am sichtbaren Inhalt wirklich etwas geändert hat.
  return JSON.stringify([h.online, h.error, h.running, h.pending]);
}

function initSortable(host, card) {
  const ul = card.querySelector(".pending-list");
  if (!ul) return;
  sortables.set(host, new Sortable(ul, {
    handle: ".drag-handle",
    animation: 150,
    onStart: () => { state.dragging = true; },
    onEnd: async (evt) => {
      state.dragging = false;
      if (evt.oldIndex === evt.newIndex) return;
      const order = [...ul.querySelectorAll("li[data-id]")].map((li) => li.dataset.id);
      await reorder(host, order);
    },
  }));
}

function dropCard(name) {
  sortables.get(name)?.destroy();
  sortables.delete(name);
  cardCache.get(name)?.el.remove();
  cardCache.delete(name);
}

function render() {
  renderFilter();

  const container = document.getElementById("hosts");
  const visible = state.hosts.filter((h) => !state.hostFilter || h.name === state.hostFilter);

  // Karten verschwundener oder ausgefilterter Hosts entfernen
  for (const name of [...cardCache.keys()]) {
    if (!visible.some((h) => h.name === name)) dropCard(name);
  }

  container.querySelector(".no-hosts")?.remove();
  if (!visible.length) {
    container.insertAdjacentHTML(
      "beforeend",
      '<div class="empty no-hosts">Keine Hosts konfiguriert (siehe config.json)</div>',
    );
  }

  // Nur Karten mit geändertem Inhalt neu aufbauen
  for (const h of visible) {
    const fp = hostFingerprint(h);
    const entry = cardCache.get(h.name);
    if (entry && entry.fp === fp) continue;

    const tpl = document.createElement("template");
    tpl.innerHTML = hostCardHtml(h).trim();
    const card = tpl.content.firstElementChild;

    sortables.get(h.name)?.destroy();
    sortables.delete(h.name);
    if (entry) entry.el.replaceWith(card);
    else container.appendChild(card);

    cardCache.set(h.name, { fp, el: card });
    initSortable(h.name, card);
  }

  // Kartenreihenfolge an die Host-Reihenfolge angleichen (no-op im Normalfall)
  let cursor = null;
  for (const h of visible) {
    const el = cardCache.get(h.name).el;
    const expected = cursor ? cursor.nextElementSibling : container.firstElementChild;
    if (expected !== el) container.insertBefore(el, cursor ? cursor.nextSibling : container.firstChild);
    cursor = el;
  }

  renderRules();

  const newest = Math.max(0, ...state.hosts.map((h) => h.updated_at || 0));
  document.getElementById("last-update").textContent =
    newest ? "Stand: " + fmtTime(newest) : "–";
}

function renderFilter() {
  const nav = document.getElementById("host-filter");
  const wanted = ["", ...state.hosts.map((h) => h.name)];
  const existing = [...nav.querySelectorAll(".filter-btn")].map((b) => b.dataset.host);
  if (JSON.stringify(wanted) !== JSON.stringify(existing)) {
    nav.innerHTML = wanted.map((name) =>
      `<button class="filter-btn" data-host="${esc(name)}">${name ? esc(name) : "Alle Hosts"}</button>`
    ).join("");
    nav.querySelectorAll(".filter-btn").forEach((btn) => {
      btn.onclick = () => { state.hostFilter = btn.dataset.host; render(); };
    });
  }
  nav.querySelectorAll(".filter-btn").forEach((btn) => {
    btn.classList.toggle("active", btn.dataset.host === state.hostFilter);
  });
}

function renderRules() {
  const fp = JSON.stringify(state.rules);
  if (fp === rulesFp) return;
  rulesFp = fp;

  const list = document.getElementById("rule-list");
  list.innerHTML = state.rules.length
    ? state.rules.map((r) => `
        <li>
          <span class="rule-ip">${esc(r.ip)}</span>
          <span class="rule-mode ${esc(r.mode)}">${r.mode === "front" ? "↑ nach vorne" : "↓ nach hinten"}</span>
          <button class="item-del" title="Regel löschen" onclick="removeRule('${esc(r.ip)}')">✕</button>
        </li>`).join("")
    : '<li class="empty" style="border:none;background:none">Keine Regeln definiert</li>';
}

// ---------------------------------------------------------------------------
// Aktionen (global, weil aus onclick-Attributen aufgerufen)
// ---------------------------------------------------------------------------

async function mutate(fn, okMessage) {
  state.busy = true;
  try {
    await fn();
    if (okMessage) toast(okMessage, true);
  } catch (e) {
    toast("Fehler: " + e.message);
  } finally {
    state.busy = false;
    poll();
  }
}

async function reorder(host, order) {
  await mutate(async () => {
    try {
      const res = await api(`/api/hosts/${host}/reorder`, {
        method: "POST",
        body: JSON.stringify({ order }),
      });
      const failed = res.report.failed || [];
      if (failed.length) throw new Error(`${failed.length} Job(s) konnten nicht neu eingereiht werden`);
    } catch (e) {
      // Drag-Zustand im DOM passt nicht mehr zum Server: Karte beim
      // nächsten Poll zwingend neu aufbauen.
      dropCard(host);
      throw e;
    }
  }, "Queue umsortiert");
}

async function deleteJob(host, id) {
  await mutate(() => api(`/api/hosts/${host}/delete`, {
    method: "POST",
    body: JSON.stringify({ ids: [id] }),
  }), "Job entfernt");
}

async function interruptJob(host, id) {
  await mutate(() => api(`/api/hosts/${host}/interrupt`, {
    method: "POST",
    body: JSON.stringify({ prompt_id: id }),
  }), "Job unterbrochen");
}

async function clearQueue(host) {
  if (!confirm(`Wirklich alle pending Jobs auf "${host}" löschen?`)) return;
  await mutate(() => api(`/api/hosts/${host}/clear`, { method: "POST" }), "Queue geleert");
}

async function saveRules(rules) {
  await mutate(async () => {
    state.rules = await api("/api/priority-ips", {
      method: "PUT",
      body: JSON.stringify(rules),
    });
    renderRules();
  }, "Priority-Regeln gespeichert");
}

function removeRule(ip) {
  saveRules(state.rules.filter((r) => r.ip !== ip));
}

// ---------------------------------------------------------------------------
// Init
// ---------------------------------------------------------------------------

document.getElementById("rule-form").addEventListener("submit", (e) => {
  e.preventDefault();
  const ip = document.getElementById("rule-ip").value.trim();
  const mode = document.getElementById("rule-mode").value;
  if (!ip) return;
  const rules = state.rules.filter((r) => r.ip !== ip);
  rules.push({ ip, mode });
  document.getElementById("rule-ip").value = "";
  saveRules(rules);
});

document.getElementById("refresh-btn").addEventListener("click", () => poll(true));

schedulePolling();
