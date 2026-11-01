// Progressive enhancement: every action also works as a plain form post.

const store = {
  get(key, fallback) { try { return JSON.parse(localStorage.getItem(key)) ?? fallback; } catch { return fallback; } },
  set(key, value) { try { localStorage.setItem(key, JSON.stringify(value)); } catch { /* private mode */ } },
};

// Settings saved before the app was renamed live under "radar.*": move them across once
try {
  for (const key of ["filters", "catsOpen", "bannerOff"]) {
    const old = localStorage.getItem(`radar.${key}`);
    if (old !== null && localStorage.getItem(`newsnest.${key}`) === null) localStorage.setItem(`newsnest.${key}`, old);
    localStorage.removeItem(`radar.${key}`);
  }
} catch { /* storage blocked */ }

async function postForm(url, data) {
  const body = new FormData();
  for (const [k, v] of Object.entries(data)) body.set(k, v);
  const response = await fetch(url, { method: "POST", body, headers: { Accept: "application/json" } });
  if (!response.ok) throw new Error(response.statusText);
  return response.json();
}

// ---------- read state ----------
function setStoryRead(story, read) {
  story.dataset.read = read ? "1" : "0";
  story.classList.toggle("read", read);
  const button = story.querySelector(".readtoggle");
  if (button) {
    button.setAttribute("aria-pressed", String(read));
    button.value = read ? "0" : "1";
    const label = read ? "Mark as unread" : "Mark as read";
    button.setAttribute("aria-label", label);
    button.title = label;
  }
}

// ---------- saved for later ----------
function setSaved(story, saved) {
  const button = story.querySelector(".savetoggle");
  if (!button) return;
  button.setAttribute("aria-pressed", String(saved));
  const label = saved ? "Remove from saved" : "Save for later";
  button.setAttribute("aria-label", label);
  button.title = label;
}

// On the Saved page a removed card leaves the list, with an Undo in its place
function removeSavedCard(story, ids) {
  story.hidden = true;
  const bar = document.createElement("div");
  bar.className = "hiddenbar";
  bar.innerHTML = '<span>Removed from saved.</span><button type="button" class="button small secondary">Undo</button>';
  story.after(bar);
  bar.querySelector("button").addEventListener("click", async () => {
    try {
      await postForm("/api/saved", { ids, value: "1" });
      setSaved(story, true);
      story.hidden = false;
      bar.remove();
    } catch { toast("That did not work; try again"); }
  });
}

document.addEventListener("click", async (event) => {
  const button = event.target.closest(".act.savetoggle");
  if (!button) return;
  const save = button.getAttribute("aria-pressed") !== "true";
  const story = button.closest(".story");
  const ids = button.dataset.ids;
  setSaved(story, save);  // instant; the request follows
  try {
    await postForm("/api/saved", { ids, value: save ? "1" : "0" });
    if (!save && story.closest("[data-saved-list]")) removeSavedCard(story, ids);
    else toast(save ? "Saved for later" : "Removed from saved");
  } catch {
    setSaved(story, !save);
    toast("That did not work; try again");
  }
});

// ---------- settings: a section switch saves as soon as it is tapped ----------
document.addEventListener("change", (event) => {
  const form = event.target.closest("form[data-autosubmit]");
  if (!form) return;
  form.classList.add("saving");
  form.requestSubmit();
});

// The onboarding region form sends the browser's time zone, so nobody has to pick one from a list
document.addEventListener("DOMContentLoaded", () => {
  for (const input of document.querySelectorAll("input[data-browser-timezone]")) {
    try { input.value = Intl.DateTimeFormat().resolvedOptions().timeZone || ""; } catch { /* leave it empty */ }
  }
});

// Settings → General saves in place: no reload, a toast says whether it worked
document.addEventListener("change", async (event) => {
  const form = event.target.closest("form[data-autosave]");
  if (!form) return;
  if (!form.checkValidity()) { form.reportValidity(); return; }
  const list = event.target.closest(".slist");
  list?.classList.add("saving");
  try {
    const response = await fetch(form.action, { method: "POST", body: new FormData(form) });
    const error = new URL(response.url).searchParams.get("error");
    if (!response.ok || error) throw new Error(error || response.statusText);
    toast("Saved");
  } catch (e) {
    toast(e.message ? `Not saved: ${e.message}` : "Not saved; try again");
  } finally {
    list?.classList.remove("saving");
  }
});

// After a settings change the page reloads with ?saved=…: say so in a toast, then drop it from the address
document.addEventListener("DOMContentLoaded", () => {
  const page = document.querySelector(".settings[data-toast]");
  if (!page) return;
  toast(page.dataset.toast);
  const url = new URL(location.href);
  url.searchParams.delete("saved");
  history.replaceState(null, "", url);
});

