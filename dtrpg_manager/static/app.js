"use strict";
const $ = (s, el = document) => el.querySelector(s);
const $$ = (s, el = document) => [...el.querySelectorAll(s)];
const esc = (v) => String(v ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const PAGE = 60;
const SRC_LABEL = { manual: "", dtrpg: "DTRPG", rule: "rule", pdf: "PDF", llm: "AI" };
const chip = (t, extra = "") => `<span class="chip ${esc(t.kind)} ${t.source && t.source !== "manual" ? "auto" : ""} filterable" data-tag-filter="${esc(t.id)}" data-label="${esc(t.kind)}: ${esc(t.name)}" title="Show all with this tag · ${t.source && t.source !== "manual" ? "added by " + esc(SRC_LABEL[t.source] || t.source) + (t.confidence ? ` (confidence ${(+t.confidence).toFixed(2)})` : "") : "added by you"}">${extra}${esc(t.name)}</span>`;
const state = { meta: null, offset: 0, total: 0, selected: new Set(), listView: false, tagSel: new Set(), filterSel: new Set(),
  pick: { must: { tags: new Set(), filters: new Set() }, any: { tags: new Set(), filters: new Set() } } };

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
  if (b.dataset.view === "enrich") loadEnrich();
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
  $("#tag-names").innerHTML = [...new Set(m.tags.map((t) => t.name))].map((n) => `<option value="${esc(n)}">`).join("");
  renderFacets();
  renderSuggestTags();
  api("/api/suggestions?limit=1").then((r) => { const b = $("#sugg-badge"); b.textContent = r.total; b.classList.toggle("hidden", !r.total); }).catch(() => {});
}
// DTRPG categories: each filter has parent_id (immediate parent) and
// group_name (top-level group: Genre, Rule System, Product Type, Format...).
// Intermediate parents are not always present in the data, so a filter whose
// parent is unknown is shown at the top of its group.
const GROUP_ORDER = ["Genre", "Rule System", "Product Type", "Format"];
function filterGroups() {
  const fl = state.meta.dtrpg_filters, ids = new Set(fl.map((f) => f.id)), byParent = {}, groups = {};
  fl.forEach((f) => (byParent[f.parent_id && ids.has(f.parent_id) ? f.parent_id : "root:" + (f.group_name || "Other")] ||= []).push(f));
  const walk = (key, depth, out) => (byParent[key] || []).forEach((f) => { out.push({ ...f, depth }); walk(f.id, depth + 1, out); });
  [...new Set(fl.map((f) => f.group_name || "Other"))]
    .sort((a, b) => ((GROUP_ORDER.indexOf(a) + 1 || 99) - (GROUP_ORDER.indexOf(b) + 1 || 99)) || a.localeCompare(b))
    .forEach((g) => { groups[g] = []; walk("root:" + g, 0, groups[g]); });
  return groups;
}
function filterTreeOptions() {
  const out = [];
  Object.entries(filterGroups()).forEach(([g, items]) => items.forEach((f) =>
    out.push([f.id, `${g}: ${"— ".repeat(f.depth)}${f.name} (${f.count})`])));
  return out;
}
// One dropdown per tag kind / DTRPG group. Each set (e.g. state.tagSel) holds at
// most one id per dropdown; dropdowns combine with AND.
const KIND_LABEL = { genre: "Genre", system: "System", setting: "Setting", category: "Category", tag: "Tag" };
function tagDropdowns(sel) {
  const kinds = {};
  state.meta.tags.forEach((t) => (kinds[t.kind] ||= []).push(t));
  return state.meta.tag_kinds.filter((k) => kinds[k]).map((k) => {
    const opts = kinds[k].slice().sort((a, b) => a.name.localeCompare(b.name, undefined, { sensitivity: "base" }));
    const cur = opts.find((t) => sel.has(String(t.id)));
    return `<label>${esc(KIND_LABEL[k] || k)} <select data-kind="${esc(k)}"><option value="">Any</option>${opts.map((t) =>
      `<option value="${t.id}" ${cur && cur.id === t.id ? "selected" : ""}>${esc(t.name)} (${t.count})</option>`).join("")}</select></label>`;
  }).join("");
}
function filterDropdowns(sel) {
  return Object.entries(filterGroups()).map(([g, items]) => {
    const cur = items.find((f) => sel.has(String(f.id)));
    return `<label>${esc(g)} <select data-group="${esc(g)}"><option value="">Any</option>${items.map((f) =>
      `<option value="${esc(f.id)}" ${cur && cur.id === f.id ? "selected" : ""}>${"\u2003".repeat(f.depth)}${esc(f.name)} (${f.count})</option>`).join("")}</select></label>`;
  }).join("");
}
function bindDropdowns(root, sel, attr, onChange) {
  $$(`select[${attr}]`, root).forEach((s) => s.addEventListener("change", () => {
    [...s.options].forEach((o) => o.value && sel.delete(o.value));  // one choice per dropdown
    if (s.value) sel.add(s.value);
    onChange();
  }));
}
function renderFacets() {
  $("#f-tags").innerHTML = state.meta.tags.length ? "<h4>Your tags</h4>" + tagDropdowns(state.tagSel) : "";
  const groups = filterDropdowns(state.filterSel);
  $("#f-dtrpg").innerHTML = groups ? "<h4>DriveThruRPG categories</h4>" + groups : "";
  bindDropdowns($("#f-tags"), state.tagSel, "data-kind", reload);
  bindDropdowns($("#f-dtrpg"), state.filterSel, "data-group", reload);
}
const toggle = (set, v, on) => (on ? set.add(v) : set.delete(v));

