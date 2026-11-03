"use strict";
const $ = (s, el = document) => el.querySelector(s);
const $$ = (s, el = document) => [...el.querySelectorAll(s)];
const esc = (v) => String(v ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const PAGE = 60;
const state = { meta: null, offset: 0, total: 0, selected: new Set(), listView: false, tagSel: new Set(), filterSel: new Set(), sTags: new Set() };

async function api(path, opts = {}) {
  const init = { method: opts.method || "GET", headers: {} };
  if (opts.body !== undefined) { init.headers["Content-Type"] = "application/json"; init.body = JSON.stringify(opts.body); }
  const r = await fetch(path, init);
  const data = r.headers.get("content-type")?.includes("json") ? await r.json() : await r.text();
  if (!r.ok) throw new Error(data.error || data || r.statusText);
  return data;
}
function toast(msg) { const t = $("#toast"); t.textContent = msg; t.classList.remove("hidden"); clearTimeout(t._h); t._h = setTimeout(() => t.classList.add("hidden"), 3000); }
const fail = (e) => toast("Error: " + e.message);

// ---------- navigation ----------
$$(".topbar nav button").forEach((b) => b.addEventListener("click", () => {
  $$(".topbar nav button").forEach((x) => x.classList.toggle("active", x === b));
  $$(".view").forEach((v) => v.classList.toggle("active", v.id === "view-" + b.dataset.view));
  if (b.dataset.view === "tags") renderTagAdmin();
  if (b.dataset.view === "settings") { loadSettings(); pollJobs(); }
}));

// ---------- reference data ----------
function fillSelect(sel, values, keepFirst = true) {
  const first = keepFirst ? sel.options[0]?.outerHTML || "" : "";
  sel.innerHTML = first + values.map(([v, l]) => `<option value="${esc(v)}">${esc(l)}</option>`).join("");
}
async function loadMeta() {
  const m = await api("/api/meta");
  state.meta = m;
  const s = m.stats;
  $("#stats").textContent = `${s.products} products · ${s.downloaded}/${s.files} files downloaded · ${s.untagged} untagged` + (s.last_synced ? ` · synced ${s.last_synced.slice(0, 16).replace("T", " ")}` : "");
  const statuses = m.statuses.map((x) => [x, x]);
  const types = m.product_types.map((x) => [x, x]);
  [["#f-status", statuses], ["#f-type", types], ["#s-type", types], ["#bulk-status", statuses]].forEach(([id, v]) => {
    const el = $(id), cur = el.value; fillSelect(el, v); el.value = cur;
  });
  fillSelect($("#s-status"), statuses, false);
  fillSelect($("#bulk-kind"), m.tag_kinds.map((k) => [k, k]), false);
  const pub = $("#f-publisher"), cur = pub.value;
  fillSelect(pub, m.publishers.map((p) => [p.name, `${p.name} (${p.count})`])); pub.value = cur;
  fillSelect($("#s-filter"), filterTreeOptions());
  $("#tag-names").innerHTML = [...new Set(m.tags.map((t) => t.name))].map((n) => `<option value="${esc(n)}">`).join("");
  renderFacets();
  renderSuggestTags();
}
function filterTreeOptions() {
  const fl = state.meta.dtrpg_filters, byParent = {};
  fl.forEach((f) => (byParent[f.parent_id || ""] ||= []).push(f));
  const ids = new Set(fl.map((f) => f.id)), out = [];
  const walk = (pid, depth) => (byParent[pid] || []).forEach((f) => {
    out.push([f.id, `${"— ".repeat(depth)}${f.group_name && !depth ? f.group_name + ": " : ""}${f.name} (${f.count})`]);
    walk(f.id, depth + 1);
  });
  walk("", 0);
  fl.filter((f) => f.parent_id && !ids.has(f.parent_id)).forEach((f) => { out.push([f.id, `${f.name} (${f.count})`]); walk(f.id, 1); });
  return out;
}
function renderFacets() {
  const kinds = {};
  state.meta.tags.forEach((t) => (kinds[t.kind] ||= []).push(t));
  $("#f-tags").innerHTML = state.meta.tag_kinds.filter((k) => kinds[k]).map((k) => `<h4>${esc(k)}</h4>` + kinds[k].map((t) =>
    `<label class="opt"><input type="checkbox" data-tag="${t.id}" ${state.tagSel.has(String(t.id)) ? "checked" : ""}> ${esc(t.name)}<span class="n">${t.count}</span></label>`).join("")).join("");
  const fl = filterTreeOptions();
  $("#f-dtrpg").innerHTML = fl.length ? "<h4>DriveThruRPG categories</h4>" + fl.map(([id, label]) =>
    `<label class="opt"><input type="checkbox" data-filter="${esc(id)}" ${state.filterSel.has(id) ? "checked" : ""}> ${esc(label)}</label>`).join("") : "";
  $$("#f-tags input").forEach((i) => i.addEventListener("change", () => { toggle(state.tagSel, i.dataset.tag, i.checked); reload(); }));
  $$("#f-dtrpg input").forEach((i) => i.addEventListener("change", () => { toggle(state.filterSel, i.dataset.filter, i.checked); reload(); }));
}
const toggle = (set, v, on) => (on ? set.add(v) : set.delete(v));

// ---------- library ----------
function libraryParams() {
  const p = new URLSearchParams();
  const set = (k, v) => v && p.set(k, v);
  set("q", $("#f-q").value); set("sort", $("#f-sort").value); set("status", $("#f-status").value);
  set("product_type", $("#f-type").value); set("publisher", $("#f-publisher").value);
  set("min_rating", $("#f-rating").value); set("players", $("#f-players").value); set("downloaded", $("#f-downloaded").value);
  if ($("#f-fav").checked) p.set("favourite", "1");
  if ($("#f-untagged").checked) p.set("untagged", "1");
  if ($("#f-archived").checked) p.set("include_archived", "1");
  state.tagSel.forEach((t) => p.append("tag", t));
  state.filterSel.forEach((f) => p.append("filter", f));
  return p;
}
function card(p, selectable = true) {
  const cover = p.cover_url ? `style="background-image:url('${esc(p.cover_url)}')"` : "";
  const dl = p.file_count ? `<span class="badge" title="files downloaded">${p.downloaded_count ?? 0}/${p.file_count}</span>` : "";
  const fav = p.favourite ? `<span class="badge">★</span>` : "";
  const rating = p.rating ? `<span class="badge">${"★".repeat(p.rating)}</span>` : "";
  return `<div class="card" data-id="${esc(p.order_product_id)}">
    ${selectable ? `<input type="checkbox" class="sel" ${state.selected.has(p.order_product_id) ? "checked" : ""}>` : ""}
    <div class="badges">${fav}${rating}${dl}</div>
    <div class="cover" ${cover}>${p.cover_url ? "" : "📘"}</div>
    <div class="body"><div class="title">${esc(p.name)}</div><div class="pub">${esc(p.publisher || "")} · ${esc(p.status)}</div>
    <div class="chips">${(p.tags || []).map((t) => `<span class="chip ${esc(t.kind)}">${esc(t.name)}</span>`).join("")}</div></div></div>`;
}
function bindCards(root) {
  $$(".card", root).forEach((c) => c.addEventListener("click", (e) => {
    if (e.target.classList.contains("sel")) { toggle(state.selected, c.dataset.id, e.target.checked); updateBulk(); return; }
    openProduct(c.dataset.id);
  }));
}
async function loadLibrary() {
  const p = libraryParams(); p.set("limit", PAGE); p.set("offset", state.offset);
  const res = await api("/api/products?" + p);
  state.total = res.total;
  const g = $("#grid");
  g.classList.toggle("list", state.listView);
  g.innerHTML = res.items.length ? res.items.map((x) => card(x)).join("") :
    `<p class="hint">No products${state.meta?.stats.products ? " match these filters" : " yet — open <b>Settings &amp; sync</b>, add your API key and sync"}.</p>`;
  bindCards(g);
  $("#result-count").textContent = `${res.total} result${res.total === 1 ? "" : "s"}`;
  $("#page").textContent = res.total ? `${state.offset + 1}–${Math.min(state.offset + PAGE, res.total)} of ${res.total}` : "";
  $("#prev").disabled = state.offset === 0; $("#next").disabled = state.offset + PAGE >= res.total;
  $("#select-all").checked = false;
}
const reload = () => { state.offset = 0; loadLibrary().catch(fail); };
let qTimer;
$("#f-q").addEventListener("input", () => { clearTimeout(qTimer); qTimer = setTimeout(reload, 250); });
["#f-sort", "#f-status", "#f-type", "#f-publisher", "#f-rating", "#f-players", "#f-downloaded", "#f-fav", "#f-untagged", "#f-archived"].forEach((id) => $(id).addEventListener("change", reload));
$("#f-clear").addEventListener("click", () => {
  $$("#filters input, #filters select").forEach((i) => (i.type === "checkbox" ? (i.checked = false) : (i.value = i.id === "f-sort" ? "name" : "")));
  state.tagSel.clear(); state.filterSel.clear(); renderFacets(); reload();
});
$("#prev").addEventListener("click", () => { state.offset = Math.max(0, state.offset - PAGE); loadLibrary().catch(fail); });
$("#next").addEventListener("click", () => { state.offset += PAGE; loadLibrary().catch(fail); });
$("#view-toggle").addEventListener("click", (e) => { state.listView = !state.listView; e.target.textContent = state.listView ? "Grid view" : "List view"; loadLibrary(); });
$("#select-all").addEventListener("change", (e) => { $$("#grid .card").forEach((c) => { $(".sel", c).checked = e.target.checked; toggle(state.selected, c.dataset.id, e.target.checked); }); updateBulk(); });
function updateBulk() { $("#bulk").classList.toggle("hidden", state.selected.size === 0); $("#bulk-tag").textContent = `Tag ${state.selected.size} selected`; }
$("#bulk-tag").addEventListener("click", async () => {
  const name = $("#bulk-name").value.trim(); if (!name) return toast("Enter a tag name");
  await api("/api/bulk/tag", { method: "POST", body: { ids: [...state.selected], kind: $("#bulk-kind").value, name } }).catch(fail);
  toast(`Tagged ${state.selected.size} products`); $("#bulk-name").value = ""; await loadMeta(); loadLibrary();
});
$("#bulk-status").addEventListener("change", async (e) => {
  if (!e.target.value) return;
  await api("/api/bulk/meta", { method: "POST", body: { ids: [...state.selected], changes: { status: e.target.value } } }).catch(fail);
  e.target.value = ""; loadLibrary(); loadMeta();
});
$("#bulk-download").addEventListener("click", () => startDownload({ ids: [...state.selected] }));

// ---------- product drawer ----------
async function openProduct(id) {
  const p = await api("/api/products/" + encodeURIComponent(id)).catch(fail);
  if (!p) return;
  const m = state.meta;
  const opt = (vals, cur) => vals.map((v) => `<option ${v === cur ? "selected" : ""}>${esc(v)}</option>`).join("");
  const dtrpgLink = p.product_id ? `<a href="https://www.drivethrurpg.com/en/product/${encodeURIComponent(p.product_id)}" target="_blank" rel="noopener">View on DriveThruRPG ↗</a>` : "";
  $("#drawer-body").innerHTML = `
    <div class="detail-head">${p.cover_url ? `<img src="${esc(p.cover_url)}" alt="">` : ""}
      <div><h2>${esc(p.name)}</h2><div class="hint">${esc(p.publisher || "")}</div>
      <div class="hint">Purchased: ${esc(p.date_purchased || "unknown")} · Files updated: ${esc(p.file_last_modified || "unknown")}${p.archived ? " · archived" : ""}</div>
      <div>${dtrpgLink}</div>
      <div class="stars" id="d-stars">${[1, 2, 3, 4, 5].map((n) => `<span data-n="${n}" class="${(p.rating || 0) >= n ? "on" : ""}">★</span>`).join("")}</div>
      <label class="check"><input type="checkbox" id="d-fav" ${p.favourite ? "checked" : ""}> Favourite</label>
      <label class="check"><input type="checkbox" id="d-excl" ${p.exclude_random ? "checked" : ""}> Exclude from random picks</label></div></div>
    <div class="meta-grid">
      <label>Status <select id="d-status">${opt(m.statuses, p.status)}</select></label>
      <label>Type <select id="d-type"><option value="">—</option>${opt(m.product_types, p.product_type)}</select></label>
      <label>Min players <input id="d-minp" type="number" min="1" value="${esc(p.min_players ?? "")}"></label>
      <label>Max players <input id="d-maxp" type="number" min="1" value="${esc(p.max_players ?? "")}"></label>
    </div>
    <h3>Genres, systems &amp; tags</h3>
    <div class="chips" id="d-tags"></div>
    <div class="add-tag"><select id="d-kind">${m.tag_kinds.map((k) => `<option>${k}</option>`).join("")}</select>
      <input id="d-tagname" list="tag-names" placeholder="e.g. horror, D&D 5e, Forgotten Realms"><button id="d-addtag">Add</button></div>
    ${p.dtrpg_filters.length ? `<h3>DriveThruRPG categories</h3><div class="chips">${p.dtrpg_filters.map((f) => `<span class="chip dtrpg">${f.group ? esc(f.group) + ": " : ""}${esc(f.name)}</span>`).join("")}</div>` : ""}
    <label>Notes <textarea id="d-notes" rows="4">${esc(p.notes || "")}</textarea></label>
    <h3>Files</h3>
    <table class="files">${p.files.map((f) => `<tr><td>${esc(f.filename)}</td><td>${f.size ? (f.size / 1048576).toFixed(1) + " MB" : ""}</td>
      <td>${f.local_path ? `<a href="/files/${encodeURIComponent(p.order_product_id)}/${f.idx}" target="_blank">Open</a>` : ""}</td>
      <td><button class="secondary" data-dl="${f.idx}">${f.local_path ? "Re-download" : "Download"}</button></td></tr>`).join("") || "<tr><td>No files listed</td></tr>"}</table>
    <div class="row" style="margin-top:8px"><button id="d-dlall">Download all files</button></div>
    <details><summary>Raw API data</summary><pre>${esc(JSON.stringify(p.raw, null, 2))}</pre></details>`;
  const renderTags = (tags) => {
    $("#d-tags").innerHTML = tags.map((t) => `<span class="chip ${esc(t.kind)}">${esc(t.kind)}: ${esc(t.name)} <button data-rm="${t.id}" title="remove">✕</button></span>`).join("") || `<span class="hint">None yet</span>`;
    $$("#d-tags [data-rm]").forEach((b) => b.addEventListener("click", async () => renderTags(await api(`/api/products/${encodeURIComponent(id)}/tags/${b.dataset.rm}`, { method: "DELETE", body: {} }))));
  };
  renderTags(p.tags);
  const save = (changes) => api("/api/products/" + encodeURIComponent(id), { method: "PATCH", body: changes }).then(() => toast("Saved")).catch(fail);
  $$("#d-stars span").forEach((s) => s.addEventListener("click", () => {
    const n = +s.dataset.n === p.rating ? 0 : +s.dataset.n; p.rating = n;
    $$("#d-stars span").forEach((x) => x.classList.toggle("on", +x.dataset.n <= n)); save({ rating: n || null });
  }));
  $("#d-fav").addEventListener("change", (e) => save({ favourite: e.target.checked ? 1 : 0 }));
  $("#d-excl").addEventListener("change", (e) => save({ exclude_random: e.target.checked ? 1 : 0 }));
  $("#d-status").addEventListener("change", (e) => save({ status: e.target.value }));
  $("#d-type").addEventListener("change", (e) => save({ product_type: e.target.value || null }));
  $("#d-minp").addEventListener("change", (e) => save({ min_players: e.target.value ? +e.target.value : null }));
  $("#d-maxp").addEventListener("change", (e) => save({ max_players: e.target.value ? +e.target.value : null }));
  $("#d-notes").addEventListener("change", (e) => save({ notes: e.target.value }));
  const addTag = async () => {
    const name = $("#d-tagname").value.trim(); if (!name) return;
    renderTags(await api(`/api/products/${encodeURIComponent(id)}/tags`, { method: "POST", body: { kind: $("#d-kind").value, name } }).catch(fail) || p.tags);
    $("#d-tagname").value = ""; loadMeta();
  };
  $("#d-addtag").addEventListener("click", addTag);
  $("#d-tagname").addEventListener("keydown", (e) => e.key === "Enter" && addTag());
  $$("[data-dl]").forEach((b) => b.addEventListener("click", () => startDownload({ ids: [id], idx: [+b.dataset.dl], force: true })));
  $("#d-dlall").addEventListener("click", () => startDownload({ ids: [id] }));
  $("#drawer").classList.remove("hidden");
}
const closeDrawer = () => { $("#drawer").classList.add("hidden"); loadLibrary().catch(fail); };
$("#drawer-close").addEventListener("click", closeDrawer);
$("#drawer").addEventListener("click", (e) => e.target.id === "drawer" && closeDrawer());
document.addEventListener("keydown", (e) => e.key === "Escape" && !$("#drawer").classList.contains("hidden") && closeDrawer());

// ---------- suggestion ----------
function renderSuggestTags() {
  $("#s-tags").innerHTML = state.meta.tags.map((t) => `<span class="chip ${esc(t.kind)} ${state.sTags.has(String(t.id)) ? "on" : ""}" data-id="${t.id}">${esc(t.kind)}: ${esc(t.name)}</span>`).join("") || `<span class="hint">Add tags to products first</span>`;
  $$("#s-tags .chip").forEach((c) => c.addEventListener("click", () => { const on = !state.sTags.has(c.dataset.id); toggle(state.sTags, c.dataset.id, on); c.classList.toggle("on", on); }));
}
$("#roll").addEventListener("click", async () => {
  const p = new URLSearchParams();
  state.sTags.forEach((t) => p.append("tag", t));
  if ($("#s-filter").value) p.set("filter", $("#s-filter").value);
  [...$("#s-status").selectedOptions].forEach((o) => p.append("status", o.value));
  if ($("#s-type").value) p.set("product_type", $("#s-type").value);
  if ($("#s-players").value) p.set("players", $("#s-players").value);
  if ($("#s-rating").value) p.set("min_rating", $("#s-rating").value);
  if ($("#s-fav").checked) p.set("favourite", "1");
  p.set("count", $("#s-count").value || 1);
  p.set("weighted", $("#s-weighted").checked ? "1" : "0");
  const picks = await api("/api/suggest?" + p).catch(fail);
  if (!picks) return;
  const out = $("#suggest-out");
  out.innerHTML = picks.length ? picks.map((x) => card(x, false)).join("") : `<p class="hint">Nothing matches those filters.</p>`;
  bindCards(out);
});

// ---------- tag admin ----------
function renderTagAdmin() {
  const tags = state.meta.tags;
  $("#tag-admin").innerHTML = tags.length ? `<table class="tag-table"><tr><th>Kind</th><th>Name</th><th>Products</th><th></th></tr>` +
    tags.map((t) => `<tr><td>${esc(t.kind)}</td><td><input value="${esc(t.name)}" data-rename="${t.id}"></td><td>${t.count}</td>
      <td><button class="secondary" data-del="${t.id}">Delete</button></td></tr>`).join("") + "</table>" : `<p class="hint">No tags yet.</p>`;
  $$("[data-rename]").forEach((i) => i.addEventListener("change", async () => { await api(`/api/tags/${i.dataset.rename}`, { method: "PATCH", body: { name: i.value } }).catch(fail); loadMeta(); }));
  $$("[data-del]").forEach((b) => b.addEventListener("click", async () => {
    if (!confirm("Delete this tag from all products?")) return;
    await api(`/api/tags/${b.dataset.del}`, { method: "DELETE", body: {} }).catch(fail); await loadMeta(); renderTagAdmin();
  }));
  const fl = filterTreeOptions();
  $("#dtrpg-admin").innerHTML = fl.length ? "<ul>" + fl.map(([, l]) => `<li>${esc(l)}</li>`).join("") + "</ul>" :
    `<p class="hint">No DriveThruRPG categories found in the synced data. If products show none after a sync, run <code>dtrpg-manager diagnose -o diagnostic.json</code> and check where the API puts them.</p>`;
}

// ---------- settings / jobs ----------
async function loadSettings() {
  const c = await api("/api/config");
  const f = $("#settings-form");
  f.api_key.value = ""; f.api_key.placeholder = c.api_key_set ? `saved (${c.api_key})` : "paste your application key";
  f.library_path.value = c.library_path; f.threads.value = c.threads; f.download_delay.value = c.download_delay;
  f.omit_publisher.checked = c.omit_publisher; f.validate_checksums.checked = c.validate_checksums; f.include_archived.checked = c.include_archived;
  f.fetch_filters.checked = c.fetch_filters; f.request_timeout.value = c.request_timeout;
}
$("#settings-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const f = e.target, body = { library_path: f.library_path.value, threads: +f.threads.value || 1,
    download_delay: f.download_delay.value === "" ? 2 : +f.download_delay.value,
    omit_publisher: f.omit_publisher.checked, validate_checksums: f.validate_checksums.checked, include_archived: f.include_archived.checked,
    fetch_filters: f.fetch_filters.checked, request_timeout: +f.request_timeout.value || 120 };
  if (f.api_key.value) body.api_key = f.api_key.value.trim();
  await api("/api/config", { method: "PATCH", body }).then(() => toast("Settings saved")).catch(fail);
  loadSettings();
});
$("#do-sync").addEventListener("click", () => api("/api/sync", { method: "POST", body: {} }).then(() => { toast("Sync started"); pollJobs(); }).catch(fail));
$("#do-download-all").addEventListener("click", () => startDownload({ all: true }));
function startDownload(body) { api("/api/download", { method: "POST", body }).then(() => { toast("Download started — see Settings & sync › Jobs"); pollJobs(); }).catch(fail); }
$("#import-file").addEventListener("change", async (e) => {
  const file = e.target.files[0]; if (!file) return;
  try { const r = await api("/api/import", { method: "POST", body: JSON.parse(await file.text()) }); toast(`Imported ${r.imported}`); loadMeta(); }
  catch (err) { fail(err); }
});
let jobTimer;
async function pollJobs() {
  clearTimeout(jobTimer);
  const jobs = await api("/api/jobs").catch(() => []);
  $("#jobs").innerHTML = jobs.map((j) => `<div class="job ${j.state}"><b>${esc(j.kind)}</b> — ${esc(j.state)} (started ${esc(j.started)})
    ${j.state === "running" ? `<button class="secondary" data-cancel="${esc(j.id)}">Stop</button>` : ""}<pre>${esc(j.log.join("\n"))}</pre></div>`).join("") || `<p class="hint">No jobs yet.</p>`;
  $$("[data-cancel]").forEach((b) => b.addEventListener("click", () => api(`/api/jobs/${b.dataset.cancel}/cancel`, { method: "POST", body: {} }).then(() => toast("Stopping…")).catch(fail)));
  if (jobs.some((j) => j.state === "running")) jobTimer = setTimeout(pollJobs, 1500);
  else if (jobs.length) { loadMeta(); loadLibrary(); }
}

loadMeta().then(loadLibrary).catch(fail);