// ---------- forms: rating, read toggle, mark all read ----------
document.addEventListener("submit", async (event) => {
  const form = event.target;
  if (form.dataset.confirm && !window.confirm(form.dataset.confirm)) {
    event.preventDefault();
    return;
  }
  const button = event.submitter;
  if (!button) return;

  if (form.dataset.checking) {
    // The server fetches the feed before saving: say so, and don't let a second tap start another check
    const label = button.textContent;
    setTimeout(() => { button.disabled = true; button.textContent = form.dataset.checking; }, 0);
    addEventListener("pageshow", () => { button.disabled = false; button.textContent = label; }, { once: true });
  } else if (form.classList.contains("rate")) {
    event.preventDefault();
    try {
      const { rating } = await postForm(form.action, { value: button.value });
      const up = form.querySelector(".up");
      const down = form.querySelector(".down");
      up.setAttribute("aria-pressed", String(rating === 1));
      down.setAttribute("aria-pressed", String(rating === -1));
      up.value = rating === 1 ? "0" : "1";
      down.value = rating === -1 ? "0" : "-1";
      updateOnboardingCount();
    } catch { form.submit(); }
  } else if (form.classList.contains("readform")) {
    event.preventDefault();
    const read = button.value === "1";
    try {
      await postForm(form.action, { ids: form.elements.ids.value, value: button.value });
      setStoryRead(form.closest(".story"), read);
      applyFilters();
    } catch { form.submit(); }
  } else if (form.classList.contains("readall")) {
    event.preventDefault();
    try {
      await postForm(form.action, { ...Object.fromEntries(new FormData(form)), value: "1" });
      document.querySelectorAll(".story").forEach((s) => setStoryRead(s, true));
      applyFilters();
    } catch { form.submit(); }
  }
});

// ---------- key points on demand: the server reads the article (a few seconds) and returns the block ----------
function sumNote(form, message) {
  let note = form.querySelector(".sumnote");
  if (!note) {
    note = document.createElement("p");
    note.className = "note sumnote";
    note.setAttribute("role", "status");
    form.append(note);
  }
  note.textContent = message;
}

document.addEventListener("click", (event) => {
  const button = event.target.closest(".sumbtn");
  if (button) summarize(button.closest(".sumform"), button);
});

async function summarize(form, button) {
  if (button.disabled) return;
  const label = button.querySelector("span");
  const idle = label.textContent;
  button.disabled = true;
  button.setAttribute("aria-busy", "true");
  label.textContent = "Reading the article…";
  form.querySelector(".sumnote")?.remove();
  try {
    const result = await postForm(`/api/items/${button.dataset.summarize}/summarize`, { lines: button.dataset.lines });
    if (result.html) {
      const body = form.closest(".bodytext");
      body.querySelector(".snippet")?.remove();
      form.outerHTML = result.html;
      body.classList.add("fresh");
      body.querySelector(".tldr summary")?.focus({ preventScroll: true });
      return;
    }
    sumNote(form, result.message || "Could not write key points");
    if (result.status === "no_text") { button.remove(); return; }
  } catch {
    sumNote(form, "Could not reach NewsNest. Try again.");
  }
  button.disabled = false;
  button.removeAttribute("aria-busy");
  label.textContent = idle;
}

// Opening a story marks it read on the server (/go/<id>); reflect it right away.
document.addEventListener("click", (event) => {
  const link = event.target.closest(".story h3 a, .thumb-img, .hero-media, .big-media, .story .openlink");
  if (link) {
    setStoryRead(link.closest(".story"), true);
    setTimeout(applyFilters, 400);  // let the new tab open before the card disappears
  }
});

// ---------- filter chips ----------
const FILTER_KEY = "newsnest.filters";

function applyFilters() {
  const toolbar = document.getElementById("toolbar");
  if (!toolbar || !toolbar.querySelector("button.chip[data-block]")) return;  // only the archive filters in-page
  const state = store.get(FILTER_KEY, { block: "all", unread: false });
  // A chip for a block that does not exist today falls back to All
  if (state.block !== "all" && !toolbar.querySelector(`.chip[data-block="${state.block}"]`)) state.block = "all";

  toolbar.querySelectorAll(".chip[data-block]").forEach((chip) =>
    chip.setAttribute("aria-pressed", String(chip.dataset.block === state.block)));
  toolbar.querySelector(".chip[data-unread]").setAttribute("aria-pressed", String(state.unread));

  let visible = 0;
  document.querySelectorAll("[data-block-section]").forEach((section) => {
    const blocks = section.dataset.blockSection.split(" ");
    const inBlock = state.block === "all" || blocks.includes(state.block);
    section.hidden = !inBlock;
    if (!inBlock) return;
    section.querySelectorAll(".story").forEach((story) => {
      const hide = state.unread && story.dataset.read === "1";
      story.hidden = hide;
      if (!hide) visible += 1;
    });
    // Hide category groups (and whole blocks) that the unread filter emptied
    section.querySelectorAll(".group").forEach((group) => {
      if (group.querySelector(".story")) group.hidden = !group.querySelector(".story:not([hidden])");
    });
    if (section.querySelector(".story") && state.unread) {
      section.hidden = !section.querySelector(".story:not([hidden])");
    }
  });
  const empty = document.querySelector(".filter-empty");
  if (empty) empty.hidden = visible > 0;
}

document.addEventListener("click", (event) => {
  const chip = event.target.closest("button.chip");
  if (!chip) return;
  const state = store.get(FILTER_KEY, { block: "all", unread: false });
  if (chip.hasAttribute("data-unread")) state.unread = !state.unread;
  else state.block = chip.dataset.block;
  store.set(FILTER_KEY, state);
  applyFilters();
  if (!chip.hasAttribute("data-unread")) {
    document.getElementById("toolbar").scrollIntoView({ block: "start", behavior: "smooth" });
  }
});

