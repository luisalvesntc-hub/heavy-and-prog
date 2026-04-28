const INDEX_URL = "data/index.json";
const WEEK_URL = wk => `data/weeks/${wk}.json`;
const INITIAL_SHOW = 12;
const PAGE_SIZE = 12;

const enc = s => encodeURIComponent(s);

function domainOf(url) {
  try { return new URL(url).hostname.replace(/^www\./, ""); }
  catch { return ""; }
}

const FILTER_CATS = [
  { id: "all",          label: "All",                  match: () => true },
  { id: "heavy",        label: "Heavy Metal",          match: g => /heavy\s+metal/.test(g) },
  { id: "classic",      label: "Classic Rock",         match: g => /(classic\s+rock|hard\s+rock|blues\s+rock|southern\s+rock|arena\s+rock)/.test(g) },
  { id: "prog-rock",    label: "Progressive Rock",     match: g => /(progressive\s+rock|prog\s+rock|art\s+rock|symphonic\s+prog|crossover\s+prog|neo[-\s]?prog|canterbury|zeuhl|krautrock)/.test(g) },
  { id: "prog-metal",   label: "Progressive Metal",    match: g => /(progressive\s+metal|prog\s+metal|technical\s+death|tech[-\s]?death|djent)/.test(g) },
  { id: "fusion",       label: "Jazz / Fusion",        match: g => /(jazz|fusion)/.test(g) },
  { id: "instrumental", label: "Instrumental / Shred", match: g => /(instrumental|shred|neoclassical|guitar\s+virtuoso|math\s+rock)/.test(g) },
  { id: "death",        label: "Death Metal",          match: g => /(death\s+metal|deathcore)/.test(g) },
  { id: "black",        label: "Black Metal",          match: g => /black\s+metal/.test(g) },
  { id: "doom",         label: "Doom / Sludge",        match: g => /(doom|sludge|stoner|post[-\s]?metal)/.test(g) },
  { id: "thrash",       label: "Thrash",               match: g => /(thrash|speed\s+metal)/.test(g) },
  { id: "power",        label: "Power / Symphonic",    match: g => /(power\s+metal|symphonic\s+metal)/.test(g) },
  { id: "core",         label: "Metal / Deathcore",    match: g => /(metalcore|deathcore|alternative\s+metal|nu[-\s]?metal|hardcore|mathcore)/.test(g) },
  { id: "folk",         label: "Folk / Gothic",        match: g => /(folk\s+metal|gothic\s+metal|viking\s+metal|pagan)/.test(g) },
  { id: "psych",        label: "Psychedelic / Space",  match: g => /(psychedelic|space\s+rock)/.test(g) },
];

// Sources we discover from the journalism pool. Kept in sync with
// scripts/fetch_releases.py JOURNALISM_INDEXES on the backend.
const KNOWN_SOURCES = [
  "Angry Metal Guy", "Blabbermouth", "Classic Rock", "Consequence",
  "Decibel", "Invisible Oranges", "Loudwire", "Louder",
  "Metal Hammer", "Metal Injection", "MetalSucks", "New Noise",
  "Pitchfork", "Prog Magazine", "Sputnikmusic", "Stereogum",
  "The Prog Report",
];

function getDisabledSources() {
  try {
    return new Set(JSON.parse(localStorage.getItem("hp_disabled_sources") || "[]"));
  } catch { return new Set(); }
}
function setDisabledSources(set) {
  localStorage.setItem("hp_disabled_sources", JSON.stringify([...set]));
}

const state = {
  index: null,           // raw index.json
  selectedKey: "this",   // "last" | "this" | "next"
  releases: [],
  shown: INITIAL_SHOW,
  query: "",
  filter: "all",
  generated_at: null,
  window_start: null,
  window_end: null,
};

let loadObserver = null;

const fmtRange = (startISO, endISO) => {
  if (!startISO || !endISO) return "";
  const mo = ["Jan","Feb","Mar","Apr","May","Jun","Jul","Aug","Sep","Oct","Nov","Dec"];
  const s = new Date(startISO + "T00:00:00Z");
  const e = new Date(endISO + "T00:00:00Z");
  return `${mo[s.getUTCMonth()]} ${s.getUTCDate()} – ${mo[e.getUTCMonth()]} ${e.getUTCDate()}, ${e.getUTCFullYear()}`;
};

const fmtDate = iso => {
  if (!iso) return "";
  const d = new Date(iso + "T00:00:00Z");
  return d.toLocaleDateString("en-US", { month: "short", day: "numeric", year: "numeric" });
};

async function loadIndex() {
  try {
    const res = await fetch(INDEX_URL, { cache: "no-store" });
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    state.index = await res.json();
  } catch (e) {
    console.error("Failed to load index:", e);
    state.index = null;
  }
  updateWeekTabs();
}

