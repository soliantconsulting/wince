// Shared by every wince page: data, helpers, the top bar, scanning and the toast.
const css = n => getComputedStyle(document.documentElement).getPropertyValue(n).trim();
const fmt = n => n >= 1000 ? (n / 1000).toFixed(1) + "k" : String(n);
const esc = s => String(s ?? "").replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;");
const params = new URLSearchParams(location.search);
// A scan is one folder seen through one Claude config folder (~/.claude, or another one used with CLAUDE_CONFIG_DIR).
const DEFAULT_CFG = "~/.claude";
const cfgOf = s => s.config || DEFAULT_CFG;
const scanKey = s => s.folder + (cfgOf(s) === DEFAULT_CFG ? "" : "|" + cfgOf(s));
const fromKey = k => { const [folder, config] = k.split("|"); return { folder, config: config || DEFAULT_CFG }; };
const scanLabel = s => s.folder + (cfgOf(s) === DEFAULT_CFG ? "" : "  ·  " + cfgOf(s));
const day = iso => iso ? new Date(iso.length <= 10 ? iso + "T12:00:00" : iso).toLocaleDateString(undefined, { month: "short", day: "numeric", year: "numeric" }) : "";
const VERSION = "0.1.0";  // keep in step with VERSION in wince/core.py
const SERVER = location.origin + "/";
const TYPE_LABEL = { conflict: "Conflict", overlap: "Overlap", redundant: "Duplicate", dead: "Not loading", heavy: "Large", provenance: "Loads everywhere" };
const flowUrl = (folder, config) => "flow.html?folder=" + encodeURIComponent(folder) + (config && config !== DEFAULT_CFG ? "&config=" + encodeURIComponent(config) : "");

const ICONS = {
  flow: "M22 11V3h-7v3H9V3H2v8h7V8h2v10h4v3h7v-8h-7v3h-2V8h2v3z",
  compare: "M9.01 14H2v2h7.01v3L13 15l-3.99-4v3zm5.98-1v-3H22V8h-7.01V5L11 9l3.99 4z",
  folder: "M20 6h-8l-2-2H4c-1.1 0-1.99.9-1.99 2L2 18c0 1.1.9 2 2 2h16c1.1 0 2-.9 2-2V8c0-1.1-.9-2-2-2zm0 12H4V8h16v10z",
  refresh: "M17.65 6.35C16.2 4.9 14.21 4 12 4c-4.42 0-7.99 3.58-7.99 8s3.57 8 7.99 8c3.73 0 6.84-2.55 7.73-6h-2.08c-.82 2.33-3.04 4-5.65 4-3.31 0-6-2.69-6-6s2.69-6 6-6c1.66 0 3.14.69 4.22 1.78L13 11h7V4l-2.35 2.35z",
  about: "M11 7h2v2h-2zm0 4h2v6h-2zm1-9C6.48 2 2 6.48 2 12s4.48 10 10 10 10-4.48 10-10S17.52 2 12 2zm0 18c-4.41 0-8-3.59-8-8s3.59-8 8-8 8 3.59 8 8-3.59 8-8 8z",
  newfolder: "M20 6h-8l-2-2H4c-1.11 0-1.99.89-1.99 2L2 18c0 1.11.89 2 2 2h16c1.11 0 2-.89 2-2V8c0-1.11-.89-2-2-2zm-1 8h-3v3h-2v-3h-3v-2h3V9h2v3h3v2z",
  settings: "M19.14 12.94c.04-.3.06-.61.06-.94 0-.32-.02-.64-.07-.94l2.03-1.58c.18-.14.23-.41.12-.61l-1.92-3.32c-.12-.22-.37-.29-.59-.22l-2.39.96c-.5-.38-1.03-.7-1.62-.94l-.36-2.54c-.04-.24-.24-.41-.48-.41h-3.84c-.24 0-.43.17-.47.41l-.36 2.54c-.59.24-1.13.57-1.62.94l-2.39-.96c-.22-.08-.47 0-.59.22L2.74 8.87c-.12.21-.08.47.12.61l2.03 1.58c-.05.3-.09.63-.09.94s.02.64.07.94l-2.03 1.58c-.18.14-.23.41-.12.61l1.92 3.32c.12.22.37.29.59.22l2.39-.96c.5.38 1.03.7 1.62.94l.36 2.54c.05.24.24.41.48.41h3.84c.24 0 .44-.17.47-.41l.36-2.54c.59-.24 1.13-.56 1.62-.94l2.39.96c.22.08.47 0 .59-.22l1.92-3.32c.12-.22.07-.47-.12-.61l-2.01-1.58zM12 15.6c-1.98 0-3.6-1.62-3.6-3.6s1.62-3.6 3.6-3.6 3.6 1.62 3.6 3.6-1.62 3.6-3.6 3.6z",
  search: "M15.5 14h-.79l-.28-.27C15.41 12.59 16 11.11 16 9.5 16 5.91 13.09 3 9.5 3S3 5.91 3 9.5 5.91 16 9.5 16c1.61 0 3.09-.59 4.23-1.57l.27.28v.79l5 4.99L20.49 19l-4.99-5zm-6 0C7.01 14 5 11.99 5 9.5S7.01 5 9.5 5 14 7.01 14 9.5 11.99 14 9.5 14z",
};
// Size a select to its chosen option so its arrow sits right after the text.
function fitSelect(el) {
  const fit = () => {
    const c = fitSelect.c || (fitSelect.c = document.createElement("canvas").getContext("2d"));
    const cs = getComputedStyle(el);
    c.font = `${cs.fontWeight} ${cs.fontSize} ${cs.fontFamily}`;
    el.style.width = Math.ceil(c.measureText(el.selectedOptions[0]?.text || "").width) + 46 + "px";
  };
  fit();
  document.fonts?.ready.then(fit);  // Roboto may arrive after the first measurement, and it's wider than the fallback
}
const icon = name => `<svg viewBox="0 0 24 24" aria-hidden="true"><path d="${ICONS[name]}"/></svg>`;