// ---------- impressions: a story on screen for a second counts as seen (feed page only) ----------
const seenQueue = new Set();
function flushSeen() {
  if (!seenQueue.size) return;
  const body = new FormData();
  body.set("ids", [...seenQueue].join(","));
  seenQueue.clear();
  if (!navigator.sendBeacon("/api/seen", body)) fetch("/api/seen", { method: "POST", body, keepalive: true });
}
// Auto-read (Settings → General): a story that was really on screen and is then scrolled past
// (it leaves through the top) is marked read. Not counted as "opened".
const autoRead = document.body.dataset.autoRead === "1";
const viewedOnce = new WeakSet();
const readQueue = new Set();
function flushRead() {
  if (!readQueue.size) return;
  const body = new FormData();
  body.set("ids", [...readQueue].join(","));
  body.set("value", "1");
  readQueue.clear();
  if (!navigator.sendBeacon("/api/read", body)) fetch("/api/read", { method: "POST", body, keepalive: true });
}

let trackStories = () => {};
if ("IntersectionObserver" in window) {
  const passObserver = autoRead && new IntersectionObserver((entries) => {
    for (const entry of entries) {
      const story = entry.target;
      const scrolledPast = !entry.isIntersecting && entry.boundingClientRect.bottom <= (entry.rootBounds?.top ?? 0);
      if (scrolledPast && viewedOnce.has(story) && story.dataset.read !== "1") {
        story.dataset.ids.split(",").forEach((id) => readQueue.add(id));
        setStoryRead(story, true);
        passObserver.unobserve(story);
      }
    }
  });
  if (autoRead) {
    setInterval(flushRead, 3000);
    addEventListener("pagehide", flushRead);
  }

  const timers = new Map();
  const observer = new IntersectionObserver((entries) => {
    for (const entry of entries) {
      const story = entry.target;
      if (entry.isIntersecting) {
        timers.set(story, setTimeout(() => {
          story.dataset.ids.split(",").forEach((id) => seenQueue.add(id));
          viewedOnce.add(story);
          if (passObserver) passObserver.observe(story);
          observer.unobserve(story);  // once per page load; the server also throttles
        }, 1000));
      } else {
        clearTimeout(timers.get(story));
      }
    }
  }, { threshold: 0.6 });
  trackStories = (stories) => stories.forEach((s) => observer.observe(s));
  document.addEventListener("DOMContentLoaded", () => trackStories(document.querySelectorAll("[data-track-seen] .story")));
  setInterval(flushSeen, 3000);
  addEventListener("pagehide", flushSeen);
}

// ---------- card menu: more like this, not interested (with undo), less/more of outlet or category ----------
function toast(message) {
  const el = document.querySelector(".toast");
  if (!el) return;
  el.textContent = message;
  el.hidden = false;
  clearTimeout(el._timer);
  el._timer = setTimeout(() => { el.hidden = true; }, 2600);
}

// One shared sheet for every card's ⋯ button (a bottom sheet on phones, a popover on desktop)
const sheet = document.getElementById("cardmenu");
const backdrop = document.querySelector(".sheet-backdrop");
let menuButton = null;

function closeSheet() {
  if (!sheet || sheet.hidden) return;
  sheet.hidden = true;
  backdrop.hidden = true;
  menuButton?.setAttribute("aria-expanded", "false");
  menuButton?.focus({ preventScroll: true });
}

function openSheet(button) {
  menuButton = button;
  const d = button.dataset;
  sheet.querySelectorAll("[data-fill]").forEach((el) => { el.textContent = d[el.dataset.fill] || ""; });
  sheet.querySelectorAll("[data-needs-cat]").forEach((el) => { el.hidden = !d.cat; });
  sheet.hidden = false;
  backdrop.hidden = false;
  button.setAttribute("aria-expanded", "true");
  if (window.matchMedia("(min-width: 761px)").matches) {
    const r = button.getBoundingClientRect();
    const top = r.top - sheet.offsetHeight - 8 > 8 ? r.top - sheet.offsetHeight - 8 : r.bottom + 8;
    sheet.style.top = `${top}px`;
    sheet.style.left = `${Math.max(8, r.right - sheet.offsetWidth)}px`;
  } else {
    sheet.style.top = sheet.style.left = "";
  }
  sheet.querySelector(".pop-item:not([hidden])").focus({ preventScroll: true });
}

document.addEventListener("click", async (event) => {
  const kebab = event.target.closest(".kebab-btn");
  if (kebab) { openSheet(kebab); return; }
  if (event.target === backdrop) { closeSheet(); return; }
  const item = event.target.closest("#cardmenu .pop-item");
  if (!item || !menuButton) return;
  const d = menuButton.dataset;
  const action = item.dataset.action;
  closeSheet();
  try {
    const target = action.endsWith("_source") ? d.src : d.cat;
    await postForm("/api/feedback", { action, target });
    const label = action.endsWith("_source") ? d.srcname : d.cat;
    toast(`${action.startsWith("less") ? "Showing less" : "Showing more"} ${action.endsWith("_source") ? "from" : "about"} ${label}`);
  } catch { toast("That did not work; try again"); }
});
document.addEventListener("keydown", (event) => { if (event.key === "Escape") closeSheet(); });

// ---------- one-tap like / dislike on every card ----------
function hideStory(story, ids) {
  story.hidden = true;
  const bar = document.createElement("div");
  bar.className = "hiddenbar";
  bar.innerHTML = '<span>Hidden. You will see fewer stories like this.</span><button type="button" class="button small secondary">Undo</button>';
  story.after(bar);
  bar.querySelector("button").addEventListener("click", async () => {
    await postForm("/api/feedback", { action: "undo_not_interested", ids });
    story.hidden = false;
    bar.remove();
  });
}