// "this"  → index.latest
// "last"  → the week immediately before latest
// "next"  → index.next (a future week the fetcher has scoped out, if present)
function resolveWeekKey(key) {
  if (!state.index) return null;
  const weeks = state.index.weeks || [];
  if (key === "next") {
    return state.index.next || null;
  }
  if (key === "this") {
    return state.index.latest || (weeks[0] && weeks[0].week_of) || null;
  }
  if (key === "last") {
    const latestIdx = weeks.findIndex(w => w.week_of === state.index.latest);
    const baseIdx = latestIdx >= 0 ? latestIdx : 0;
    return (weeks[baseIdx + 1] && weeks[baseIdx + 1].week_of) || null;
  }
  return null;
}

async function loadWeek(key) {
  const wk = resolveWeekKey(key);
  if (!wk) {
    state.releases = [];
    state.window_start = null;
    state.window_end = null;
    state.generated_at = null;
    renderHeaderDate();
    renderEyebrow();
    renderGrid();
    return;
  }
  try {
    const res = await fetch(WEEK_URL(wk), { cache: "no-store" });
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const data = await res.json();
    state.releases = data.releases || [];
    state.generated_at = data.generated_at;
    state.window_start = data.window_start;
    state.window_end = data.window_end;
    state.shown = INITIAL_SHOW;
  } catch (e) {
    console.error(`Failed to load week ${wk}:`, e);
    state.releases = [];
  }
  renderHeaderDate();
  renderEyebrow();
  renderGrid();
}

function updateWeekTabs() {
  const tabs = document.querySelectorAll(".week-tab");
  tabs.forEach(t => {
    const key = t.dataset.key;
    const target = resolveWeekKey(key);
    t.classList.toggle("active", key === state.selectedKey);
    t.setAttribute("aria-selected", key === state.selectedKey ? "true" : "false");
    t.disabled = !target;
  });
}

function renderHeaderDate() {
  const el = document.getElementById("header-date");
  if (state.window_start && state.window_end) {
    el.textContent = fmtRange(state.window_start, state.window_end);
  } else {
    el.textContent = "";
  }
}

function renderEyebrow() {
  const text = document.getElementById("eyebrow-text");
  const count = document.getElementById("eyebrow-count");
  const filterLabel = state.filter === "all"
    ? null
    : (FILTER_CATS.find(c => c.id === state.filter)?.label || null);
  const range = fmtRange(state.window_start, state.window_end);
  const parts = [range || "Awaiting fetch"];
  if (filterLabel) parts.push(filterLabel);
  if (state.query) parts.push(`"${state.query}"`);
  text.textContent = parts.join(" · ");
  const total = filteredReleases().length;
  count.textContent = total ? `${total} release${total === 1 ? "" : "s"}` : "";
}

function renderChips() {
  const container = document.getElementById("chips");
  container.innerHTML = "";
  for (const c of FILTER_CATS) {
    const el = document.createElement("button");
    el.className = "filter-pill";
    if (state.filter === c.id) el.classList.add("active");
    el.dataset.id = c.id;
    el.type = "button";
    el.textContent = c.label;
    el.setAttribute("role", "tab");
    el.addEventListener("click", () => {
      state.filter = c.id;
      state.shown = INITIAL_SHOW;
      renderChips();
      renderEyebrow();
      renderGrid();
    });
    container.appendChild(el);
  }
}

function matchesFilter(r) {
  const cat = FILTER_CATS.find(c => c.id === state.filter);
  if (!cat || cat.id === "all") return true;
  const blob = (r.genres || []).join(" ").toLowerCase();
  return cat.match(blob);
}

function matchesQuery(r) {
  if (!state.query) return true;
  const q = state.query.toLowerCase();
  return (
    (r.artist || "").toLowerCase().includes(q) ||
    (r.album || "").toLowerCase().includes(q) ||
    (r.genres || []).some(g => g.toLowerCase().includes(q))
  );
}

function filteredReleases() {
  return state.releases.filter(r => matchesFilter(r) && matchesQuery(r));
}

function buildPlaceholder(r) {
  const ph = document.createElement("div");
  ph.className = "cover-placeholder";
  ph.innerHTML = `
    <div class="ph-artist">${escapeHtml(r.artist || "")}</div>
    <div class="ph-title">${escapeHtml(r.album || "")}</div>
  `;
  return ph;
}

function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, c => (
    { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]
  ));
}