// ---------- library ----------
function libraryParams() {
  const p = new URLSearchParams();
  const set = (k, v) => v && p.set(k, v);
  set("q", $("#f-q").value); set("sort", $("#f-sort").value); set("status", $("#f-status").value);
  set("product_type", $("#f-type").value); set("publisher", $("#f-publisher").value);
  set("min_rating", $("#f-rating").value); set("players", $("#f-players").value); set("downloaded", $("#f-downloaded").value); set("source", $("#f-source").value);
  if ($("#f-fav").checked) p.set("favourite", "1");
  if ($("#f-untagged").checked) p.set("untagged", "1");
  if ($("#f-archived").checked) p.set("include_archived", "1");
  state.tagSel.forEach((t) => p.append("tag", t));
  state.filterSel.forEach((f) => p.append("filter", f));
  return p;
}
function card(p, selectable = true) {
  const img = p.thumb_url || p.cover_url;
  const cover = img ? `style="background-image:url('${esc(img)}')"` : "";
  const dl = p.file_count ? `<span class="badge" title="files downloaded">${p.downloaded_count ?? 0}/${p.file_count}</span>` : "";
  const fav = (p.source === "local" ? `<span class="badge" title="local file">LOCAL</span>` : "") + (p.favourite ? `<span class="badge">★</span>` : "");
  const rating = p.rating ? `<span class="badge">${"★".repeat(p.rating)}</span>` : "";
  return `<div class="card" data-id="${esc(p.order_product_id)}">
    ${selectable ? `<input type="checkbox" class="sel" ${state.selected.has(p.order_product_id) ? "checked" : ""}>` : ""}
    <div class="badges">${fav}${rating}${dl}</div>
    <div class="cover" ${cover}>${img ? "" : "📘"}</div>
    <div class="body"><div class="title">${esc(p.name)}</div><div class="pub">${[p.publisher, p.status].filter(Boolean).map(esc).join(" · ")}</div>
    <div class="chips">${(p.tags || []).map((t) => chip(t)).join("")}</div></div></div>`;
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
["#f-sort", "#f-status", "#f-type", "#f-publisher", "#f-rating", "#f-players", "#f-downloaded", "#f-source", "#f-fav", "#f-untagged", "#f-archived"].forEach((id) => $(id).addEventListener("change", reload));
function clearFilterInputs() {
  $$("#filters input, #filters select").forEach((i) => (i.type === "checkbox" ? (i.checked = false) : (i.value = i.id === "f-sort" ? "name" : "")));
  state.tagSel.clear(); state.filterSel.clear();
}
// Click a committed tag or DTRPG category anywhere -> library filtered by it.
// Capture phase, so it wins over the card's own "open product" click.
document.addEventListener("click", (e) => {
  const el = e.target.closest("[data-tag-filter], [data-dtrpg-filter]");
  if (!el || e.target.closest("button")) return;
  e.preventDefault(); e.stopPropagation();
  clearFilterInputs();
  if (el.dataset.tagFilter) state.tagSel.add(String(el.dataset.tagFilter));
  else state.filterSel.add(String(el.dataset.dtrpgFilter));
  $("#drawer").classList.add("hidden");
  $('nav button[data-view="library"]').click();
  renderFacets(); reload();
  toast(`Showing: ${el.dataset.label}`);
}, true);
$("#f-clear").addEventListener("click", () => { clearFilterInputs(); renderFacets(); reload(); });
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
  const dtrpgLink = p.product_id ? `<a href="https://www.drivethrurpg.com/en/product/${encodeURIComponent(p.product_id)}${p.slug ? "/" + encodeURIComponent(p.slug) : ""}" target="_blank" rel="noopener">View on DriveThruRPG ↗</a>` : "";
  $("#drawer-body").innerHTML = `
    <div class="detail-head">${p.cover_url ? `<img src="${esc(p.cover_url)}" alt="">` : ""}
      <div><h2>${esc(p.name)}</h2><div class="hint">${esc(p.publisher || "")}${p.source === "local" ? ' <span class="badge">LOCAL</span>' : ""}</div>
      ${p.source === "local" ? `<details class="local-edit"><summary>Edit details</summary>
        <label>Name <input id="d-i-name" value="${esc(p.name)}"></label>
        <label>Publisher <input id="d-i-pub" value="${esc(p.publisher || "")}"></label>
        <label>Description <textarea id="d-i-desc" rows="3">${esc(p.description || "")}</textarea></label>
        <button id="d-i-save">Save details</button></details>` : ""}
      <div class="hint">Purchased: ${esc(p.date_purchased || "unknown")} · Files updated: ${esc(p.file_last_modified || "unknown")}${p.archived ? " · archived" : ""}</div>
      <div>${dtrpgLink}</div>
      ${p.description ? `<p class="desc">${esc(p.description)}</p>` : ""}
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
    <div id="d-sugg"></div>
    ${p.dtrpg_filters.length ? `<h3>DriveThruRPG categories</h3><div class="chips">${p.dtrpg_filters.slice().sort((a, b) => ((GROUP_ORDER.indexOf(a.group) + 1 || 99) - (GROUP_ORDER.indexOf(b.group) + 1 || 99))).map((f) => `<span class="chip dtrpg filterable" data-dtrpg-filter="${esc(f.id)}" data-label="${esc(f.group || "DriveThruRPG")}: ${esc(f.name)}" title="Show all in this category">${f.group ? esc(f.group) + ": " : ""}${esc(f.name)}</span>`).join("")}</div>` : ""}
    <label>Notes <textarea id="d-notes" rows="4">${esc(p.notes || "")}</textarea></label>
    <h3>Files</h3>
    <table class="files">${p.files.map((f) => `<tr><td>${esc(f.filename)}</td><td>${f.size ? (f.size / 1048576).toFixed(1) + " MB" : ""}</td>
      <td>${f.local_path ? `<a href="/files/${encodeURIComponent(p.order_product_id)}/${f.idx}" target="_blank">Open</a>` : ""}</td>
      <td>${p.source === "local" ? `<span class="hint">${esc(f.local_path || "")}</span>` : `<button class="secondary" data-dl="${f.idx}">${f.local_path ? "Re-download" : "Download"}</button>`}</td></tr>`).join("") || "<tr><td>No files listed</td></tr>"}</table>
    ${p.source === "local" ? "" : `<div class="row" style="margin-top:8px"><button id="d-dlall">Download all files</button></div>`}
    <h3>Related <button id="d-like" class="secondary" title="Random pick among similar products">🎲 Something like this</button></h3>
    <div id="d-related" class="hint">Loading…</div>
    <div class="link-add"><select id="d-linkkind">${(m.link_kinds || []).map((k) => `<option>${esc(k)}</option>`).join("")}</select>
      <input id="d-linksearch" list="d-linkopts" placeholder="Link to another product… (type to search)"><datalist id="d-linkopts"></datalist><button id="d-linkadd">Link</button></div>
    ${p.pages ? `<p class="hint">PDF: ${p.pages} pages</p>` : ""}
    <details><summary>Raw API data</summary><pre>${esc(JSON.stringify(p.raw, null, 2))}</pre></details>`;
  const renderTags = (tags) => {
    $("#d-tags").innerHTML = tags.map((t) => chip(t, `${esc(t.kind)}: `).replace(/<\/span>$/, ` <button data-rm="${t.id}" title="remove (won't be suggested again)">✕</button></span>`)).join("") || `<span class="hint">None yet</span>`;
    $$("#d-tags [data-rm]").forEach((b) => b.addEventListener("click", async () => renderTags(await api(`/api/products/${encodeURIComponent(id)}/tags/${b.dataset.rm}`, { method: "DELETE", body: {} }))));
  };
  renderTags(p.tags);
  loadDrawerSuggestions(id);
  loadRelated(id);
  $("#d-like").addEventListener("click", () => { state.like = { id, name: p.name }; $("#drawer").classList.add("hidden"); $('nav button[data-view="suggest"]').click(); renderLike(); $("#roll").click(); });
  let linkTimer;
  $("#d-linksearch").addEventListener("input", (e) => {
    clearTimeout(linkTimer);
    linkTimer = setTimeout(async () => {
      const q = e.target.value.trim(); if (q.length < 2) return;
      const res = await api(`/api/products?limit=15&include_archived=1&q=${encodeURIComponent(q)}`).catch(() => null);
      if (res) $("#d-linkopts").innerHTML = res.items.filter((x) => x.order_product_id !== id).map((x) => `<option value="${esc(x.name)} #${esc(x.order_product_id)}">`).join("");
    }, 200);
  });
  $("#d-linkadd").addEventListener("click", async () => {
    const m2 = $("#d-linksearch").value.match(/#(\w+)\s*$/);
    if (!m2) return toast("Pick a product from the list");
    await api(`/api/products/${encodeURIComponent(id)}/links`, { method: "POST", body: { target: m2[1], kind: $("#d-linkkind").value } }).catch(fail);
    $("#d-linksearch").value = ""; loadRelated(id);
  });
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
  $("#d-dlall")?.addEventListener("click", () => startDownload({ ids: [id] }));
  $("#d-i-save")?.addEventListener("click", async () => {
    const body = { name: $("#d-i-name").value, publisher: $("#d-i-pub").value, description: $("#d-i-desc").value };
    if (await api(`/api/products/${encodeURIComponent(id)}/info`, { method: "PATCH", body }).catch(fail)) { toast("Saved"); openProduct(id); loadMeta(); }
  });
  $("#drawer").classList.remove("hidden");
}
const closeDrawer = () => { $("#drawer").classList.add("hidden"); loadLibrary().catch(fail); };
$("#drawer-close").addEventListener("click", closeDrawer);
$("#drawer").addEventListener("click", (e) => e.target.id === "drawer" && closeDrawer());
document.addEventListener("keydown", (e) => e.key === "Escape" && !$("#drawer").classList.contains("hidden") && closeDrawer());

// ---------- suggestion ----------
// Random pick: two groups ("must" = AND, "any" = OR). Each has one "add…" dropdown
// per tag kind and per DTRPG group; choosing adds a removable chip.
function renderSuggestTags() {
  const tagById = Object.fromEntries(state.meta.tags.map((t) => [String(t.id), t]));
  const fltById = Object.fromEntries(state.meta.dtrpg_filters.map((f) => [String(f.id), f]));
  ["must", "any"].forEach((mode) => {
    const box = $(`#s-${mode}`), sel = state.pick[mode];
    const chips = [...sel.tags].filter((id) => tagById[id]).map((id) => {
      const t = tagById[id];
      return `<span class="chip ${esc(t.kind)}">${esc(KIND_LABEL[t.kind] || t.kind)}: ${esc(t.name)} <button data-rm-tag="${esc(id)}" title="remove">✕</button></span>`;
    }).concat([...sel.filters].filter((id) => fltById[id]).map((id) => {
      const f = fltById[id];
      return `<span class="chip dtrpg">DTRPG ${esc(f.group_name || "")}: ${esc(f.name)} <button data-rm-flt="${esc(id)}" title="remove">✕</button></span>`;
    }));
    $(".picked", box).innerHTML = chips.join("") || `<span class="hint">Nothing chosen — any product qualifies.</span>`;
    // "add" dropdowns: reuse the library builders with nothing pre-selected
    const adders = tagDropdowns(new Set()) + filterDropdowns(new Set()).replace(/<label>/g, "<label>DTRPG ");
    $(".pick-adders", box).innerHTML = adders.replace(/<option value="">Any<\/option>/g, '<option value="">add…</option>');
    $$("select[data-kind]", box).forEach((s) => s.addEventListener("change", () => { if (s.value) { sel.tags.add(s.value); renderSuggestTags(); } }));
    $$("select[data-group]", box).forEach((s) => s.addEventListener("change", () => { if (s.value) { sel.filters.add(s.value); renderSuggestTags(); } }));
    $$("[data-rm-tag]", box).forEach((b) => b.addEventListener("click", () => { sel.tags.delete(b.dataset.rmTag); renderSuggestTags(); }));
    $$("[data-rm-flt]", box).forEach((b) => b.addEventListener("click", () => { sel.filters.delete(b.dataset.rmFlt); renderSuggestTags(); }));
  });
}
$("#roll").addEventListener("click", async () => {
  const p = new URLSearchParams();
  state.pick.must.tags.forEach((t) => p.append("tag", t));
  state.pick.must.filters.forEach((f) => p.append("filter", f));
  state.pick.any.tags.forEach((t) => p.append("any_tag", t));
  state.pick.any.filters.forEach((f) => p.append("any_filter", f));
  [...$("#s-status").selectedOptions].forEach((o) => p.append("status", o.value));
  if ($("#s-type").value) p.set("product_type", $("#s-type").value);
  if ($("#s-players").value) p.set("players", $("#s-players").value);
  if ($("#s-rating").value) p.set("min_rating", $("#s-rating").value);
  if ($("#s-fav").checked) p.set("favourite", "1");
  p.set("count", $("#s-count").value || 1);
  p.set("weighted", $("#s-weighted").checked ? "1" : "0");
  if (state.like) p.set("like", state.like.id);
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
    tags.map((t) => `<tr><td>${esc(t.kind)}</td><td><input value="${esc(t.name)}" data-rename="${t.id}"></td><td><a href="#" data-tag-filter="${t.id}" data-label="${esc(t.kind)}: ${esc(t.name)}" title="Show these products">${t.count}</a></td>
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
async function loadHolds() {
  const r = await api("/api/download-holds").catch(() => null); if (!r) return;
  $("#hold-hours").value = r.hours;
  $("#holds").innerHTML = r.items.length ? `<table class="files">${r.items.map((f) => `<tr>
      <td>${esc(f.product_name)}<br><span class="hint">${esc(f.filename)}</span></td>
      <td class="hint">${esc(f.fail_reason || "")}</td>
      <td class="hint">${f.retry_after ? "retry after " + esc(f.retry_after.replace("T", " ").slice(0, 16)) + " UTC" : "will retry next run"}</td>
      <td><button class="secondary" data-unhold="${esc(f.order_product_id)}|${f.idx}">Retry next run</button></td></tr>`).join("")}</table>
      <button class="secondary" id="unhold-all">Release all</button>` : `<p class="hint">Nothing on hold.</p>`;
  $$("[data-unhold]").forEach((b) => b.addEventListener("click", async () => {
    const [order_product_id, idx] = b.dataset.unhold.split("|");
    await api("/api/download-holds/clear", { method: "POST", body: { order_product_id, idx: +idx } }).catch(fail); loadHolds();
  }));
  $("#unhold-all")?.addEventListener("click", async () => { await api("/api/download-holds/clear", { method: "POST", body: { all: true } }).catch(fail); loadHolds(); });
}
$("#hold-hours").addEventListener("change", (e) => api("/api/config", { method: "PATCH", body: { hold_failed_hours: Math.max(0, +e.target.value || 0) } }).then(() => { toast("Saved"); loadHolds(); }).catch(fail));
async function loadSettings() {
  loadHolds();
  api("/api/config").then((c) => { $("#local-folders").value = (c.local_folders || []).join("\n"); }).catch(() => {});
  const c = await api("/api/config");
  const f = $("#settings-form");
  f.api_key.value = ""; f.api_key.placeholder = c.api_key_set ? `saved (${c.api_key})` : "paste your application key";
  f.library_path.value = c.library_path; f.threads.value = c.threads; f.download_delay.value = c.download_delay; f.verify_existing.checked = c.verify_existing;
  f.omit_publisher.checked = c.omit_publisher; f.validate_checksums.checked = c.validate_checksums; f.include_archived.checked = c.include_archived;
  f.anthropic_api_key.value = ""; f.anthropic_api_key.placeholder = c.anthropic_api_key_set ? `saved ${c.anthropic_api_key || "(from environment)"}` : "optional — only for AI tag suggestions";
  f.fetch_filters.checked = c.fetch_filters; f.request_timeout.value = c.request_timeout;
}
$("#settings-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const f = e.target, body = { library_path: f.library_path.value, threads: Math.min(8, Math.max(1, +f.threads.value || 4)),
    download_delay: f.download_delay.value === "" ? 0 : +f.download_delay.value, verify_existing: f.verify_existing.checked,
    omit_publisher: f.omit_publisher.checked, validate_checksums: f.validate_checksums.checked, include_archived: f.include_archived.checked,
    fetch_filters: f.fetch_filters.checked, request_timeout: +f.request_timeout.value || 120 };
  if (f.api_key.value) body.api_key = f.api_key.value.trim();
  if (f.anthropic_api_key.value) body.anthropic_api_key = f.anthropic_api_key.value.trim();
  await api("/api/config", { method: "PATCH", body }).then(() => toast("Settings saved")).catch(fail);
  loadSettings();
});
const saveFolders = () => api("/api/config", { method: "PATCH", body: { local_folders: $("#local-folders").value.split("\n").map((x) => x.trim()).filter(Boolean) } });
$("#local-save").addEventListener("click", () => saveFolders().then(() => toast("Folders saved")).catch(fail));
$("#local-scan").addEventListener("click", () => saveFolders().then(() => api("/api/local/scan", { method: "POST", body: {} }))
  .then(() => { toast("Scanning — progress under Jobs"); pollJobs(); }).catch(fail));
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
    ${j.state === "running" ? `<button class="secondary" data-cancel="${esc(j.id)}">Stop</button>` : ""}<pre title="newest first">${esc(j.log.slice().reverse().join("\n"))}</pre></div>`).join("") || `<p class="hint">No jobs yet.</p>`;
  $$("[data-cancel]").forEach((b) => b.addEventListener("click", () => api(`/api/jobs/${b.dataset.cancel}/cancel`, { method: "POST", body: {} }).then(() => toast("Stopping…")).catch(fail)));
  if (jobs.some((j) => j.state === "running")) jobTimer = setTimeout(pollJobs, 1500);
  else if (jobs.length) { loadMeta(); loadLibrary(); }
}

loadMeta().then(loadLibrary).catch(fail);

// ---------- drawer: suggestions & related ----------
async function loadDrawerSuggestions(id) {
  const r = await api(`/api/suggestions?order_product_id=${encodeURIComponent(id)}`).catch(() => null);
  const el = $("#d-sugg"); if (!el || !r) return;
  el.innerHTML = r.items.length ? `<div class="hint">Suggested — click ✓ to accept, ✕ to reject:</div><div class="chips">` + r.items.map((x) =>
    `<span class="chip suggested" title="${esc(x.source)} · confidence ${x.confidence.toFixed(2)}">${esc(x.kind)}: ${esc(x.name)}${x.is_new ? " (new)" : ""}
      <button data-acc="${x.id}">✓</button><button data-rej="${x.id}">✕</button></span>`).join("") + "</div>" : "";
  const decide = async (action, sid) => {
    await api(`/api/suggestions/${action}`, { method: "POST", body: { ids: [sid] } }).catch(fail);
    openProduct(id);
    loadMeta();
  };
  $$("[data-acc]", el).forEach((b) => b.addEventListener("click", () => decide("accept", +b.dataset.acc)));
  $$("[data-rej]", el).forEach((b) => b.addEventListener("click", () => decide("reject", +b.dataset.rej)));
}
async function loadRelated(id) {
  const r = await api(`/api/products/${encodeURIComponent(id)}/related`).catch(() => null);
  const el = $("#d-related"); if (!el || !r) return;
  const row = (x, extra = "") => `<li data-open="${esc(x.order_product_id)}">${x.thumb_url ? `<img src="${esc(x.thumb_url)}" alt="">` : ""}<span>${esc(x.name || "?")}${extra}</span></li>`;
  let html = "";
  if (r.links.length) html += "<h4>Linked</h4><ul class=\"rel-list\">" + r.links.map((l) => row(l, ` <span class="why">${esc(l.direction === "out" ? l.kind : "← " + l.kind)}</span> <button class="secondary" data-unlink="${esc(l.order_product_id)}|${esc(l.kind)}">✕</button>`)).join("") + "</ul>";
  if (r.same_line.length) html += "<h4>Same line</h4><ul class=\"rel-list\">" + r.same_line.map((x) => row(x)).join("") + "</ul>";
  const top = Math.max(...r.similar.map((x) => x.score), 0.01);
  if (r.similar.length) html += "<h4>Similar</h4><ul class=\"rel-list\">" + r.similar.map((x) =>
    `<li data-open="${esc(x.order_product_id)}" title="similarity ${x.score.toFixed(2)}"><span class="bar"><span style="width:${Math.round(x.score / top * 100)}%"></span></span>${x.thumb_url ? `<img src="${esc(x.thumb_url)}" alt="">` : ""}<span>${esc(x.name || "?")}<br><span class="why">${esc(x.why.join(" · "))}</span></span></li>`).join("") + "</ul>";
  el.className = ""; el.innerHTML = html || `<span class="hint">Nothing related found yet — more tags make this better.</span>`;
  $$("[data-open]", el).forEach((li) => li.addEventListener("click", (e) => { if (e.target.dataset.unlink) return; openProduct(li.dataset.open); }));
  $$("[data-unlink]", el).forEach((b) => b.addEventListener("click", async (e) => {
    e.stopPropagation(); const [other, kind] = b.dataset.unlink.split("|");
    await api(`/api/products/${encodeURIComponent(id)}/links/${encodeURIComponent(other)}/${encodeURIComponent(kind)}`, { method: "DELETE", body: {} }).catch(fail);
    loadRelated(id);
  }));
}
function renderLike() {
  const el = $("#s-like");
  if (!state.like) { el.classList.add("hidden"); return; }
  el.innerHTML = `Like: <b>${esc(state.like.name)}</b> <button title="clear">✕</button>`;
  el.classList.remove("hidden");
  $("button", el).addEventListener("click", () => { state.like = null; renderLike(); });
}

// ---------- enrich view ----------
const qState = { offset: 0, total: 0 };
const QPAGE = 50;
function show(id, data) { $(id).textContent = typeof data === "string" ? data : JSON.stringify(data, null, 2); }
async function loadEnrich() {
  fillSelect($("#q-kind"), [...state.meta.tag_kinds, "product_type", "players", "name", "publisher"].map((k) => [k, k]));
  loadQueue(); loadBatches();
  const conf = await api("/api/enrich/config").catch(fail);
  if (conf) $("#enrich-config").value = JSON.stringify(conf, null, 2);
}
function qFilters() {
  return { source: $("#q-source").value || undefined, kind: $("#q-kind").value || undefined, min_confidence: $("#q-conf").value || undefined };
}
async function loadQueue() {
  const f = qFilters(), p = new URLSearchParams({ limit: QPAGE, offset: qState.offset });
  Object.entries(f).forEach(([k, v]) => v !== undefined && p.set(k, v));
  const r = await api("/api/suggestions?" + p).catch(fail); if (!r) return;
  qState.total = r.total;
  $("#q-count").textContent = `${r.total} pending` + (Object.keys(r.pending_by_source).length ? ` (${Object.entries(r.pending_by_source).map(([k, v]) => `${k}: ${v}`).join(", ")})` : "");
  $("#q-table").innerHTML = r.items.map((x) => `<tr>
      <td>${x.thumb_url ? `<img src="${esc(x.thumb_url)}" alt="">` : ""}</td>
      <td><a href="#" data-open="${esc(x.order_product_id)}">${esc(x.product_name)}</a><br><span class="hint">${esc(x.publisher || "")}</span></td>
      <td><span class="chip ${esc(x.kind)}">${esc(x.kind)}: ${esc(x.name)}</span>${x.is_new ? ' <span class="hint">new tag</span>' : ""}</td>
      <td class="hint">${esc(x.source)}${x.evidence ? `<br><span class="evidence">${esc(x.evidence)}</span>` : ""}</td>
      <td><span class="conf" style="width:${Math.round(x.confidence * 50)}px"></span> ${x.confidence.toFixed(2)}</td>
      <td><button data-acc="${x.id}">✓</button> <button class="secondary" data-rej="${x.id}">✕</button></td></tr>`).join("") || `<tr><td class="hint">Nothing waiting for review.</td></tr>`;
  $("#q-page").textContent = r.total ? `${qState.offset + 1}–${Math.min(qState.offset + QPAGE, r.total)} of ${r.total}` : "";
  $("#q-prev").disabled = qState.offset === 0; $("#q-next").disabled = qState.offset + QPAGE >= r.total;
  $$("#q-table [data-open]").forEach((a) => a.addEventListener("click", (e) => { e.preventDefault(); openProduct(a.dataset.open); }));
  const decide = (action, sid) => api(`/api/suggestions/${action}`, { method: "POST", body: { ids: [sid] } }).then(() => { loadQueue(); loadMeta(); }).catch(fail);
  $$("#q-table [data-acc]").forEach((b) => b.addEventListener("click", () => decide("accept", +b.dataset.acc)));
  $$("#q-table [data-rej]").forEach((b) => b.addEventListener("click", () => decide("reject", +b.dataset.rej)));
}
["#q-source", "#q-kind", "#q-conf"].forEach((id) => $(id).addEventListener("change", () => { qState.offset = 0; loadQueue(); }));
$("#q-prev").addEventListener("click", () => { qState.offset = Math.max(0, qState.offset - QPAGE); loadQueue(); });
$("#q-next").addEventListener("click", () => { qState.offset += QPAGE; loadQueue(); });
async function decideAll(action) {
  if (!qState.total) return;
  if (!confirm(`${action === "accept" ? "Accept" : "Reject"} all ${qState.total} suggestions matching the current filters?`)) return;
  const r = await api(`/api/suggestions/${action}`, { method: "POST", body: { ...qFilters(), all_matching: true } }).catch(fail);
  if (r) toast(`${r.decided} suggestions ${action}ed`);
  qState.offset = 0; loadQueue(); loadMeta();
}
$("#q-accept-all").addEventListener("click", () => decideAll("accept"));
$("#q-reject-all").addEventListener("click", () => decideAll("reject"));

$("#p-dtrpg").addEventListener("click", async () => { show("#p-dtrpg-out", "Working…"); const r = await api("/api/enrich/dtrpg", { method: "POST", body: {} }).catch(fail); if (r) { show("#p-dtrpg-out", `${r.tags_added} tags added across ${r.products} products; ${r.product_types_set} product types set`); loadMeta(); } });
async function rules(preview) {
  show("#p-rules-out", "Working…");
  const r = await api("/api/enrich/rules", { method: "POST", body: { preview } }).catch(fail); if (!r) return;
  show("#p-rules-out", Object.entries(r.matches).map(([k, v]) => `${k}: ${v}${r.examples[k].length ? "  e.g. " + r.examples[k][0] : ""}`).join("\n") + (preview ? "\n(preview — nothing queued)" : `\n${r.queued} suggestions queued`));
  if (!preview) { loadQueue(); loadMeta(); }
}
$("#p-rules-preview").addEventListener("click", () => rules(true));
$("#p-rules").addEventListener("click", () => rules(false));
$("#p-pdf").addEventListener("click", () => api("/api/enrich/pdf", { method: "POST", body: { force: $("#p-pdf-force").checked } }).then(() => { show("#p-pdf-out", "Started — progress under Settings & sync › Jobs"); }).catch(fail));
let lastEstimate = null;
$("#p-llm-est").addEventListener("click", async () => {
  const p = new URLSearchParams({ scope: $("#p-llm-scope").value, pdf: $("#p-llm-pdf").checked ? "1" : "0" });
  const e = await api("/api/enrich/llm/estimate?" + p).catch(fail); if (!e) return;
  const limit = +$("#p-llm-limit").value || e.products, n = Math.min(limit, e.products), f = e.products ? n / e.products : 0;
  lastEstimate = { ...e, n };
  show("#p-llm-out", `${n} products → about $${(e.est_cost_usd_batch * f).toFixed(2)} with batch pricing ($${(e.est_cost_usd_standard * f).toFixed(2)} standard)\n~${Math.round(e.est_input_tokens * f).toLocaleString()} input / ~${Math.round(e.est_output_tokens * f).toLocaleString()} output tokens on ${e.model}\n${e.note}\ne.g. ${e.sample.slice(0, 5).join("; ")}`);
  $("#p-llm-submit").disabled = !n;
});
$("#p-llm-submit").addEventListener("click", async () => {
  if (!lastEstimate) return;
  if (!confirm(`Submit ${lastEstimate.n} products to Claude Haiku 5.5? Estimated cost about $${(lastEstimate.est_cost_usd_batch * (lastEstimate.n / lastEstimate.products)).toFixed(2)}.`)) return;
  const r = await api("/api/enrich/llm/submit", { method: "POST", body: { confirm: true, scope: $("#p-llm-scope").value, pdf: $("#p-llm-pdf").checked, limit: +$("#p-llm-limit").value || null } }).catch(fail);
  if (r) { show("#p-llm-out", `Submitted ${r.submitted} requests as batch ${r.batch_id || "-"} (${r.status || ""}). Results usually arrive within an hour — use “Check & import”.`); $("#p-llm-submit").disabled = true; loadBatches(); }
});
async function loadBatches() {
  const b = await api("/api/enrich/llm/batches").catch(() => []);
  $("#p-llm-batches").innerHTML = b.map((x) => `<div>${esc(x.created_at.slice(0, 16).replace("T", " "))} · ${x.request_count} products · <b>${esc(x.status)}</b>
    ${x.status !== "imported" ? `<button class="secondary" data-collect="${esc(x.id)}">Check &amp; import</button>` : ` · ${x.summary.suggestions ?? 0} suggestions · ≈$${(x.summary.approx_cost_usd ?? 0).toFixed(3)}`}</div>`).join("");
  $$("[data-collect]").forEach((btn) => btn.addEventListener("click", async () => {
    const r = await api(`/api/enrich/llm/batches/${btn.dataset.collect}/collect`, { method: "POST", body: {} }).catch(fail);
    if (r) { show("#p-llm-out", r.status === "imported" ? `Imported: ${r.suggestions ?? 0} suggestions (${r.succeeded ?? 0} ok, ${r.errored ?? 0} errors, ${r.refused ?? 0} refused)` : `Still ${r.status}: ${r.succeeded ?? 0} done, ${r.processing ?? "?"} processing`); loadBatches(); loadQueue(); loadMeta(); }
  }));
}
$("#enrich-save").addEventListener("click", async () => {
  let conf; try { conf = JSON.parse($("#enrich-config").value); } catch (e) { return toast("Invalid JSON: " + e.message); }
  const r = await api("/api/enrich/config", { method: "PUT", body: conf }).catch(fail);
  if (r) { toast("Saved"); $("#enrich-config").value = JSON.stringify(r, null, 2); }
});
$("#enrich-reset").addEventListener("click", loadEnrich);
