// NewsNest project page: theme toggle, copy buttons, reveal-on-scroll, the swipe demo and the star count.
document.documentElement.classList.add("js");

const store = {
  get(k) { try { return localStorage.getItem(k); } catch (e) { return null; } },
  set(k, v) { try { localStorage.setItem(k, v); } catch (e) {} },
};

// Theme: follow the system until the visitor picks one.
document.querySelector("[data-theme-toggle]")?.addEventListener("click", () => {
  const root = document.documentElement;
  const dark = root.dataset.theme ? root.dataset.theme === "dark" : matchMedia("(prefers-color-scheme: dark)").matches;
  root.dataset.theme = dark ? "light" : "dark";
  store.set("nn-theme", root.dataset.theme);
});

// The nav gets its glass background once the page scrolls.
const nav = document.querySelector(".nav");
const onScroll = () => nav.classList.toggle("scrolled", scrollY > 12);
addEventListener("scroll", onScroll, { passive: true });
onScroll();

// Copy buttons: data-copy holds the text, data-copy-from names an element to copy from.
for (const btn of document.querySelectorAll("[data-copy], [data-copy-from]")) {
  btn.addEventListener("click", async () => {
    const text = btn.dataset.copy ?? document.getElementById(btn.dataset.copyFrom).innerText.replace(/ +#.*$/gm, "");
    const label = btn.querySelector("[data-copy-label]");
    try {
      await navigator.clipboard.writeText(text);
      label.textContent = "Copied";
      label.classList.add("copied");
    } catch (e) {
      label.textContent = "Press ⌘C";
    }
    setTimeout(() => { label.textContent = "Copy"; label.classList.remove("copied"); }, 1600);
  });
}

// Reveal sections, and start the tile demos, as they scroll into view.
const io = new IntersectionObserver((entries) => {
  for (const e of entries) {
    if (e.isIntersecting) { e.target.classList.add("in"); io.unobserve(e.target); }
  }
}, { rootMargin: "0px 0px -12% 0px", threshold: 0.1 });
document.querySelectorAll(".reveal").forEach((el) => io.observe(el));

// Hero phone: tap "Get key points", let them open, close again, repeat, while the phone is on screen.
const feed = document.querySelector("[data-hero-feed]");
if (feed && !matchMedia("(prefers-reduced-motion: reduce)").matches) {
  const kp = feed.querySelector(".kp"), btn = kp.querySelector(".sumbtn");
  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
  let visible = false, running = false;
  const loop = async () => {
    running = true;
    while (visible) {
      await sleep(1800);
      btn.classList.add("press");
      await sleep(900);
      btn.classList.remove("press");
      kp.classList.add("open"); feed.classList.add("open");
      await sleep(4800);
      kp.classList.remove("open"); feed.classList.remove("open");
      await sleep(1200);
    }
    running = false;
  };
  new IntersectionObserver(([e]) => {
    visible = e.isIntersecting;
    if (visible && !running) loop();
  }).observe(feed);
}

// Swipe demo: drag a card, or use the buttons.
const deck = document.querySelector("[data-deck]");
if (deck) {
  const stack = deck.querySelector(".deck-stack");
  const summary = deck.querySelector("[data-deck-summary]");
  let liked = 0, skipped = 0;
  const pile = () => [...stack.querySelectorAll("[data-card]:not(.gone)")].reverse();
  const top = () => pile()[0];
  const restack = () => pile().forEach((c, i) => c.style.setProperty("--i", Math.min(i, 3)));
  restack();

  const fling = (card, dir) => {
    if (!card) return;
    card.classList.remove("dragging");
    card.dataset.verdict = dir === "right" ? "More" : "Skip";
    card.style.transform = `translate(${dir === "right" ? 140 : -140}%, -6%) rotate(${dir === "right" ? 18 : -18}deg)`;
    card.classList.add("gone");
    dir === "right" ? liked++ : skipped++;
    restack();
    if (!top()) summary.textContent = `${liked} more like this, ${skipped} skipped. Your feed just got sharper.`;
  };

  deck.querySelectorAll("[data-swipe]").forEach((b) => b.addEventListener("click", () => fling(top(), b.dataset.swipe)));
  deck.querySelector("[data-deck-reset]").addEventListener("click", () => {
    liked = skipped = 0;
    for (const c of stack.querySelectorAll("[data-card]")) {
      c.classList.remove("gone"); c.style.transform = ""; delete c.dataset.verdict;
    }
    restack();
  });

  let start = null;
  stack.addEventListener("pointerdown", (e) => {
    const card = e.target.closest("[data-card]");
    if (!card || card !== top()) return;
    start = { x: e.clientX, y: e.clientY, card };
    card.setPointerCapture(e.pointerId);
    card.classList.add("dragging");
  });
  stack.addEventListener("pointermove", (e) => {
    if (!start) return;
    const dx = e.clientX - start.x, dy = (e.clientY - start.y) * 0.3;
    start.card.style.transform = `translate(${dx}px, ${dy}px) rotate(${dx / 14}deg)`;
    if (Math.abs(dx) > 40) start.card.dataset.verdict = dx > 0 ? "More" : "Skip";
    else delete start.card.dataset.verdict;
  });
  const end = (e) => {
    if (!start) return;
    const dx = e.clientX - start.x, card = start.card;
    start = null;
    if (Math.abs(dx) > 90) fling(card, dx > 0 ? "right" : "left");
    else { card.classList.remove("dragging"); card.style.transform = ""; delete card.dataset.verdict; }
  };
  stack.addEventListener("pointerup", end);
  stack.addEventListener("pointercancel", end);
}

// GitHub star count, if the API answers; the button works without it.
fetch("https://api.github.com/repos/Lucifix/newsnest")
  .then((r) => (r.ok ? r.json() : null))
  .then((repo) => {
    const el = document.querySelector("[data-stars]");
    if (repo && el && repo.stargazers_count > 0) {
      el.textContent = repo.stargazers_count.toLocaleString("en");
      el.hidden = false;
    }
  })
  .catch(() => {});
