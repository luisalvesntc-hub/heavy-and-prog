const DATA_URL = "data/releases.json";

const SUBGENRE_BUCKETS = [
  { id: "all", label: "All", match: () => true },
  { id: "metal", label: "Metal", match: g => /metal/.test(g) && !/post-metal/.test(g) },
  { id: "prog", label: "Progressive", match: g => /prog/.test(g) },
  { id: "doom", label: "Doom / Sludge / Stoner", match: g => /(doom|sludge|stoner)/.test(g) },
  { id: "post", label: "Post-rock / Post-metal", match: g => /post-(rock|metal)/.test(g) },
  { id: "extreme", label: "Black / Death / Grind", match: g => /(black metal|death metal|grindcore)/.test(g) },
  { id: "core", label: "Core", match: g => /(metalcore|deathcore|hardcore|mathcore)/.test(g) },
];

const DIGEST_SIZE = 12;
const PAGE_SIZE = 12;

const state = { releases: [], query: "", bucket: "all", visibleCount: DIGEST_SIZE };

let loadObserver = null;

const fmtDate = iso => {
  if (!iso) return "";
  return new Date(iso + "T00:00:00Z").toLocaleDateString(undefined,
    { year: "numeric", month: "short", day: "numeric" });
};

const fmtDateTime = iso => {
  if (!iso) return "";
  return new Date(iso).toLocaleString(undefined, { dateStyle: "medium", timeStyle: "short" });
};

function applyData(data) {
  state.releases = data.releases || [];
  document.getElementById("week-label").textContent =
    data.week_of ? `Week of ${fmtDate(data.week_of)}` : "Awaiting first fetch";
  document.getElementById("generated-at").textContent =
    data.generated_at ? `Updated ${fmtDateTime(data.generated_at)}` : "";
}

async function load() {
  // Prefer fresh JSON over HTTP so reloads pick up new fetcher output. Fall back to the
  // inline global (data/releases.js) when the page is opened directly (file:// or fetch
  // is blocked).
  try {
    const res = await fetch(DATA_URL, { cache: "no-store" });
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    applyData(await res.json());
  } catch (e) {
    if (window.RELEASES_DATA) {
      applyData(window.RELEASES_DATA);
    } else {
      console.error("Failed to load releases:", e);
      document.getElementById("week-label").textContent = "Failed to load releases";
    }
  }
  renderChips();
  render();
}

function renderChips() {
  const container = document.getElementById("chips");
  container.innerHTML = "";
  for (const b of SUBGENRE_BUCKETS) {
    const el = document.createElement("button");
    el.className = "chip" + (state.bucket === b.id ? " active" : "");
    el.textContent = b.label;
    el.addEventListener("click", () => {
      state.bucket = b.id;
      state.visibleCount = DIGEST_SIZE;
      renderChips();
      render();
    });
    container.appendChild(el);
  }
}

function matchesBucket(release) {
  const bucket = SUBGENRE_BUCKETS.find(b => b.id === state.bucket);
  if (!bucket || bucket.id === "all") return true;
  const blob = (release.genres || []).join(" ").toLowerCase();
  return bucket.match(blob);
}

function matchesQuery(release) {
  if (!state.query) return true;
  const q = state.query.toLowerCase();
  return (
    release.artist.toLowerCase().includes(q) ||
    release.album.toLowerCase().includes(q) ||
    (release.genres || []).some(g => g.toLowerCase().includes(q))
  );
}

function placeholderFor(release) {
  const seed = (release.artist + release.album).toLowerCase();
  let hash = 0;
  for (let i = 0; i < seed.length; i++) hash = (hash * 31 + seed.charCodeAt(i)) | 0;
  const hue = Math.abs(hash) % 360;
  const initial = (release.artist || "?").trim().charAt(0).toUpperCase();
  const div = document.createElement("div");
  div.className = "cover placeholder";
  div.style.background =
    `linear-gradient(135deg, hsl(${hue} 35% 22%), hsl(${(hue + 40) % 360} 50% 12%))`;
  div.innerHTML = `<span>${initial || "?"}</span>`;
  return div;
}