document.addEventListener("click", async (event) => {
  const like = event.target.closest(".act.like");
  const dislike = event.target.closest(".act.dislike");
  if (!like && !dislike) return;
  try {
    if (like) {
      const next = like.getAttribute("aria-pressed") === "true" ? "0" : "1";
      like.setAttribute("aria-pressed", String(next === "1"));  // instant; the request follows
      const { rating } = await postForm(`/api/items/${like.dataset.lead}/rate`, { value: next });
      like.setAttribute("aria-pressed", String(rating === 1));
      toast(rating === 1 ? "More like this" : "Removed");
    } else {
      const story = dislike.closest(".story");
      hideStory(story, dislike.dataset.ids);
      await postForm("/api/feedback", { action: "not_interested", ids: dislike.dataset.ids });
    }
  } catch { toast("That did not work; try again"); }
});

// ---------- infinite scroll: the next 20 stories load before you reach the end ----------
let loadingMore = false;
async function loadMore(link) {
  if (loadingMore || !link.isConnected) return;
  loadingMore = true;
  link.classList.add("loading");
  link.textContent = "Loading…";
  try {
    const response = await fetch(link.dataset.fragment, { headers: { Accept: "text/html" } });
    if (!response.ok) throw new Error(response.statusText);
    const tpl = document.createElement("template");
    tpl.innerHTML = await response.text();
    // Skip stories already on the page (only possible when the frozen ranking expired)
    tpl.content.querySelectorAll(".story").forEach((s) => { if (document.getElementById(s.id)) s.remove(); });
    const fresh = document.createElement("div");
    fresh.append(tpl.content);
    const nodes = [...fresh.childNodes];
    link.replaceWith(...nodes);
    trackStories(nodes.filter((n) => n.nodeType === 1 && n.matches(".story")));
    watchLoadMore();
  } catch {
    link.classList.remove("loading");
    link.textContent = "Show more stories";
  } finally {
    loadingMore = false;
  }
}
const moreObserver = "IntersectionObserver" in window &&
  new IntersectionObserver((entries) => entries.forEach((e) => { if (e.isIntersecting) loadMore(e.target); }),
                           { rootMargin: "900px 0px" });
function watchLoadMore() {
  const link = document.querySelector(".loadmore[data-fragment]");
  if (link && moreObserver) moreObserver.observe(link);
}
document.addEventListener("click", (event) => {
  const link = event.target.closest(".loadmore[data-fragment]");
  if (link) { event.preventDefault(); loadMore(link); }
});
document.addEventListener("DOMContentLoaded", watchLoadMore);

// ---------- Home categories: More folds the long tail, a tap swaps the feed in place (no reload) ----------
const CATS_OPEN = "newsnest.catsOpen";

function setupCats(scrollLeft = 0) {
  const nav = document.querySelector(".cats");
  if (!nav) return;
  const more = nav.querySelector(".cats-more");
  if (more) {
    const open = store.get(CATS_OPEN, false);
    nav.classList.add("folded");
    nav.classList.toggle("open", open);
    more.hidden = false;
    more.setAttribute("aria-expanded", String(open));
    more.innerHTML = open ? "Less" : `More <span>${more.dataset.count}</span>`;
  }
  // Keep the row where it was, and bring the selected chip into view if it sits off-screen.
  // (Measured by hand: scrollIntoView can also scroll the page in Safari.)
  nav.scrollLeft = scrollLeft;
  const active = nav.querySelector('[aria-current="true"]');
  if (active) {
    const n = nav.getBoundingClientRect();
    const a = active.getBoundingClientRect();
    if (a.left < n.left + 16 || a.right > n.right - 24) nav.scrollLeft += a.left - n.left - 16;
  }
}

// ---------- Problem banner: ✕ hides it until the problems change, or for 12 hours ----------
// The red dot on ⋯ and the Status page keep showing them, so nothing is lost, it just stops shouting.
const BANNER_OFF = "newsnest.bannerOff";
const BANNER_HOURS = 12;
function applyBanner() {
  let off = null;
  try { off = JSON.parse(localStorage.getItem(BANNER_OFF) || "null"); } catch { /* storage blocked */ }
  document.querySelectorAll(".banner[data-banner]").forEach((el) => {
    el.hidden = !!off && off.key === el.dataset.banner && Date.now() - off.at < BANNER_HOURS * 3600 * 1000;
  });
}
document.addEventListener("click", (event) => {
  const close = event.target.closest(".banner-close");
  if (!close) return;
  const banner = close.closest(".banner");
  try { localStorage.setItem(BANNER_OFF, JSON.stringify({ key: banner.dataset.banner, at: Date.now() })); } catch { /* ignore */ }
  banner.hidden = true;
});
applyBanner();