function renderTopBar(page, scan) {
  const folder = scan?.folder;
  const link = (id, label, href) => `<a href="${href}" class="${page === id ? "on" : ""}">${icon(id)}${label}</a>`;
  document.getElementById("topbar").innerHTML = `
    <a class="org" href="https://www.soliantconsulting.com" target="_blank" rel="noopener" title="soliantconsulting.com (opens in a new tab)"><img src="soliant-curve.png" alt="Soliant Consulting website"></a>
    <a class="brand" href="flow.html"><b>wince</b><small>where your context comes from</small></a>
    <nav class="tabs">${link("flow", "Flow", folder ? flowUrl(folder, cfgOf(scan)) : "flow.html")}${link("compare", "Compare", "compare.html" + (folder ? "?a=" + encodeURIComponent(scanKey(scan)) : ""))}${link("about", "About", "about.html")}</nav>
`;
}

// The "Scan a folder…" button, placed right after the folder dropdown on each page.
function addScanButton(after, config) {
  const btn = document.createElement("button");
  btn.id = "scan"; btn.className = "icon-btn";
  btn.title = "Scan a folder… Pick one and see what reaches Claude there" + (config && config !== DEFAULT_CFG ? ` with ${config}` : "");
  btn.setAttribute("aria-label", "Scan a folder");
  btn.innerHTML = `${icon("newfolder")}<span class="sr">Scan a folder…</span>`;
  after.after(btn);
  btn.onclick = async () => {
    const r = await post("/api/pick", {}, btn, "Pick a folder…", "A folder picker is asking which folder to scan. It may be behind this window.");
    if (r?.status === 501) {  // no native picker on this system: ask for a path instead
      const folder = prompt("Folder to scan (full path or ~/…):");
      if (folder) location.href = flowUrl(folder, config);
    } else if (r?.canceled) toast("Scan canceled.", 3000);
    else if (r) location.href = flowUrl(r.folder, config);
  };
}

function toast(html, ms = 9000) {
  const t = document.getElementById("toast");
  t.innerHTML = html; t.hidden = false;
  clearTimeout(toast.timer);
  if (ms) toast.timer = setTimeout(() => t.hidden = true, ms);
}

const NEED_SERVER = `This needs the wince server. Start it with <code>wince serve</code> and open <a href="${SERVER}">${SERVER}</a>.`;

const API = { "Content-Type": "application/json", "X-Wince": "1" };
const failed = e => toast(e instanceof TypeError ? "Can't reach the wince server. " + NEED_SERVER : esc(e.message), 15000);

