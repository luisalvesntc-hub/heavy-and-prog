const INDEX_URL = "data/index.json";
const WEEK_URL = wk => `data/weeks/${wk}.json`;
const INITIAL_SHOW = 12;
const PAGE_SIZE = 12;
const SCORE_BADGE_THRESHOLD = 80;

const FILTER_CATS = [
  { id: "all",          label: "All",                  match: () => true },
  { id: "progressive",  label: "Progressive",          match: g => /(progressive|prog|djent|art\s+rock|symphonic\s+prog|crossover\s+prog|neo[-\s]?prog|canterbury|zeuhl|krautrock)/.test(g) },
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

const state = {
  weeks: [],
  weekOffset: 0,
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
    const idx = await res.json();
    state.weeks = idx.weeks || [];
  } catch (e) {
    console.error("Failed to load index:", e);
    state.weeks = [];
  }
  updateWeekTabs();
}

async function loadWeek(offset) {
  const wk = state.weeks[offset];
  if (!wk) {
    state.releases = [];
    state.window_start = null;
    state.window_end = null;
    state.generated_at = null;
    renderEyebrow();
    renderGrid();
    return;
  }
  try {
    const res = await fetch(WEEK_URL(wk.week_of), { cache: "no-store" });
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const data = await res.json();
    state.releases = data.releases || [];
    state.generated_at = data.generated_at;
    state.window_start = data.window_start;
    state.window_end = data.window_end;
    state.shown = INITIAL_SHOW;
  } catch (e) {
    console.error(`Failed to load week ${wk.week_of}:`, e);
    state.releases = [];
  }
  renderHeaderDate();
  renderEyebrow();
  renderGrid();
}

function updateWeekTabs() {
  const tabs = document.querySelectorAll(".week-tab");
  tabs.forEach(t => {
    const offset = parseInt(t.dataset.offset, 10);
    t.classList.toggle("active", offset === state.weekOffset);
    t.setAttribute("aria-selected", offset === state.weekOffset ? "true" : "false");
    t.disabled = offset >= state.weeks.length;
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

  const coverWrap = node.querySelector(".cover-wrap");
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

  // Score badge — show the headline number when ≥ threshold.
  if (typeof r.score === "number" && r.score >= SCORE_BADGE_THRESHOLD) {
    const badge = node.querySelector(".score-badge");
    badge.hidden = false;
    badge.querySelector(".score-num").textContent = Math.round(r.score);
  }

  node.querySelector(".card-artist").textContent = r.artist || "";
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
  // Reviews button → AOTY search (most useful single review aggregator).
  node.querySelector(".btn-reviews").href = (r.reviews && r.reviews.aoty) || "#";

  // MA (Metal Archives) button → band page if we have one.
  const maBtn = node.querySelector(".btn-ma");
  if (r.band_url && r.band_url.startsWith("https://www.metal-archives.com/")) {
    maBtn.href = r.band_url;
    maBtn.hidden = false;
  }

  return node;
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
    const offset = parseInt(tab.dataset.offset, 10);
    if (offset === state.weekOffset || offset >= state.weeks.length) return;
    state.weekOffset = offset;
    state.shown = INITIAL_SHOW;
    state.query = "";
    state.filter = "all";
    document.getElementById("filter").value = "";
    document.getElementById("search-clear").hidden = true;
    updateWeekTabs();
    renderChips();
    loadWeek(offset);
  });
});

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
  await loadWeek(state.weekOffset);
  renderChips();
})();