// ---------- News tab: coming back to it from another tab reopens the chip I was on ----------
// Session only (it dies with the browser tab), so a fresh open always starts on All.
const NEWS_CHIP = "newsnest.newsChip";
function rememberNewsChip() {
  if (document.body.dataset.tab !== "headlines") return;
  const href = document.querySelector('.cats [aria-current="true"]')?.getAttribute("href");
  try { if (href) sessionStorage.setItem(NEWS_CHIP, href); } catch { /* storage blocked: no memory */ }
}
document.addEventListener("click", (event) => {
  const tab = event.target.closest('a[data-tab="headlines"]');
  if (!tab || event.button > 0 || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;
  // Tapping News while already on it goes back to All, like any tab
  if (document.body.dataset.tab === "headlines") return;
  let saved = null;
  try { saved = sessionStorage.getItem(NEWS_CHIP); } catch { /* ignore */ }
  if (saved && saved !== tab.getAttribute("href")) { event.preventDefault(); location.href = saved; }
});

let feedRequest = 0;
async function showFeed(href, push) {
  const wrap = document.querySelector(".feedwrap");
  const scrollLeft = wrap.querySelector(".cats")?.scrollLeft ?? 0;
  const mine = ++feedRequest;
  try {
    const response = await fetch(href, { headers: { Accept: "text/html" } });
    if (!response.ok || response.redirected) throw new Error(response.statusText);
    const doc = new DOMParser().parseFromString(await response.text(), "text/html");
    const fresh = doc.querySelector(".feedwrap");
    if (!fresh) throw new Error("no feed");
    if (mine !== feedRequest) return;  // a later tap won
    wrap.replaceChildren(...fresh.childNodes);
    document.title = doc.title;
    if (push) history.pushState({ feed: true }, "", href);
    setupCats(scrollLeft);
    applyBanner();
    rememberNewsChip();
    trackStories(wrap.querySelectorAll("[data-track-seen] .story"));
    watchLoadMore();
  } catch {
    location.href = href;
  }
}

document.addEventListener("click", (event) => {
  const more = event.target.closest(".cats-more");
  if (more) {
    store.set(CATS_OPEN, more.getAttribute("aria-expanded") !== "true");
    setupCats(more.parentElement.scrollLeft);
    return;
  }
  const link = event.target.closest(".cats a");
  if (!link || event.button > 0 || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;
  event.preventDefault();
  // Light the chip at once; the stories follow
  link.parentElement.querySelectorAll("a").forEach((a) => a.removeAttribute("aria-current"));
  link.setAttribute("aria-current", "true");
  showFeed(link.href, true);
});
addEventListener("popstate", (event) => { if (event.state?.feed) showFeed(event.state.href || location.href, false); });
document.addEventListener("DOMContentLoaded", () => {
  rememberNewsChip();
  const nav = document.querySelector(".cats");
  if (!nav) return;
  // So Back to the first category swaps too. A bare / follows the filter cookie, which later taps
  // change, so remember the selected chip's own address.
  history.replaceState({ feed: true, href: nav.querySelector('[aria-current="true"]')?.href }, "");
  setupCats();
});
// Backup for browsers where the observer does not fire: check on scroll, at most once per frame
let scrollCheck = false;
addEventListener("scroll", () => {
  if (scrollCheck) return;
  scrollCheck = true;
  requestAnimationFrame(() => {
    scrollCheck = false;
    const link = document.querySelector(".loadmore[data-fragment]");
    if (link && link.getBoundingClientRect().top < innerHeight + 1200) loadMore(link);
  });
}, { passive: true });

// ---------- images: drop broken or hotlink-blocked ones ----------
document.addEventListener("error", (event) => {
  const img = event.target;
  if (!(img instanceof HTMLImageElement)) return;
  const thumb = img.closest(".thumb-img");
  if (thumb) {
    thumb.closest(".card")?.classList.remove("has-img");
    thumb.remove();
  } else if (img.closest(".hero-media, .big-media, .swipe-media") || img.classList.contains("badge")) {
    img.remove();
  }
}, true);

// ---------- swipe deck: right = more like this, left = not interested, up = later, tap opens ----------
// The link stays a real link, so a tap opens the story; the card only takes the pointer once it moves.
const deck = document.querySelector("[data-deck]");
if (deck) {
  const LATER_COOKIE = "newsnest_swipe_later";
  const history = [];
  const leftCount = document.querySelector("[data-left]");
  const undoButton = document.querySelector("[data-undo]");
  let drag = null;
  let dragged = false;

  const topCard = () => deck.querySelector(".swipecard:not(.leaving)");
  // The top three cards form the visible stack; the rest wait out of sight
  function restack() {
    deck.querySelectorAll(".swipecard:not(.leaving)").forEach((card, i) => { card.dataset.pos = String(Math.min(i, 3)); });
  }
  function countBy(delta) { leftCount.textContent = String(Math.max(0, Number(leftCount.textContent) + delta)); }

  function paint(card, dx, dy) {
    card.style.transform = `translate(${dx}px, ${dy}px) rotate(${dx / 20}deg)`;
    const reach = card.offsetWidth * 0.3;
    const sideways = Math.abs(dx) >= Math.abs(dy);
    card.style.setProperty("--like", sideways && dx > 0 ? Math.min(1, dx / reach) : 0);
    card.style.setProperty("--nope", sideways && dx < 0 ? Math.min(1, -dx / reach) : 0);
    card.style.setProperty("--skip", !sideways && dy < 0 ? Math.min(1, -dy / reach) : 0);
  }

  function later(id, add) {
    const match = document.cookie.match(new RegExp(`${LATER_COOKIE}=([^;]*)`));
    let ids = match ? match[1].split(".").filter(Boolean) : [];
    ids = ids.filter((x) => x !== id);
    if (add) ids.push(id);
    document.cookie = `${LATER_COOKIE}=${ids.slice(-300).join(".")}; path=/; max-age=${3 * 86400}; samesite=lax`;
  }

  async function decide(card, dir, from = { dx: 0, dy: 0 }) {
    if (!card) return;
    card.classList.add("leaving");
    card.style.transition = "transform 0.32s ease-in, opacity 0.32s ease-in";
    const far = Math.max(innerWidth, 600);
    if (dir === "up") paint(card, from.dx, -innerHeight);
    else paint(card, dir === "right" ? far : -far, from.dy + (dir === "right" ? -40 : 40));
    card.style.opacity = "0";
    card.removal = setTimeout(() => card.remove(), 340);
    history.push({ card, dir });
    undoButton.disabled = false;
    countBy(-1);
    restack();
    try {
      if (dir === "right") await postForm(`/api/items/${card.dataset.lead}/rate`, { value: "1" });
      else if (dir === "left") await postForm("/api/feedback", { action: "not_interested", ids: card.dataset.ids });
      else later(card.dataset.lead, true);
    } catch { toast("That did not work; try again"); }
  }

  async function undo() {
    const last = history.pop();
    undoButton.disabled = !history.length;
    if (!last) return;
    const { card, dir } = last;
    clearTimeout(card.removal);
    card.classList.remove("leaving");
    card.style.transition = "none";
    card.style.opacity = "1";
    deck.prepend(card);
    restack();
    void card.offsetWidth;  // start the way back from where the card flew to
    card.style.transition = "transform 0.3s cubic-bezier(.2,.8,.3,1.1)";
    paint(card, 0, 0);
    countBy(1);
    try {
      if (dir === "right") await postForm(`/api/items/${card.dataset.lead}/rate`, { value: "0" });
      else if (dir === "left") await postForm("/api/feedback", { action: "undo_not_interested", ids: card.dataset.ids });
      else later(card.dataset.lead, false);
    } catch { toast("That did not work; try again"); }
  }

  deck.addEventListener("pointerdown", (event) => {
    const card = event.target.closest(".swipecard");
    if (!card || card !== topCard() || event.button > 0) return;
    drag = { card, id: event.pointerId, x: event.clientX, y: event.clientY, dx: 0, dy: 0, vx: 0, vy: 0, t: event.timeStamp };
    dragged = false;
    card.style.transition = "none";
  });
  deck.addEventListener("pointermove", (event) => {
    if (!drag || event.pointerId !== drag.id) return;
    const dx = event.clientX - drag.x;
    const dy = event.clientY - drag.y;
    const dt = Math.max(1, event.timeStamp - drag.t);
    drag.vx = (dx - drag.dx) / dt;
    drag.vy = (dy - drag.dy) / dt;
    Object.assign(drag, { dx, dy, t: event.timeStamp });
    if (!dragged && Math.hypot(dx, dy) > 10) {
      dragged = true;
      drag.card.setPointerCapture(event.pointerId);
    }
    if (dragged) paint(drag.card, dx, dy);
  });
  function release(event) {
    if (!drag || event.pointerId !== drag.id) return;
    const { card, dx, dy, vx, vy } = drag;
    drag = null;
    if (!dragged) return;
    const reach = card.offsetWidth * 0.3;
    if (Math.abs(dx) >= Math.abs(dy) && (Math.abs(dx) > reach || (Math.abs(dx) > 50 && Math.abs(vx) > 0.6))) {
      decide(card, dx > 0 ? "right" : "left", { dx, dy });
    } else if (dy < 0 && -dy > Math.abs(dx) && (-dy > reach || (-dy > 50 && vy < -0.6))) {
      decide(card, "up", { dx, dy });
    } else {
      card.style.transition = "transform 0.25s cubic-bezier(.2,.8,.3,1.2)";
      paint(card, 0, 0);
    }
  }
  deck.addEventListener("pointerup", release);
  deck.addEventListener("pointercancel", release);
  // A drag that ends over the title is not a tap: keep the story closed
  deck.addEventListener("click", (event) => {
    if (dragged) {
      event.preventDefault();
      event.stopPropagation();
      dragged = false;
      return;
    }
    const link = event.target.closest(".swipecard h2 a");
    if (link) link.closest(".swipecard").classList.add("read");  // /go marks it read on the server
  }, true);

  document.addEventListener("click", (event) => {
    const button = event.target.closest("[data-swipe]");
    if (button) decide(topCard(), button.dataset.swipe);
    else if (event.target.closest("[data-undo]")) undo();
  });
  document.addEventListener("keydown", (event) => {
    if (event.target.closest("input, textarea, select") || event.metaKey || event.ctrlKey || event.altKey) return;
    const dir = { ArrowRight: "right", ArrowLeft: "left", ArrowUp: "up" }[event.key];
    if (dir) { event.preventDefault(); decide(topCard(), dir); }
    else if (event.key === "Backspace" || event.key === "z") { event.preventDefault(); undo(); }
  });
  restack();
}

// ---------- onboarding counter ----------
const pressedOnPage = () => document.querySelectorAll('.onboard .up[aria-pressed="true"], .onboard .down[aria-pressed="true"]').length;
let ratedElsewhere = null;
function updateOnboardingCount() {
  const counter = document.getElementById("rated-count");
  if (!counter || ratedElsewhere === null) return;
  const total = ratedElsewhere + pressedOnPage();
  counter.textContent = String(total);
  const draft = document.getElementById("draft-button");
  if (draft) draft.disabled = total < 10;
}

document.addEventListener("DOMContentLoaded", () => {
  const counter = document.getElementById("rated-count");
  if (counter) ratedElsewhere = Number(counter.textContent) - pressedOnPage();
  applyFilters();
});

// ---------- search: the dock (phones) or the top bar (desktop) turns into a field; results fill a layer ----------
// Without JS the magnifier is a link to /search and the fields submit there.
const searchLayer = document.querySelector(".searchlayer");
const searchResults = document.querySelector("[data-search-results]");
const searchInputs = [...document.querySelectorAll(".docksearch input, .topsearch input")];
const phoneLayout = matchMedia("(max-width: 760px)");
let searchTimer = null;
let searchRequest = null;
let searchLoaded = false;

const searching = () => document.body.classList.contains("searching");
const searchInput = () => document.querySelector(phoneLayout.matches ? ".docksearch input" : ".topsearch input");

// iOS keeps fixed elements behind the keyboard; --kb lifts the dock to sit right above it
function syncKeyboard() {
  const vv = window.visualViewport;
  const kb = vv ? Math.max(0, innerHeight - vv.height - vv.offsetTop) : 0;
  document.documentElement.style.setProperty("--kb", `${Math.round(kb)}px`);
}

function openSearch() {
  const input = searchInput();
  if (!input || !searchLayer) return;
  searchInputs.forEach((i) => { i.tabIndex = 0; });
  input.focus({ preventScroll: true });  // inside the tap, or iOS will not raise the keyboard
  if (searching()) return;
  document.body.classList.add("searching");
  searchLayer.inert = false;
  document.querySelectorAll("[data-search-close]").forEach((b) => { b.tabIndex = 0; });
  history.pushState({ search: true }, "", "#search");
  window.visualViewport?.addEventListener("resize", syncKeyboard);
  window.visualViewport?.addEventListener("scroll", syncKeyboard);
  syncKeyboard();
  if (!searchLoaded) runSearch(input.value);
}

function closeSearch(viaHistory = false) {
  if (!searching()) return;
  document.body.classList.remove("searching");
  searchLayer.inert = true;
  document.activeElement?.blur();
  searchInputs.forEach((i) => { i.tabIndex = -1; });
  document.querySelectorAll("[data-search-close]").forEach((b) => { b.tabIndex = -1; });
  window.visualViewport?.removeEventListener("resize", syncKeyboard);
  window.visualViewport?.removeEventListener("scroll", syncKeyboard);
  document.documentElement.style.setProperty("--kb", "0px");
  if (!viaHistory && history.state?.search) history.back();
}

async function runSearch(raw, everything = false) {
  const q = raw.trim();
  if (q.length === 1) return;  // one letter matches everything; wait for the next
  clearTimeout(searchTimer);
  searchRequest?.abort();
  searchRequest = new AbortController();
  try {
    const url = `/search?q=${encodeURIComponent(q)}${everything ? "&all=1" : ""}`;
    const response = await fetch(url, { headers: { "X-Fragment": "1" }, signal: searchRequest.signal });
    if (response.redirected) { location.href = response.url; return; }  // logged out meanwhile
    if (!response.ok) throw new Error(response.statusText);
    searchResults.innerHTML = await response.text();
    searchLoaded = true;
    searchLayer.scrollTop = 0;
  } catch (error) {
    if (error.name !== "AbortError") searchResults.innerHTML = '<p class="empty">Could not reach NewsNest. Try again.</p>';
  }
}

if (searchLayer) {
  // A reload or a restored page must not come back as a dangling #search entry
  if (history.state?.search) history.replaceState(null, "", location.pathname + location.search);

  document.addEventListener("click", (event) => {
    if (event.target.closest("[data-search-open]")) {
      event.preventDefault();
      // the desktop magnifier toggles; on phones the section circle closes
      if (searching() && !phoneLayout.matches) closeSearch();
      else openSearch();
    } else if (searching() && event.target.closest("[data-search-close]")) {
      event.preventDefault();
      closeSearch();
    } else if (event.target.closest(".searchlayer [data-search-all]")) {
      event.preventDefault();
      runSearch(searchInput().value, true);
    } else if (event.target.closest(".search-clear")) {
      const input = searchInput();
      input.value = "";
      input.dispatchEvent(new Event("input"));
      input.focus();
    }
  });
  searchInputs.forEach((input) => {
    input.addEventListener("input", () => {
      input.form.querySelector(".search-clear")?.toggleAttribute("hidden", !input.value);
      clearTimeout(searchTimer);
      searchTimer = setTimeout(() => runSearch(input.value), 250);
    });
    input.form.addEventListener("submit", (event) => {
      event.preventDefault();
      runSearch(input.value);
      input.blur();  // the keyboard goes away so the results show
    });
  });
  addEventListener("popstate", () => { if (searching() && !history.state?.search) closeSearch(true); });
  document.addEventListener("keydown", (event) => { if (event.key === "Escape" && searching()) closeSearch(); });
}

// ---------- PWA: service worker, install, refresh ----------
if ("serviceWorker" in navigator && window.isSecureContext) {
  addEventListener("load", () => navigator.serviceWorker.register("/sw.js").catch(() => { /* not installable here; the site still works */ }));
}

const isStandalone = matchMedia("(display-mode: standalone)").matches || navigator.standalone === true;
let installEvent = null;
addEventListener("beforeinstallprompt", (event) => {
  event.preventDefault();  // keep the browser's mini-bar away; the button on the More page installs
  installEvent = event;
  renderInstallPanel();
});
addEventListener("appinstalled", () => { installEvent = null; renderInstallPanel(); });

function renderInstallPanel() {
  const panel = document.getElementById("install");
  if (!panel) return;
  const status = panel.querySelector("[data-install-status]");
  const button = panel.querySelector("[data-install]");
  button.hidden = true;
  if (isStandalone) {
    status.textContent = "NewsNest is installed on this device. You're using the app right now.";
  } else if (!window.isSecureContext) {
    status.textContent = "Installing needs a secure (https) address. Open NewsNest through its https address, e.g. https://newsnest.example.com, then come back here.";
  } else if (installEvent) {
    status.textContent = "Add NewsNest to your home screen: it opens full screen like an app.";
    button.hidden = false;
  } else if (/iphone|ipad|ipod/i.test(navigator.userAgent)) {
    status.textContent = "On iPhone: tap the Share button in Safari, then “Add to Home Screen”.";
  } else {
    status.textContent = "Open the browser menu (⋮) and choose “Install app” or “Add to Home screen”. If it isn't offered, reload this page once and try again.";
  }
}
document.addEventListener("DOMContentLoaded", () => {
  renderInstallPanel();
  document.querySelector("[data-install]")?.addEventListener("click", async () => {
    if (!installEvent) return;
    installEvent.prompt();
    await installEvent.userChoice;
    installEvent = null;
    renderInstallPanel();
  });
  document.querySelectorAll("[data-refresh]").forEach((b) => b.addEventListener("click", () => location.reload()));
});


// ---------- settings: a row's ⋯ menu, words as chips, folded groups that open for the row you came back to ----------
const rowSheet = document.getElementById("rowmenu");
const rowBackdrop = document.getElementById("rowbackdrop");
let rowButton = null;

function closeRowMenu() {
  if (!rowSheet || rowSheet.hidden) return;
  rowSheet.hidden = true;
  rowBackdrop.hidden = true;
  rowButton?.setAttribute("aria-expanded", "false");
  rowButton?.focus({ preventScroll: true });
}

document.addEventListener("click", (event) => {
  if (!rowSheet) return;
  if (event.target === rowBackdrop) { closeRowMenu(); return; }
  const button = event.target.closest(".rowmenu-btn");
  if (!button) return;
  rowButton = button;
  rowSheet.querySelector(".sheet-title").textContent = button.dataset.title;
  rowSheet.querySelector("[data-items]").replaceChildren(button.nextElementSibling.content.cloneNode(true));
  rowSheet.hidden = false;
  rowBackdrop.hidden = false;
  button.setAttribute("aria-expanded", "true");
  if (window.matchMedia("(min-width: 761px)").matches) {
    const r = button.getBoundingClientRect();
    const top = r.bottom + 8 + rowSheet.offsetHeight < innerHeight ? r.bottom + 8 : r.top - rowSheet.offsetHeight - 8;
    rowSheet.style.top = `${Math.max(8, top)}px`;
    rowSheet.style.left = `${Math.max(8, r.right - rowSheet.offsetWidth)}px`;
  } else {
    rowSheet.style.top = rowSheet.style.left = "";
  }
  rowSheet.querySelector(".pop-item")?.focus({ preventScroll: true });
});
document.addEventListener("keydown", (event) => { if (event.key === "Escape") closeRowMenu(); });

// Mute / always-show words: the textarea (one word per line) becomes chips; every change saves through the form's data-autosave
document.querySelectorAll("[data-chipbox]").forEach((box) => {
  const area = box.querySelector("textarea");
  const form = box.closest("form");
  const words = area.value.split(/[\n,]/).map((w) => w.trim()).filter(Boolean);
  const ui = document.createElement("div");
  ui.className = "chipbox-ui";
  const input = document.createElement("input");
  input.className = "chipinput";
  input.placeholder = "Add a word…";
  input.setAttribute("aria-label", `Add to ${area.getAttribute("aria-label")}`);
  input.enterKeyHint = "done";

  function sync(save) {
    area.value = words.join("\n");
    ui.querySelectorAll(".schip").forEach((chip) => chip.remove());
    words.forEach((word) => {
      const chip = document.createElement("span");
      chip.className = "schip";
      chip.innerHTML = '<span></span><button type="button"><svg viewBox="0 0 24 24" aria-hidden="true"><path d="M7 7l10 10M17 7 7 17"/></svg></button>';
      chip.firstChild.textContent = word;
      chip.lastChild.setAttribute("aria-label", `Remove ${word}`);
      chip.lastChild.addEventListener("click", () => { words.splice(words.indexOf(word), 1); sync(true); });
      ui.insertBefore(chip, input);
    });
    if (save) area.dispatchEvent(new Event("change", { bubbles: true }));
  }
  function add() {
    const typed = input.value.split(",").map((w) => w.trim()).filter((w) => w && !words.includes(w));
    input.value = "";
    if (typed.length) { words.push(...typed); sync(true); }
  }
  input.addEventListener("keydown", (event) => {
    if (event.key === "Enter" || event.key === ",") { event.preventDefault(); add(); }
    else if (event.key === "Backspace" && !input.value && words.length) { words.pop(); sync(true); }
  });
  input.addEventListener("blur", add);
  ui.append(input);
  area.hidden = true;
  box.append(ui);
  form.querySelector("[data-chip-save]")?.setAttribute("hidden", "");
  sync(false);
});

// A feed you came back to (#feed-…) may sit in a folded group: open it and bring the row into view
function openTargetGroup() {
  const target = document.querySelector(":target");
  const group = target?.closest("details");
  if (group && !group.open) { group.open = true; target.scrollIntoView({ block: "center" }); }
}
document.addEventListener("DOMContentLoaded", openTargetGroup);
addEventListener("hashchange", openTargetGroup);
