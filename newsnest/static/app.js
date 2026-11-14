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
// On wide screens a story can be on the page twice (the front page and the feed beside it): both copies
// carry the same data-ids, and both change together.
function setStoryRead(story, read) {
  if (!story) return;
  const ids = story.dataset.ids;
  const copies = ids ? document.querySelectorAll(`.story[data-ids="${CSS.escape(ids)}"]`) : [story];
  for (const copy of copies) {
    copy.dataset.read = read ? "1" : "0";
    copy.classList.toggle("read", read);
    if (read) copy.classList.remove("unseen");  // as the server renders it: a read story is no longer new
    const button = copy.querySelector(".readtoggle");
    if (button) {
      button.setAttribute("aria-pressed", String(read));
      button.value = read ? "0" : "1";
      const label = read ? "Mark as unread" : "Mark as read";
      button.setAttribute("aria-label", label);
      button.title = label;
    }
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
  showChoice(event.target);
  form.classList.add("saving");
  form.requestSubmit();
});

// A value-select is gray text over an invisible <select>: the text follows the choice right away
function showChoice(select) {
  const text = select.closest?.(".vselect")?.querySelector(".vselect-text");
  if (text && select.selectedOptions?.[0]) text.textContent = select.selectedOptions[0].textContent;
}

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
  // A button can ask on its own (the bin beside an interest being edited), else the form asks
  const ask = event.submitter?.dataset.confirm || form.dataset.confirm;
  if (ask && !window.confirm(ask)) {
    event.preventDefault();
    return;
  }
  if (form.matches("[data-inplace]")) {
    event.preventDefault();
    saveInPlace(form, event.submitter);
    return;
  }
  const button = event.submitter;
  if (!button) return;

  if (form.classList.contains("rate")) {
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
  const link = event.target.closest(".story :is(h2, h3) a, .thumb-img, .hero-media, .big-media, .story .openlink");
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
// Not beside the desktop front page (laneEnds): that page is an overview you look around in, not a list
// you read through, so there a story is read when you open it, tick it or mark all read. Seen still counts.
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
      if (scrolledPast && viewedOnce.has(story) && story.dataset.read !== "1" && !laneEnds()) {
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
  trackStories = (stories) => stories.forEach((s) => {
    observer.observe(s);
    attention.observe(s);
  });
  document.addEventListener("DOMContentLoaded", () => trackStories(document.querySelectorAll("[data-track-seen] .story")));
  setInterval(flushSeen, 3000);
  addEventListener("pagehide", flushSeen);
}

// ---------- attention: how long each card is in view while you read (feed page only) ----------
// Most stories are read from the headline and summary, never opened, so the time a card stays on screen
// is the main sign of interest (newsnest.drift). Time only runs while the page is visible and you did
// something in the last ATTENTION_IDLE ms, and it is split between the cards in view at once, so two
// cards side by side get half each. A card is in view when 60% of it shows, or when it fills at least
// half the screen (a tall card can never show 60%).
const ATTENTION_TICK = 500;
const ATTENTION_IDLE = 30000;
const ATTENTION_CARD_MAX = 60000;   // per card per page load; the server caps the total
const attentionMs = new Map();      // story element -> ms not sent yet
const attentionTotal = new WeakMap(); // story element -> ms this page load
const inView = new Set();
let lastInput = performance.now();  // the first screen counts without a scroll
let lastTick = performance.now();

for (const type of ["scroll", "wheel", "touchstart", "touchmove", "pointerdown", "pointermove", "keydown"]) {
  addEventListener(type, () => { lastInput = performance.now(); }, { passive: true, capture: true });
}

const attention = "IntersectionObserver" in window ? new IntersectionObserver((entries) => {
  for (const entry of entries) {
    const big = entry.intersectionRect.height >= 0.5 * innerHeight;
    if (entry.isIntersecting && (entry.intersectionRatio >= 0.6 || big)) inView.add(entry.target);
    else inView.delete(entry.target);
  }
}, { threshold: [0, 0.25, 0.5, 0.6, 0.75, 1] }) : { observe() {} };

setInterval(() => {
  const now = performance.now();
  const dt = Math.min(now - lastTick, 1000);  // a sleeping tab or laptop wakes up without a jump
  lastTick = now;
  if (document.visibilityState !== "visible" || now - lastInput > ATTENTION_IDLE) return;
  for (const story of inView) if (!story.isConnected || story.hidden) inView.delete(story);
  if (!inView.size) return;
  const share = dt / inView.size;
  for (const story of inView) {
    const total = attentionTotal.get(story) || 0;
    const add = Math.min(share, ATTENTION_CARD_MAX - total);
    if (add <= 0) continue;
    attentionTotal.set(story, total + add);
    attentionMs.set(story, (attentionMs.get(story) || 0) + add);
  }
}, ATTENTION_TICK);

function leadId(story) {
  const m = /^item-(\d+)$/.exec(story?.id || "");
  return m ? m[1] : story?.dataset.ids?.split(",")[0];
}

function flushAttention() {
  const pairs = [];
  for (const [story, ms] of attentionMs) {
    const id = leadId(story);
    if (ms >= 100 && id) {
      pairs.push(`${id}:${Math.round(ms)}`);
      attentionMs.delete(story);  // before sending, so a second trigger right after sends nothing twice
    }
  }
  if (!pairs.length) return;
  const body = new FormData();
  body.set("ms", pairs.join(","));
  if (!navigator.sendBeacon("/api/attention", body)) fetch("/api/attention", { method: "POST", body, keepalive: true });
}
setInterval(flushAttention, 3000);
addEventListener("pagehide", flushAttention);
// iOS Safari often skips pagehide when you switch apps; this one does fire
document.addEventListener("visibilitychange", () => { if (document.visibilityState === "hidden") flushAttention(); });

// Opening a card's key points yourself (not a page that shows them open) is a sign of interest too
document.addEventListener("click", (event) => {
  const summary = event.target.closest(".tldr > summary");
  if (!summary || summary.parentElement.open) return;  // the click opens it only if it was closed
  const id = leadId(summary.closest(".story, [data-ids]"));
  if (!id) return;
  const url = `/api/items/${id}/expanded`;
  if (!navigator.sendBeacon(url)) fetch(url, { method: "POST", keepalive: true });
});

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
  const readItem = sheet.querySelector('[data-action="read"]');
  if (readItem) {
    const read = button.closest(".story")?.dataset.read === "1";
    readItem.dataset.value = read ? "0" : "1";
    readItem.querySelector("span").textContent = read ? "Mark as unread" : "I've already read this";
  }
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
  const story = menuButton.closest(".story");
  closeSheet();
  if (action === "read") {
    const read = item.dataset.value === "1";
    try {
      await postForm("/api/read", { ids: d.ids, value: item.dataset.value });
      setStoryRead(story, read);
      applyFilters();
      toast(read ? "Marked as read" : "Marked as unread");
    } catch { toast("That did not work; try again"); }
    return;
  }
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
// Beside the front page (wide screens) the feed ends in More stories instead of loading on
const laneEnds = () => wideFront.matches && !!document.querySelector(".feedwrap.home > .front");
async function loadMore(link) {
  if (loadingMore || !link.isConnected || laneEnds()) return;
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
  if (laneEnds()) return;
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

const wideFront = matchMedia("(min-width: 1240px)");

// Beside the front page the feed lane shows as many stories as fit next to it (at least LANE_MIN), so the two
// lanes end together; More stories then opens the list view starting at the first story left out. Measured on load,
// on resize and after a tab swap; not while reading (opening key points in the lane does not cut it again).
// The lane's copies of the front page's stories (.on-front) are hidden by CSS and not counted here.
// Without the script the lane shows the whole first page and the button's link starts after it.
const LANE_MIN = 8;
function fitLane() {
  const wrap = document.querySelector(".feedwrap.home");
  const front = wrap?.querySelector(":scope > .front");
  const more = wrap?.querySelector(".lane-more");
  if (!front || !more) return;  // no button: the whole feed fits in the first page, so nothing is cut
  const all = [...wrap.querySelectorAll(".stream > .story")];
  all.forEach((card) => card.classList.remove("lane-over"));
  const cards = wideFront.matches ? all.filter((card) => !card.classList.contains("on-front")) : all;
  let shown = cards.length;
  if (wideFront.matches) {
    const end = front.getBoundingClientRect().bottom + 80;  // a card may run a little past the front page
    const over = cards.findIndex((card, i) => i >= LANE_MIN && card.getBoundingClientRect().bottom > end);
    if (over > 0) shown = over;
  }
  cards.slice(shown).forEach((card) => card.classList.add("lane-over"));
  const next = cards[shown];
  // Nothing left out and nothing past the first page: no button
  more.hidden = !next && more.dataset.rest !== "1";
  if (next) {  // else the server's link already starts after the first page
    const url = new URL(more.getAttribute("href"), location.href);
    url.searchParams.set("at", next.id.replace("item-", ""));  // the list starts at this story
    more.setAttribute("href", url.pathname + url.search);
    // The list view runs from that story to the end of the feed, front page stories included
    more.querySelector("span").textContent = `${Number(more.dataset.total) - all.indexOf(next)} more`;
  }
}
addEventListener("load", fitLane);
wideFront.addEventListener?.("change", fitLane);
let laneResize = null;
addEventListener("resize", () => { clearTimeout(laneResize); laneResize = setTimeout(fitLane, 200); });

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
// ---------- the feed swaps in place: dock tabs, category chips, Back ----------
// The tapped tab or chip lights at once and the old stories dim; if the server takes a moment (ranking a
// new feed session can), placeholder cards hold the place until the stories arrive. Two timers keep the
// placeholders from flickering: an answer within SKELETON_DELAY swaps straight in and never shows them,
// and once shown they stay at least SKELETON_MIN, so they never flash by. A dock tab is another page, so
// its placeholders go up at once, before the jump to the top (the old tab's top never shows); they stay a
// shorter SKELETON_MIN_TAB, so a quick answer is not held back for long. Anything unexpected falls back
// to loading the page.
const SKELETON_DELAY = 250;    // ms
const SKELETON_MIN = 400;      // ms
const SKELETON_MIN_TAB = 200;  // ms
let skeletonShownAt = null;  // when the placeholders on screen appeared (a later tap keeps them up)
let skeletonMin = SKELETON_MIN;
const SKELETON_CARD = '<div class="skel-card"><div class="skel-text"><i class="skel skel-meta"></i>' +
  '<i class="skel skel-title"></i><i class="skel skel-title short"></i><i class="skel skel-line"></i></div><i class="skel skel-thumb"></i></div>';
const SKELETON = '<div class="skel-feed" aria-hidden="true"><div class="skel-card big"><i class="skel skel-media"></i>' +
  '<div class="skel-text"><i class="skel skel-meta"></i><i class="skel skel-title"></i><i class="skel skel-title short"></i>' +
  `<i class="skel skel-line"></i><i class="skel skel-line"></i></div></div>${SKELETON_CARD.repeat(3)}</div>`;
const SKELETON_CHIPS = `<div class="cats skel-chips" aria-hidden="true">${'<i class="skel"></i>'.repeat(5)}</div>`;

const swapsInPlace = () => !!document.body.dataset.tab && !!document.querySelector(".feedwrap #stream");
const plainClick = (event) => !(event.button > 0 || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey);

function showSkeleton(wrap, tab, min = SKELETON_MIN) {
  const stream = wrap.querySelector("#stream");
  if (!stream) return;
  if (!stream.querySelector(".skel-feed")) {
    stream.innerHTML = SKELETON;
    skeletonShownAt = performance.now();
    skeletonMin = min;
  }
  if (!tab || wrap.querySelector(".skel-chips")) return;
  // Another tab: its chips and notes are not known yet either
  wrap.querySelectorAll(".callout").forEach((c) => c.remove());
  const cats = wrap.querySelector(".cats");
  if (cats) cats.outerHTML = SKELETON_CHIPS;
  else stream.insertAdjacentHTML("beforebegin", SKELETON_CHIPS);
}

// Light a dock tab (top bar and phone dock) before its stories arrive
function lightTab(key) {
  document.querySelectorAll(".sections a[data-tab], .tabbar a[data-tab]").forEach((a) => {
    if (a.dataset.tab === key) a.setAttribute("aria-current", "page");
    else a.removeAttribute("aria-current");
  });
}

// The dock as the new page has it: which tab is lit, which one refreshes when tapped again, its icon
function syncDock(doc) {
  document.body.dataset.tab = doc.body.dataset.tab || "";
  for (const selector of [".topbar .sections", ".tabbar"]) {
    const here = document.querySelector(selector);
    const there = doc.querySelector(selector);
    if (!here || !there) continue;
    const keys = (nav) => [...nav.querySelectorAll("a[data-tab]")].map((a) => a.dataset.tab).join(" ");
    if (keys(here) !== keys(there)) {  // a section became a tab or stopped being one
      here.replaceWith(document.importNode(there, true));
      continue;
    }
    here.querySelectorAll("a[data-tab]").forEach((a) => {
      const b = there.querySelector(`a[data-tab="${CSS.escape(a.dataset.tab)}"]`);
      a.setAttribute("href", b.getAttribute("href"));
      if (b.hasAttribute("aria-current")) a.setAttribute("aria-current", "page");
      else a.removeAttribute("aria-current");
    });
  }
  const icon = doc.querySelector(".tabcurrent");
  if (icon) document.querySelector(".tabcurrent")?.replaceChildren(...[...icon.childNodes].map((n) => document.importNode(n, true)));
}

// ---------- each view keeps its place: back on a tab (or Back to a view), you are where you left it ----------
// As tab bars do on iOS, and as Material 3 asks of apps people switch around in. A view (a tab, a chip, the
// list view) remembers the story at the top of the screen, how far down it sat, and how many stories had
// loaded; coming back loads that many again and puts that story where it was. Only within the same feed
// session (newsnest.feedsession): there the order is frozen, so the same stories come back in the same
// places. A new session (Refresh, Mark all read, away for the idle time) is a new feed and starts at the top.
// Tapping the tab you are on still goes to the top. Kept per browser tab (sessionStorage), so it also
// covers a tab tapped on another page and Back after the browser dropped the page; the browser's own
// scroll restoration is off on the feed, since it only knows pixels, not which story was there.
const PLACES = "newsnest.places";
function viewKey(href) {
  const url = new URL(href, location.href);
  url.searchParams.delete("refresh");
  url.searchParams.delete("n");
  url.searchParams.sort();
  return url.pathname + url.search;
}
function readPlaces() {
  try { return JSON.parse(sessionStorage.getItem(PLACES) || "{}") || {}; } catch { return {}; }
}
// Only a place from that feed session counts
function placeFor(href, session) {
  const place = readPlaces()[viewKey(href)];
  return place && place.session === session ? place : null;
}
function savePlace() {
  const wrap = document.querySelector(".feedwrap[data-href][data-session]");
  // Mid-swap the page shows the old view dimmed or placeholders: its place was saved when the swap began
  if (!wrap || !swapsInPlace() || wrap.classList.contains("swapping")) return;
  const session = wrap.dataset.session;
  const stories = [...wrap.querySelectorAll("#stream .story[id]")].filter((s) => !s.hidden && s.getClientRects().length);
  // The first story starting on screen; scrolled past the last one (beside the desktop front page, the lane
  // ends first), the last one, above the screen
  const story = scrollY > AT_TOP ? stories.find((s) => s.getBoundingClientRect().top >= 0) ?? stories.at(-1) : null;
  const place = { session, y: Math.round(scrollY), count: stories.length, story: story?.id ?? null,
                  offset: story ? Math.round(story.getBoundingClientRect().top) : 0 };
  // Places from an earlier session can never be used again: drop them
  const places = Object.fromEntries(Object.entries(readPlaces()).filter(([, p]) => p.session === session));
  places[viewKey(wrap.dataset.href)] = place;
  try { sessionStorage.setItem(PLACES, JSON.stringify(places)); } catch { /* storage blocked: no memory */ }
}
// Things still move just after the jump: cards are content-visibility: auto (app.css), so the ones that come
// into view only then take their real height instead of the estimate, and web fonts can land late. The
// story is put back where it was each frame until it stays put, unless you start scrolling first.
let settling = null;
function goToPlace(place) {
  // true once there
  const go = () => {
    const story = place.story && document.getElementById(place.story);
    const target = story && story.getClientRects().length
      ? scrollY + story.getBoundingClientRect().top - place.offset
      : place.y;  // that story has gone (hidden meanwhile): about the same depth
    if (Math.abs(scrollY - target) <= 1) return true;
    scrollTo({ top: Math.max(0, target), behavior: "instant" });
    return false;
  };
  settling = go;
  const stop = () => { if (settling === go) settling = null; };
  for (const type of ["wheel", "touchstart", "keydown", "pointerdown"]) addEventListener(type, stop, { once: true, passive: true });
  let still = 0;
  let frames = 0;
  // Three quiet frames in a row, or about a second (a page still too short by then stays as close as it got)
  const step = () => {
    if (settling !== go) return;
    still = go() ? still + 1 : 0;
    if (still < 3 && ++frames < 60) requestAnimationFrame(step);
  };
  step();
  document.fonts?.ready.then(() => settling?.());
  if (document.readyState !== "complete") addEventListener("load", () => settling?.(), { once: true });
  setTimeout(stop, 3000);
}
addEventListener("pagehide", savePlace);
document.addEventListener("visibilitychange", () => { if (document.visibilityState === "hidden") savePlace(); });

let feedLoad = null;
// push: a new history entry (a tap); otherwise the current one is updated (Back, or a refresh).
// now: the placeholders go up at once instead of after SKELETON_DELAY (a dock tab)
// restore: arrive where this view was left (else at the top), loading as many stories as it had
async function showFeed(href, { push = false, tab = false, now = false, restore = false } = {}) {
  const wrap = document.querySelector(".feedwrap");
  const scrollLeft = tab ? 0 : wrap.querySelector(".cats")?.scrollLeft ?? 0;
  if (viewKey(href) !== viewKey(wrap.dataset.href || location.href)) savePlace();  // the view being left
  if (restore) {
    // One session per device: a place from another one will not be used, so it needs no extra stories
    const count = placeFor(href, wrap.dataset.session)?.count;
    if (count) {
      const url = new URL(href, location.href);
      url.searchParams.set("n", String(count));
      href = url.href;
    }
  }
  feedLoad?.abort();  // a later tap wins
  const load = feedLoad = new AbortController();
  wrap.classList.remove("swapped");
  wrap.classList.add("swapping");
  wrap.querySelector("#stream")?.setAttribute("aria-busy", "true");
  if (now) showSkeleton(wrap, tab, SKELETON_MIN_TAB);
  const skeleton = now ? null : setTimeout(() => showSkeleton(wrap, tab), SKELETON_DELAY);
  try {
    const response = await fetch(href, { headers: { Accept: "text/html" }, signal: load.signal });
    // Refresh (?refresh=1) redirects back to the feed; anything else (the login page) loads for real
    const landed = new URL(response.url);
    if (!response.ok || landed.origin !== location.origin || landed.pathname !== "/") throw new Error(response.statusText);
    const doc = new DOMParser().parseFromString(await response.text(), "text/html");
    const fresh = doc.querySelector(".feedwrap");
    if (!fresh) throw new Error("no feed");
    if (load !== feedLoad) return;
    clearTimeout(skeleton);  // arrived in time: the placeholders never show
    if (skeletonShownAt !== null) {  // already showing: keep them up long enough not to flash
      const wait = skeletonMin - (performance.now() - skeletonShownAt);
      if (wait > 0) await new Promise((resolve) => { setTimeout(resolve, wait); });
      if (load !== feedLoad) return;  // tapped elsewhere meanwhile
    }
    skeletonShownAt = null;
    wrap.replaceChildren(...fresh.childNodes);
    wrap.classList.add("swapped");  // the new stories fade in
    wrap.dataset.href = fresh.dataset.href;
    wrap.dataset.session = fresh.dataset.session;
    document.title = doc.title;
    syncDock(doc);
    const state = { feed: true, href: fresh.dataset.href };
    landed.searchParams.delete("n");  // how many to load back is not part of the view's address
    const address = landed.pathname + landed.search;
    if (push) history.pushState(state, "", address);
    else history.replaceState(state, "", address);
    setupCats(scrollLeft);
    fitLane();
    applyBanner();
    rememberNewsChip();
    trackStories(wrap.querySelectorAll("[data-track-seen] .story"));
    watchLoadMore();
    if (restore) {
      const place = placeFor(fresh.dataset.href, fresh.dataset.session);
      if (place) goToPlace(place);
      else scrollTo({ top: 0, behavior: "instant" });
    }
  } catch (error) {
    if (error.name !== "AbortError" && load === feedLoad) location.href = href;
  } finally {
    clearTimeout(skeleton);
    if (load === feedLoad) {
      feedLoad = null;
      wrap.classList.remove("swapping");
    }
  }
}

// A dock tab: lit at once, its placeholders at once, back to the top behind them, its stories follow, and
// another tab opens where you left it (its place, above). Tapping the tab you are on takes two
// steps, as in most feed apps: scrolled down, the tap only goes back to the top (the feed keeps its order);
// at the top it refreshes (its link carries refresh=1), with the same placeholders instead of a blank reload.
// From a chip inside the tab, the tap goes to the tab's All at the top: the tab you are on always means the top.
const AT_TOP = 8;  // px: this close to the top counts as there
document.addEventListener("click", (event) => {
  const tab = event.target.closest(".sections a[data-tab], .tabbar a[data-tab]");
  if (!tab || !plainClick(event) || !swapsInPlace()) return;
  event.preventDefault();
  if (new URL(tab.href).searchParams.has("refresh") && scrollY > AT_TOP) {
    const still = matchMedia("(prefers-reduced-motion: reduce)").matches;
    scrollTo({ top: 0, behavior: still ? "instant" : "smooth" });
    return;
  }
  let href = tab.href;
  // News reopens the chip I was on inside it (tapping News while on it goes back to its All)
  if (tab.dataset.tab === "headlines" && document.body.dataset.tab !== "headlines") {
    let saved = null;
    try { saved = sessionStorage.getItem(NEWS_CHIP); } catch { /* storage blocked */ }
    if (saved) {
      // The chip, in the view the tab was tapped in (front page or the list of all stories)
      const chip = new URL(saved, location.href);
      const list = new URL(href).searchParams.get("view");
      if (list) chip.searchParams.set("view", list);
      else chip.searchParams.delete("view");
      href = chip.href;
    }
  }
  const refresh = new URL(href).searchParams.has("refresh");
  const another = tab.dataset.tab !== document.body.dataset.tab;
  lightTab(tab.dataset.tab);
  // puts the placeholders up before it waits
  showFeed(href, { push: !refresh, tab: true, now: true, restore: another && !refresh });
  scrollTo({ top: 0, behavior: "instant" });
});

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
  showFeed(link.href, { push: true });
});
addEventListener("popstate", (event) => {
  if (!event.state?.feed || !swapsInPlace()) return;
  // Closing search goes Back to the view already on screen: nothing to load
  if (event.state.href === document.querySelector(".feedwrap").dataset.href) return;
  showFeed(event.state.href || location.href, { tab: true, restore: true });
});
document.addEventListener("DOMContentLoaded", () => {
  rememberNewsChip();
  // So Back to the first view swaps too. A bare / follows the filter cookie, which later taps
  // change, so remember the view's own address.
  const wrap = document.querySelector(".feedwrap[data-href]");
  if (wrap && !history.state?.search) history.replaceState({ feed: true, href: wrap.dataset.href }, "");
  setupCats();
  fitLane();
  if (!wrap || !swapsInPlace()) return;
  // The feed puts each view back in its place itself (above), Back and reloads included
  if ("scrollRestoration" in history) history.scrollRestoration = "manual";
  // Opened anew (a tab tapped on another page, Back after the browser dropped the page, a reload): its place
  const place = placeFor(wrap.dataset.href, wrap.dataset.session);
  if (!place) return;
  const stories = wrap.querySelectorAll("#stream .story[id]").length;
  if (place.story && !document.getElementById(place.story) && place.count > stories) {
    showFeed(wrap.dataset.href, { restore: true });  // it was past the first page: load as many as then
  } else {
    goToPlace(place);
  }
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

// ---------- feed catalog: the selected count and where the picks go, Select all per topic, topic chips and search
// that narrow the list, and logos that arrive while the page is open ----------
const catalogForm = document.getElementById("catalog");
if (catalogForm) {
  const boxes = (root) => [...root.querySelectorAll('input[name="url"]')];
  const shownBoxes = (root) => boxes(root).filter((b) => !b.closest("[data-q]").hidden);   // Select all leaves out what search hides
  const topics = [...catalogForm.querySelectorAll("[data-topic]")];
  const chips = [...document.querySelectorAll(".catalog-topics [data-filter]")];
  const search = document.querySelector("[data-catalog-search]");
  const syncCatalog = () => {
    const picked = boxes(catalogForm).filter((b) => b.checked).length;
    catalogForm.querySelector("[data-count]").textContent = String(picked);
    catalogForm.querySelector("[data-try]").disabled = picked === 0;
    catalogForm.querySelectorAll("[data-select-all]").forEach((button) => {
      const own = shownBoxes(button.closest("[data-topic]"));
      button.hidden = !own.length;
      button.textContent = own.every((b) => b.checked) ? "Clear" : "Select all";
    });
    // "· 2 to World, 1 to Tech": each topic's picks go to the section its "Goes to" names
    const dest = new Map();
    topics.forEach((topic) => {
      const n = boxes(topic).filter((b) => b.checked).length;
      const select = topic.querySelector('select[name^="section."]');
      chips.find((c) => c.dataset.filter === topic.dataset.topic)?.classList.toggle("picked", n > 0);
      if (!n || !select) return;
      const label = select.selectedOptions[0].textContent.replace(/^New section: /, "");
      dest.set(label, (dest.get(label) || 0) + n);
    });
    catalogForm.querySelector("[data-dest-summary]").textContent =
      dest.size ? " · " + [...dest].map(([label, n]) => `${n} to ${label}`).join(", ") : "";
  };
  catalogForm.addEventListener("change", (event) => {
    // "Goes to" is gray text over an invisible <select>: the text follows the choice (the page doesn't reload)
    const text = event.target.closest(".vselect")?.querySelector(".vselect-text");
    if (text) text.textContent = event.target.selectedOptions[0].textContent;
    syncCatalog();
  });

  // One topic at a time (or all), narrowed by what is typed; hidden rows keep their ticks
  const linked = location.hash.startsWith("#topic-") ? location.hash.slice(7) : "";
  let shown = topics.some((t) => t.dataset.topic === linked) ? linked : topics[0]?.dataset.topic || "";
  const filterCatalog = () => {
    const words = (search?.value || "").toLowerCase().trim().split(/\s+/).filter(Boolean);
    let any = false;
    topics.forEach((topic) => {
      let visible = 0;
      topic.querySelectorAll("[data-q]").forEach((row) => {
        const hit = words.every((w) => row.dataset.q.includes(w));
        row.hidden = !hit;
        visible += hit;
      });
      topic.hidden = !visible || (!words.length && shown !== "" && topic.dataset.topic !== shown);
      any ||= !topic.hidden;
    });
    chips.forEach((chip) => chip.setAttribute("aria-pressed", String(!words.length && chip.dataset.filter === shown)));
    // Where the chips scroll sideways (phones), keep the chosen one in view
    chips.find((c) => c.getAttribute("aria-pressed") === "true")?.scrollIntoView({ block: "nearest", inline: "nearest" });
    catalogForm.querySelector("[data-catalog-empty]").hidden = any;
    syncCatalog();
  };
  chips.forEach((chip) => {
    chip.hidden = false;
    chip.addEventListener("click", (event) => {
      event.preventDefault();
      shown = chip.dataset.filter;
      if (search) search.value = "";
      filterCatalog();
      document.querySelector(".catalog-tools").scrollIntoView({ block: "start", behavior: "smooth" });
    });
  });
  if (search) {
    search.hidden = false;
    search.addEventListener("input", filterCatalog);
  }
  filterCatalog();
  catalogForm.addEventListener("click", (event) => {
    const button = event.target.closest("[data-select-all]");
    if (!button) return;
    const own = shownBoxes(button.closest("[data-topic]"));
    const on = !own.every((b) => b.checked);
    own.forEach((b) => { b.checked = on; });
    syncCatalog();
  });
  syncCatalog();
  window.addEventListener("pageshow", syncCatalog);  // Back restores ticked boxes

  // The server starts looking up missing logos when the page opens: swap each in as it is found, for a minute at most
  const started = Date.now();
  const pollIcons = async () => {
    if (!catalogForm.querySelector("[data-icon]")) return;
    let data;
    try {
      data = await (await fetch(catalogForm.dataset.icons, { headers: { Accept: "application/json" } })).json();
    } catch {
      return;
    }
    catalogForm.querySelectorAll("[data-icon]").forEach((badge) => {
      const src = data.icons[badge.dataset.icon];
      if (!src) return;
      const img = Object.assign(document.createElement("img"), { className: "savatar logo", src, alt: "", decoding: "async" });
      badge.replaceWith(img);
    });
    if (data.running && Date.now() - started < 60000) setTimeout(pollIcons, 3000);
  };
  setTimeout(pollIcons, 1500);
}

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

// ---------- refresh: on the feed, a fresh ranking (a new feed session); anywhere else, a reload ----------
// The feed keeps its order through reloads on purpose (newsnest.feedsession), so a plain reload would
// bring nothing new. Refresh asks the server for a new session for the chip I'm on (it redirects back).
const feedPage = !!document.getElementById("stream");
function refreshPage() {
  if (!feedPage) { location.reload(); return; }
  const url = new URL(location.href);
  url.hash = "";
  url.searchParams.delete("n");
  url.searchParams.set("refresh", "1");
  location.replace(url.href);  // no extra history entry: Back still leaves the feed
}

// Pull to refresh on the feed: pull down from the very top and let go past the mark. The browser's own
// pull-to-reload is off on this page (app.css), since a reload keeps the order. Touch only; buttons
// (Refresh in the ⋯ menu) does the same for everyone else. Once a pull has started, the touch belongs
// to it until the finger lifts: moving back up only takes the indicator back in, it never scrolls the feed.
if (feedPage && "ontouchstart" in window) {
  const ARM = 64;   // how far the indicator travels before letting go refreshes
  const MAX = 110;
  const PEEK = 98;  // nearly as far as it goes (a pull of about a third of a phone screen): the chick looks out
  document.documentElement.classList.add("pull-refresh");
  const ptr = document.createElement("div");
  ptr.className = "ptr";
  ptr.setAttribute("aria-hidden", "true");
  // a ring that fills as you pull, an arrow that flips when letting go will refresh
  ptr.innerHTML = '<svg viewBox="0 0 24 24">'
    + '<circle class="ptr-track" cx="12" cy="12" r="9"/>'
    + '<circle class="ptr-ring" cx="12" cy="12" r="9" pathLength="100"/>'
    + '<path class="ptr-arrow" d="M12 8.5v7M9 12.5l3 3 3-3"/></svg>'
    // and, for a pull far past the mark, the chick from the About page's logo, looking out from below
    + '<span class="ptr-peek"><svg viewBox="0 0 40 40"><defs><radialGradient id="ptr-g-down" cx="0.38" cy="0.24" r="0.82">'
    + '<stop offset="0" stop-color="#fff8bf"/><stop offset="0.42" stop-color="#ffe14d"/><stop offset="1" stop-color="#f2a500"/>'
    + '</radialGradient></defs><g transform="translate(20 45) scale(0.56)">'
    + '<path fill="#ffd633" stroke="#d48a00" stroke-width="1.2" d="M-9 -47C-8.6 -53.5 -4.6 -57 -0.6 -56.2C-2.7 -54 -3.1 -51.4 -2.3 -48.2'
    + 'C-1.1 -52 1.9 -53.6 4.7 -52.4C2.3 -50.6 1.5 -48 1.7 -44.6Z"/>'
    + '<path fill="url(#ptr-g-down)" stroke="#d48a00" stroke-width="1.2" d="M0 -50C13.5 -50 24 -40.5 24 -28.5C24 -22.5 22 -18 19.5 -14.5'
    + 'C23.5 -9.5 25 -3 25 3V30H-25V3C-25 -3 -23.5 -9.5 -19.5 -14.5C-22 -18 -24 -22.5 -24 -28.5C-24 -40.5 -13.5 -50 0 -50Z"/>'
    + '<ellipse cx="-15.6" cy="-20.5" rx="5.4" ry="3.7" fill="#ff4fae" opacity="0.42"/>'
    + '<ellipse cx="15.6" cy="-20.5" rx="5.4" ry="3.7" fill="#ff4fae" opacity="0.42"/>'
    + [-9.2, 9.2].map((x) => `<g transform="translate(${x} -29.5)"><g class="ptr-eye"><ellipse rx="4.4" ry="5.3" fill="#1b1408"/>`
      + '<circle cx="-1.5" cy="-2.1" r="1.75" fill="#fff"/><circle cx="1.5" cy="2.3" r="0.85" fill="#fff" opacity="0.85"/></g></g>').join("")
    + '<path fill="#ff9d1c" d="M-5.4 -22.6Q0 -26 5.4 -22.6Q3 -18.6 0 -18.2Q-3 -18.6 -5.4 -22.6Z"/>'
    + '<path fill="#ffc86a" d="M-3.6 -22.9Q0 -24.9 3.6 -22.9Q0 -22.1 -3.6 -22.9Z"/></g></svg></span>';
  document.body.append(ptr);
  let start = null;    // {x, y} of the touch, while a pull may be under way
  let vertical = null; // decided on the first few pixels: a pull down, or something else (chips, scrolling)
  let travel = 0;
  let busy = false;

  const atTop = () => (document.scrollingElement || document.documentElement).scrollTop <= 0;
  const overlayOpen = () => {
    if (searching() || document.querySelector(".sheet:not([hidden])")) return true;
    try { return !!document.querySelector(":popover-open"); } catch { return false; }
  };
  const show = (distance) => {
    const wasArmed = travel >= ARM;
    travel = distance;
    const progress = Math.min(1, distance / ARM);
    ptr.style.setProperty("--pull", `${distance}px`);
    ptr.style.setProperty("--p", progress.toFixed(3));
    ptr.classList.toggle("armed", distance >= ARM);
    ptr.classList.add("pulling");
    if (!wasArmed && distance >= ARM) navigator.vibrate?.(8);  // a tick where letting go starts to count
    // once out, the chick stays until the pull eases a little, so a finger held at the edge doesn't make it flicker
    const peeking = ptr.classList.contains("peek");
    const peek = distance >= (peeking ? PEEK - 10 : PEEK);
    ptr.classList.toggle("peek", peek);
    if (peek && !peeking) navigator.vibrate?.([6, 50, 6]);
  };
  const reset = () => {
    start = vertical = null;
    travel = 0;
    ptr.classList.remove("pulling", "armed", "peek");
    ptr.style.removeProperty("--pull");
    ptr.style.removeProperty("--p");
  };

  addEventListener("touchstart", (event) => {
    if (busy || event.touches.length !== 1 || !atTop() || overlayOpen()
        || event.target.closest("input, textarea, select, [contenteditable]")) { start = null; return; }
    start = { x: event.touches[0].clientX, y: event.touches[0].clientY };
    vertical = null;
  }, { passive: true });

  // Not passive: once the pull owns the touch, the browser must not scroll with it
  addEventListener("touchmove", (event) => {
    if (!start || event.touches.length !== 1) return;
    const dx = event.touches[0].clientX - start.x;
    const dy = event.touches[0].clientY - start.y;
    if (vertical === null) {
      // The first moves decide it; a downward one at the top has nothing to scroll anyway, so hold it
      // back from the browser now (once it starts a scroll, later moves can't be stopped)
      if (Math.abs(dx) < 8 && Math.abs(dy) < 8) {
        if (dy > 0 && dy >= Math.abs(dx) && event.cancelable) event.preventDefault();
        return;
      }
      vertical = dy > 0 && dy > Math.abs(dx) * 1.5;
      if (!vertical) { start = vertical = null; return; }  // chips, a scroll: leave it to the browser
    }
    if (event.cancelable) event.preventDefault();
    show(Math.max(0, Math.min(MAX, rubber(dy))));
  }, { passive: false });

  // resistance that grows as you pull: easy at first, heavier past the mark
  function rubber(dy) {
    if (dy <= 0) return 0;
    const linear = dy * 0.55;
    return linear <= ARM ? linear : ARM + (MAX - ARM) * (1 - Math.exp(-(linear - ARM) / (MAX - ARM)));
  }

  addEventListener("touchend", () => {
    if (!start) return;
    if (travel < ARM) { reset(); return; }  // not far enough: it slides back
    start = null;
    busy = true;
    ptr.classList.remove("pulling", "armed", "peek");
    ptr.classList.add("refreshing");        // settles at the mark and spins until the new page arrives
    ptr.style.setProperty("--pull", `${ARM}px`);
    ptr.style.setProperty("--p", "1");
    refreshPage();
  });
  addEventListener("touchcancel", reset);
  // Back to a page restored from the browser's cache: no spinner left behind
  addEventListener("pageshow", () => { busy = false; ptr.classList.remove("refreshing"); reset(); });
}

// ---------- About: the logo's dot is an egg ----------
// Tap the logo seven times. Taps 4 to 6 each bring the view closer and crack the shell a bit more; the seventh
// hatches a chick, which chirps the logo's arcs, and chirps again on every tap after. Nothing is stored: leave
// the page and it is a dot again. With reduced motion the steps still happen, without the movement.
const egg = document.querySelector("[data-egg]");
if (egg) {
  const TAPS = 7;
  const CRACKS = 3;        // taps 4, 5 and 6
  const FORGET = 2500;     // ms: stray taps before the first crack don't add up over a visit
  const LINE = "The dot was an egg all along.";
  const still = matchMedia("(prefers-reduced-motion: reduce)");
  const all = (selector) => [...egg.querySelectorAll(selector)];
  const line = document.querySelector("[data-egg-line]");
  let taps = 0;
  let forget = null;
  // one-off moves on top of whatever state the CSS holds: they use translate, rotate and scale, which add to
  // an element's transform instead of replacing it
  const play = (els, frames, options) => {
    if (!still.matches) for (const el of [].concat(els)) el.animate?.(frames, options);
  };

  // A hop, the beak opens twice, the hat lags a beat behind, the arcs ring and two notes float off
  function chirp(delay = 0) {
    const ease = "cubic-bezier(0.3, 0.7, 0.2, 1)";
    const hop = (rise) => ({ translate: ["0 0", `0 ${-rise}px`, "0 0", `0 ${-rise * 0.4}px`, "0 0"], offset: [0, 0.3, 0.62, 0.8, 1] });
    play(all(".egg-chick, .egg-wings"), hop(7), { duration: 520, delay, easing: ease });
    play(all(".egg-cap"), hop(11), { duration: 560, delay: delay + 45, easing: ease });
    play(all(".egg-beak-low"), { translate: ["0 0", "0 3px", "0 0", "0 2.2px", "0 0"] }, { duration: 520, delay });
    all(".egg-arc").forEach((arc, i) => play(arc, { scale: [1, 0.94, 1.07, 1], opacity: [1, 0.5, 1, 1] },
                                             { duration: 460, delay: delay + 90 + i * 110, easing: ease }));
    all(".egg-note").forEach((note, i) => play(note, {
      opacity: [0, 1, 1, 0], offset: [0, 0.15, 0.6, 1],
      translate: ["0 0", i ? "-2.2px -4px" : "3px -3.4px", i ? "-3px -6.6px" : "5.4px -6px", i ? "-3.4px -8.4px" : "6.6px -8px"],
      rotate: ["0deg", i ? "-10deg" : "12deg", i ? "6deg" : "-6deg", i ? "-8deg" : "10deg"],
    }, { duration: 1100, delay: delay + 60 + i * 190, easing: "ease-out" }));
  }

  egg.addEventListener("click", () => {
    clearTimeout(forget);
    if (egg.classList.contains("hatched")) { navigator.vibrate?.(6); chirp(); return; }
    taps += 1;
    play(egg, { scale: [1, 0.94, 1.03, 1] }, { duration: 280, easing: "ease-out" });
    const crack = taps - (TAPS - 1 - CRACKS);  // 1..CRACKS on the taps that crack the shell
    if (taps < TAPS && crack < 1) {
      forget = setTimeout(() => { taps = 0; }, FORGET);
      return;
    }
    if (taps < TAPS) {
      egg.dataset.stage = String(crack);
      navigator.vibrate?.(6 + 4 * crack);
      const lean = 5 + 4 * crack;  // it rocks harder as it cracks
      play(all(".egg-rock"), { rotate: ["0deg", `${-lean}deg`, `${lean * 0.8}deg`, `${-lean * 0.45}deg`, `${lean * 0.2}deg`, "0deg"] },
           { duration: 520, delay: 120, easing: "ease-out" });
      return;
    }
    delete egg.dataset.stage;
    egg.classList.add("hatched");
    navigator.vibrate?.([14, 60, 10, 40, 10]);
    chirp(1250);
    if (line) {
      setTimeout(() => { line.textContent = LINE; line.classList.add("on"); }, still.matches ? 0 : 1500);
    }
  });
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
  document.querySelectorAll("[data-refresh]").forEach((b) => b.addEventListener("click", refreshPage));
});


// ---------- profile: the pencil beside an interest turns its words into a field, in the same place ----------
function closeLineEdit(form) {
  form.reset();
  form.hidden = true;
  form.previousElementSibling.hidden = false;
  form.previousElementSibling.querySelector("[data-line-edit]")?.focus({ preventScroll: true });
}

document.addEventListener("click", (event) => {
  // The pencil, or the words themselves: a bigger target on a phone
  const pencil = event.target.closest("[data-line-edit]")
    || event.target.closest(".line-view .srow-title")?.parentElement.querySelector("[data-line-edit]");
  if (pencil) {
    event.preventDefault();
    document.querySelectorAll(".line-edit:not([hidden])").forEach(closeLineEdit);
    const view = pencil.closest(".line-view");
    const form = view.nextElementSibling;
    view.hidden = true;
    form.hidden = false;
    const input = form.querySelector(".line-input");
    input.focus();
    input.setSelectionRange(input.value.length, input.value.length);
    return;
  }
  const cancel = event.target.closest("[data-line-cancel]");
  if (cancel) {
    event.preventDefault();
    closeLineEdit(cancel.closest(".line-edit"));
  }
});
document.addEventListener("keydown", (event) => {
  if (event.key === "Escape" && event.target.matches(".line-edit .line-input")) closeLineEdit(event.target.form);
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

// ---------- settings: save in place ----------
// A form with data-inplace posts in the background. The server answers as it does without JS, with a redirect to the
// settings page and ?saved=… or ?error=…; that page's content replaces this one's, so counts, names and order are the
// server's own, while the scroll position, the groups you opened or closed and the focus stay. One save at a time, in
// the order they were made, so a slower answer never overwrites a newer one.
let saveQueue = Promise.resolve();
const savingForms = new WeakSet();

function saveInPlace(form, submitter) {
  if (savingForms.has(form)) return;  // a double tap is one save
  savingForms.add(form);
  const body = new FormData(form);
  if (submitter?.name) body.append(submitter.name, submitter.value);
  // A ⋯ menu item lives in the sheet: the row it belongs to is where the page holds still and the focus returns
  const fromMenu = rowSheet?.contains(form);
  if (fromMenu) closeRowMenu();
  const origin = fromMenu ? rowButton : form;
  const page = document.querySelector(".spage");
  const anchors = [];
  for (let el = origin?.closest("[id]"); el && page?.contains(el); el = el.parentElement.closest("[id]")) anchors.push(el.id);
  const focus = focusPath(fromMenu ? rowButton : document.activeElement, page);
  const row = origin?.closest(".srow, .srow-expand");
  form.classList.add("saving");
  row?.classList.add("saving");
  saveQueue = saveQueue.then(async () => {
    try {
      const response = await fetch(form.action, { method: "POST", body });
      if (!response.ok) throw new Error(await failure(response));
      const url = new URL(response.url);
      const error = url.searchParams.get("error");
      if (error) throw new Error(error);
      const doc = new DOMParser().parseFromString(await response.text(), "text/html");
      // Saved, but the answer is another page (or not a settings page): go there, as the plain form would
      if (url.pathname !== location.pathname || !doc.querySelector(".spage")) { location.assign(url); return; }
      swapSettings(doc, anchors, focus);
      toast(doc.querySelector(".settings")?.dataset.toast || "Saved");
    } catch (e) {
      toast(e instanceof TypeError || !e.message ? "Not saved; try again" : `Not saved: ${e.message}`);
      // A switch or a select that didn't save goes back to what is saved
      if (form.matches("[data-autosubmit]") && form.isConnected) {
        form.reset();
        form.querySelectorAll("select").forEach(showChoice);
      }
    } finally {
      savingForms.delete(form);
      form.classList.remove("saving");
      row?.classList.remove("saving");
    }
  });
}

// The reason a save was refused: FastAPI's {"detail": …}, the login check's {"error": …}, else the status
async function failure(response) {
  try {
    const data = await response.json();
    if (typeof (data.detail || data.error) === "string") return data.detail || data.error;
  } catch { /* not JSON */ }
  return response.statusText || `error ${response.status}`;
}

// Where the focus was, as selectors that find the same control on the new page: the control by its form and name
// inside the nearest element with an id, else that row's ⋯ button
function focusPath(el, page) {
  if (!el || !page?.contains(el)) return [];
  const host = el.closest("[id]");
  const scope = host && page.contains(host) ? `#${CSS.escape(host.id)} ` : "";
  const paths = [];
  const action = el.form?.getAttribute("action");
  if (action) {
    const form = `${scope}form[action="${CSS.escape(action)}"]`;
    paths.push(el.name ? `${form} [name="${CSS.escape(el.name)}"]` : `${form} ${el.localName}`);
  }
  if (scope) paths.push(`${scope}.rowmenu-btn`);
  return paths;
}

function swapSettings(doc, anchors, focus) {
  const page = document.querySelector(".spage");
  const fresh = doc.querySelector(".spage");
  fresh.querySelectorAll("noscript").forEach((n) => n.remove());  // parsed without scripting, they would show
  // Groups stay as you left them; a new one opens or not as the server says
  for (const details of fresh.querySelectorAll("details[id]")) {
    const old = document.getElementById(details.id);
    if (old?.tagName === "DETAILS") details.open = old.open;
  }
  const tops = new Map(anchors.map((id) => [id, document.getElementById(id)?.getBoundingClientRect().top]));
  page.replaceChildren(...fresh.childNodes);
  // The settings menu shows counts (feeds, on trial, watches): take the new rows, not the whole menu, which may be scrolled
  for (const link of doc.querySelectorAll(".snav a.srow")) {
    document.querySelector(`.snav a.srow[href="${CSS.escape(link.getAttribute("href"))}"]`)?.replaceWith(link);
  }
  initChipboxes(page);
  // Hold the page still: the innermost element around the change that is still shown stays where it was on screen
  for (const id of anchors) {
    const el = document.getElementById(id);
    if (!el?.getClientRects().length || tops.get(id) === undefined) continue;
    window.scrollBy({ top: el.getBoundingClientRect().top - tops.get(id), behavior: "instant" });
    break;
  }
  for (const path of focus) {
    const el = page.querySelector(path);
    if (!el?.getClientRects().length) continue;
    el.focus({ preventScroll: true });
    break;
  }
}

// Mute / always-show words: the textarea (one word per line) becomes chips; every change saves through the form's data-autosave
function initChipboxes(root) {
  root.querySelectorAll("[data-chipbox]").forEach((box) => {
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
}
initChipboxes(document);

// A feed you came back to (#feed-…) may sit in a folded group: open it and bring the row into view
function openTargetGroup() {
  const target = document.querySelector(":target");
  const group = target?.closest("details");
  if (group && !group.open) { group.open = true; target.scrollIntoView({ block: "center" }); }
}
document.addEventListener("DOMContentLoaded", openTargetGroup);
addEventListener("hashchange", openTargetGroup);