function buildCard(r) {
  const tpl = document.getElementById("card-template");
  const node = tpl.content.cloneNode(true);
  const coverLink = node.querySelector(".cover-link");
  const img = node.querySelector(".cover");

  coverLink.href = r.spotify || r.source_url || "#";

  if (r.cover) {
    img.src = r.cover;
    img.alt = `${r.album} by ${r.artist}`;
    img.referrerPolicy = "no-referrer";
    img.addEventListener("error", () => img.replaceWith(placeholderFor(r)));
  } else {
    img.replaceWith(placeholderFor(r));
  }

  node.querySelector(".album-title").textContent = r.album;
  node.querySelector(".artist").textContent = r.artist;

  const meta = [fmtDate(r.release_date), r.album_type || "album"];
  node.querySelector(".release-date").textContent = meta.filter(Boolean).join(" · ");

  const genresEl = node.querySelector(".genres");
  for (const g of (r.genres || []).slice(0, 4)) {
    const li = document.createElement("li");
    li.textContent = g;
    genresEl.appendChild(li);
  }

  node.querySelector(".btn-spotify").href = r.spotify;
  node.querySelector(".btn-yt").href = r.youtube_music;
  node.querySelector(".btn-bc").href = r.bandcamp;

  const rev = r.reviews || {};
  node.querySelector(".rev-aoty").href = rev.aoty || "#";
  node.querySelector(".rev-mc").href = rev.metacritic || "#";
  node.querySelector(".rev-sp").href = rev.sputnik || "#";
  node.querySelector(".rev-rym").href = rev.rym || "#";

  const sourcesEl = node.querySelector(".sources");
  for (const s of (r.sources || [])) {
    const span = document.createElement("span");
    span.className = "source-tag";
    span.textContent = s === "metal-archives" ? "MA" : s === "wikipedia" ? "Wiki" : s;
    sourcesEl.appendChild(span);
  }

  return node;
}

function sectionHeader(title, subtitle) {
  const h = document.createElement("div");
  h.className = "section-header";
  h.innerHTML = `<h2>${title}</h2>${subtitle ? `<span>${subtitle}</span>` : ""}`;
  return h;
}

function render() {
  const grid = document.getElementById("grid");
  grid.innerHTML = "";
  if (loadObserver) loadObserver.disconnect();

  const filtered = state.releases.filter(r => matchesBucket(r) && matchesQuery(r));

  if (filtered.length === 0) {
    const empty = document.createElement("div");
    empty.className = "empty";
    if (state.releases.length === 0) {
      empty.innerHTML = `
        <h3>No releases yet</h3>
        <p>The weekly fetch hasn't run yet. Once the GitHub Action runs on Friday,
        new releases will appear here.</p>`;
    } else {
      empty.innerHTML = `<h3>Nothing matches</h3><p>Try a different filter or clear the search.</p>`;
    }
    grid.appendChild(empty);
    return;
  }

  const visible = filtered.slice(0, state.visibleCount);

  visible.forEach((r, i) => {
    if (i === 0) {
      grid.appendChild(sectionHeader("Top picks this week",
        `${Math.min(DIGEST_SIZE, filtered.length)} highest-rated releases`));
    } else if (i === DIGEST_SIZE && filtered.length > DIGEST_SIZE) {
      grid.appendChild(sectionHeader("More from this week",
        `${filtered.length - DIGEST_SIZE} more`));
    }
    grid.appendChild(buildCard(r));
  });

  if (state.visibleCount < filtered.length) {
    const sentinel = document.createElement("div");
    sentinel.className = "load-sentinel";
    sentinel.textContent = "Loading more…";
    grid.appendChild(sentinel);
    loadObserver = new IntersectionObserver(entries => {
      if (entries.some(e => e.isIntersecting)) {
        state.visibleCount = Math.min(state.visibleCount + PAGE_SIZE, filtered.length);
        render();
      }
    }, { rootMargin: "200px" });
    loadObserver.observe(sentinel);
  }
}

document.getElementById("filter").addEventListener("input", e => {
  state.query = e.target.value.trim();
  state.visibleCount = DIGEST_SIZE;
  render();
});

load();