// A live scan of one folder through one config folder, with its saved conflict check. Throws with the server's message.
async function loadScan(folder, config) {
  const res = await fetch(`/api/scan?folder=${encodeURIComponent(folder)}&config=${encodeURIComponent(config || DEFAULT_CFG)}`, { headers: API });
  const r = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(r.error || `The server answered ${res.status}.`);
  return r;
}
async function recentScans() {
  const res = await fetch("/api/recent", { headers: API });
  return res.ok ? (await res.json()).recent : [];
}

// POST to the API with a busy state on the button. Returns the reply (with status 501 passed through), or null after a toast.
async function post(path, body, btn, busy, hint) {
  const label = btn.querySelector("span"), was = label.textContent;
  btn.disabled = true; label.textContent = busy;
  if (hint) toast(hint, 0);
  try {
    const res = await fetch(path, { method: "POST", headers: API, body: JSON.stringify(body) });
    const r = await res.json().catch(() => ({}));
    if (res.status === 501 && path === "/api/pick") return { status: 501 };
    if (!res.ok) throw new Error(r.error || `The server answered ${res.status}.`);
    document.getElementById("toast").hidden = true;
    return r;
  } catch (e) {
    failed(e);
    return null;
  } finally {
    btn.disabled = false; label.textContent = was;
  }
}

function emptyState() {
  document.getElementById("main").innerHTML = `<div class="empty card"><h2>Nothing scanned yet</h2>
    <p>Click the new-folder button (${icon("newfolder")}) above and pick a project folder. wince shows everything that reaches Claude in a session there, and where each piece comes from.</p></div>`;
}

// Draggable divider between the canvas and the side panel. Width is remembered per browser.
function addSplitter() {
  const main = document.getElementById("main"), side = document.getElementById("side");
  if (!side) return;
  const KEY = "wince.sideWidth", MIN = 280, minCanvas = 520;
  const bar = document.createElement("div");
  bar.className = "splitter";
  bar.setAttribute("role", "separator");
  bar.setAttribute("aria-orientation", "vertical");
  bar.setAttribute("aria-label", "Resize side panel");
  bar.tabIndex = 0;
  bar.title = "Drag to resize. Double-click to reset.";
  side.before(bar);
  const clamp = w => Math.round(Math.max(MIN, Math.min(w, main.clientWidth - minCanvas)));
  let pending;
  const set = (w, save = true) => {
    w = clamp(w);
    main.style.setProperty("--side-w", w + "px");
    bar.setAttribute("aria-valuenow", w);
    if (save) try { localStorage.setItem(KEY, w); } catch (e) { /* storage blocked: width just isn't remembered */ }
    cancelAnimationFrame(pending);
    pending = requestAnimationFrame(() => window.dispatchEvent(new Event("resize")));  // redraw the sankey at the new width
  };
  try { const w = +localStorage.getItem(KEY); if (w) set(w, false); } catch (e) { /* ignore */ }
  bar.addEventListener("pointerdown", e => {
    e.preventDefault();
    bar.setPointerCapture(e.pointerId);
    bar.classList.add("dragging");
    const right = main.getBoundingClientRect().right - parseFloat(getComputedStyle(main).paddingRight);
    const move = ev => set(right - ev.clientX - bar.offsetWidth / 2);
    const ends = ["pointerup", "pointercancel", "lostpointercapture"];
    const up = () => { bar.classList.remove("dragging"); bar.removeEventListener("pointermove", move); ends.forEach(t => bar.removeEventListener(t, up)); };
    bar.addEventListener("pointermove", move);
    ends.forEach(t => bar.addEventListener(t, up));
  });
  bar.addEventListener("dblclick", () => { main.style.removeProperty("--side-w"); try { localStorage.removeItem(KEY); } catch (e) { /* ignore */ } window.dispatchEvent(new Event("resize")); });
  bar.addEventListener("keydown", e => {
    const step = e.shiftKey ? 80 : 20, now = side.getBoundingClientRect().width;
    if (e.key === "ArrowLeft") { set(now + step); e.preventDefault(); }
    if (e.key === "ArrowRight") { set(now - step); e.preventDefault(); }
  });
}