function buildCard(r, idx) {
  const tpl = document.getElementById("card-template");
  const node = tpl.content.cloneNode(true);
  const article = node.querySelector(".album-card");
  article.style.animationDelay = `${Math.min(idx, 11) * 0.04}s`;

  const skeleton = node.querySelector(".cover-skeleton");
  const coverLink = node.querySelector(".cover-link");
  const img = node.querySelector(".cover-img");

  coverLink.href = r.spotify || r.source_url || "#";

  if (r.cover) {
    img.alt = `${r.album} by ${r.artist} cover`;
    img.referrerPolicy = "no-referrer";
    img.addEventListener("load", () => skeleton.remove());
    img.addEventListener("error", () => {
      skeleton.remove();
      coverLink.replaceWith(buildPlaceholder(r));
    });
    img.src = r.cover;
  } else {
    skeleton.remove();
    coverLink.replaceWith(buildPlaceholder(r));
  }

  // Artist link: prefer the band's actual Wikipedia article; otherwise send to
  // English Wikipedia search (the &go=Go param redirects when an exact match exists).
  const artistEl = node.querySelector(".card-artist");
  artistEl.textContent = r.artist || "";
  artistEl.href = r.wikipedia_url
    || `https://en.wikipedia.org/wiki/Special:Search?search=${enc(r.artist || "")}&go=Go`;

  node.querySelector(".card-title").textContent = r.album || "";
  node.querySelector(".card-date").textContent = [
    fmtDate(r.release_date),
    r.album_type && r.album_type.toLowerCase() !== "album" ? r.album_type : null,
  ].filter(Boolean).join(" · ");

  const tagsEl = node.querySelector(".card-tags");
  for (const g of (r.genres || []).slice(0, 4)) {
    const span = document.createElement("span");
    span.className = "tag";
    span.textContent = g;
    tagsEl.appendChild(span);
  }

  node.querySelector(".btn-spotify").href = r.spotify;
  node.querySelector(".btn-yt").href = r.youtube_music;

  // Info button: opens modal listing real articles + the band's Metal Archives page
  // when known. Hidden only when there's literally nothing to show.
  const infoBtn = node.querySelector(".btn-info");
  const articles = Array.isArray(r.articles) ? r.articles : [];
  const maUrl = r.ma_url
    || (r.band_url && r.band_url.startsWith("https://www.metal-archives.com/") ? r.band_url : null);
  if (articles.length > 0 || maUrl) {
    infoBtn.addEventListener("click", () => openInfoModal(r, articles, maUrl));
  } else {
    infoBtn.hidden = true;
  }

  return node;
}

function setLinkOrHide(anchor, url) {
  if (url) {
    anchor.href = url;
    anchor.hidden = false;
  } else {
    anchor.removeAttribute("href");
    anchor.hidden = true;
  }
}

function openInfoModal(r, articles, maUrl) {
  const modal = document.getElementById("reviews-modal");
  document.getElementById("reviews-modal-title").textContent = r.album || "";
  modal.querySelector(".modal-artist").textContent = r.artist || "";
  const list = document.getElementById("reviews-modal-list");
  list.innerHTML = "";

  const disabled = getDisabledSources();
  const visibleArticles = (articles || []).filter(a => !disabled.has(a.source));

  const note = document.getElementById("reviews-modal-note");
  const articleNote = visibleArticles.length === 0
    ? "No journalism coverage matched on enabled sources."
    : `${visibleArticles.length} article${visibleArticles.length === 1 ? "" : "s"} mentioning this release.`;
  note.textContent = articleNote;

  for (const art of visibleArticles) {
    list.appendChild(buildModalRow(art.title, art.url, art.source || domainOf(art.url)));
  }
  // Always append the band's Metal Archives page as a reference entry, separated visually.
  if (maUrl) {
    if (visibleArticles.length > 0) {
      const sep = document.createElement("li");
      sep.className = "modal-list-separator";
      sep.textContent = "Reference";
      list.appendChild(sep);
    }
    list.appendChild(buildModalRow(
      `${r.artist} on Metal Archives`,
      maUrl,
      "metal-archives.com"
    ));
  }

  modal.hidden = false;
  modal.setAttribute("aria-hidden", "false");
  document.body.style.overflow = "hidden";
}

function buildModalRow(title, url, source) {
  const li = document.createElement("li");
  const a = document.createElement("a");
  a.href = url;
  a.target = "_blank";
  a.rel = "noopener";
  const t = document.createElement("span");
  t.className = "modal-list-title";
  t.textContent = title;
  const m = document.createElement("span");
  m.className = "modal-list-meta";
  m.textContent = source;
  a.appendChild(t);
  a.appendChild(m);
  li.appendChild(a);
  return li;
}

function closeModal(modal) {
  modal.hidden = true;
  modal.setAttribute("aria-hidden", "true");
  if (![...document.querySelectorAll(".modal")].some(m => !m.hidden)) {
    document.body.style.overflow = "";
  }
}

function openSourcesModal() {
  const modal = document.getElementById("sources-modal");
  const list = document.getElementById("sources-checklist");
  list.innerHTML = "";
  const disabled = getDisabledSources();
  for (const src of KNOWN_SOURCES) {
    const li = document.createElement("li");
    const label = document.createElement("label");
    const cb = document.createElement("input");
    cb.type = "checkbox";
    cb.checked = !disabled.has(src);
    cb.addEventListener("change", () => {
      const cur = getDisabledSources();
      if (cb.checked) cur.delete(src);
      else cur.add(src);
      setDisabledSources(cur);
    });
    label.appendChild(cb);
    label.appendChild(document.createTextNode(src));
    li.appendChild(label);
    list.appendChild(li);
  }
  modal.hidden = false;
  modal.setAttribute("aria-hidden", "false");
  document.body.style.overflow = "hidden";
}

function renderGrid() {
  const grid = document.getElementById("grid");
  grid.innerHTML = "";
  if (loadObserver) loadObserver.disconnect();

  const items = filteredReleases();

  if (items.length === 0) {
    const empty = document.createElement("div");
    empty.className = "empty-wrap";
    if (state.releases.length === 0) {
      empty.innerHTML = `
        <h2 class="empty-title">Nothing Yet</h2>
        <p class="empty-msg">The weekly fetch hasn't run yet. New releases land every Friday.</p>`;
    } else {
      empty.innerHTML = `
        <h2 class="empty-title">No Matches</h2>
        <p class="empty-msg">Try a different filter or clear the search.</p>`;
    }
    grid.appendChild(empty);
    document.getElementById("load-sentinel").hidden = true;
    document.getElementById("load-more-spinner").hidden = true;
    return;
  }

  const visible = items.slice(0, state.shown);
  visible.forEach((r, i) => grid.appendChild(buildCard(r, i)));

  const sentinel = document.getElementById("load-sentinel");
  const spinner = document.getElementById("load-more-spinner");
  if (state.shown < items.length) {
    sentinel.hidden = false;
    spinner.hidden = false;
    loadObserver = new IntersectionObserver(entries => {
      if (entries.some(e => e.isIntersecting)) {
        state.shown = Math.min(state.shown + PAGE_SIZE, items.length);
        renderGrid();
      }
    }, { rootMargin: "200px" });
    loadObserver.observe(sentinel);
  } else {
    sentinel.hidden = true;
    spinner.hidden = true;
  }
}

// ── Event wiring ──
document.querySelectorAll(".week-tab").forEach(tab => {
  tab.addEventListener("click", () => {
    const key = tab.dataset.key;
    if (tab.disabled || key === state.selectedKey) return;
    state.selectedKey = key;
    state.shown = INITIAL_SHOW;
    state.query = "";
    state.filter = "all";
    document.getElementById("filter").value = "";
    document.getElementById("search-clear").hidden = true;
    updateWeekTabs();
    renderChips();
    loadWeek(key);
  });
});

// Modal close handlers (works for both reviews-modal and sources-modal).
document.querySelectorAll("[data-modal-close]").forEach(el => {
  el.addEventListener("click", () => {
    const modal = el.closest(".modal");
    if (modal) closeModal(modal);
  });
});
document.addEventListener("keydown", e => {
  if (e.key !== "Escape") return;
  for (const modal of document.querySelectorAll(".modal")) {
    if (!modal.hidden) closeModal(modal);
  }
});

// Footer link → open sources modal
const sourcesLink = document.getElementById("open-sources-modal");
if (sourcesLink) {
  sourcesLink.addEventListener("click", e => {
    e.preventDefault();
    openSourcesModal();
  });
}
const sourcesAll = document.getElementById("sources-all");
if (sourcesAll) {
  sourcesAll.addEventListener("click", () => {
    setDisabledSources(new Set());
    openSourcesModal();
  });
}
const sourcesNone = document.getElementById("sources-none");
if (sourcesNone) {
  sourcesNone.addEventListener("click", () => {
    setDisabledSources(new Set(KNOWN_SOURCES));
    openSourcesModal();
  });
}

const searchInput = document.getElementById("filter");
const searchClear = document.getElementById("search-clear");
searchInput.addEventListener("input", e => {
  state.query = e.target.value.trim();
  state.shown = INITIAL_SHOW;
  searchClear.hidden = !state.query;
  renderEyebrow();
  renderGrid();
});
searchClear.addEventListener("click", () => {
  searchInput.value = "";
  state.query = "";
  searchClear.hidden = true;
  renderEyebrow();
  renderGrid();
  searchInput.focus();
});

// ── Init ──
(async function init() {
  await loadIndex();
  await loadWeek(state.selectedKey);
  renderChips();
})();
