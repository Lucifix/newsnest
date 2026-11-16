// Loaded before app.js on every page of the demo: nothing is ever sent anywhere.
// Feed actions (like, save, mark read, ...) get a fake "ok" so the page updates as usual;
// everything else (settings, profile, ...) is refused with the message below.
(() => {
  const MSG = "This demo doesn't save changes";

  function say(message) {
    if (typeof window.toast === "function") { window.toast(message); return; }
    let note = document.querySelector(".demo-toast");
    if (!note) {
      note = document.createElement("div");
      note.className = "demo-toast";
      note.setAttribute("role", "status");
      document.body.append(note);
    }
    note.textContent = message;
    note.hidden = false;
    clearTimeout(say.timer);
    say.timer = setTimeout(() => { note.hidden = true; }, 3000);
  }

  function answer(status, payload) {
    const response = new Response(JSON.stringify(payload), {
      status,
      statusText: status === 200 ? "OK" : MSG,
      headers: { "Content-Type": "application/json" },
    });
    // app.js reads response.url; a constructed Response has an empty one
    Object.defineProperty(response, "url", { value: location.href });
    return response;
  }

  const realFetch = window.fetch.bind(window);
  window.fetch = (input, init) => {
    const method = String((init && init.method) || (input instanceof Request ? input.method : "GET")).toUpperCase();
    if (method === "GET" || method === "HEAD") return realFetch(input, init);
    const url = new URL(input instanceof Request ? input.url : String(input), location.href);
    const path = url.pathname.slice(url.pathname.indexOf("/api/") >= 0 ? url.pathname.indexOf("/api/") : 0);
    const rated = path.match(/^\/api\/items\/(\d+)\/rate$/);
    if (rated) {
      const body = init && init.body instanceof FormData ? init.body : null;
      return Promise.resolve(answer(200, { id: Number(rated[1]), rating: Number(body ? body.get("value") : 0) }));
    }
    if (/^\/api\/(saved|read|seen|attention|feedback)$/.test(path) || /^\/api\/items\/\d+\/expanded$/.test(path)) {
      return Promise.resolve(answer(200, {}));
    }
    return Promise.resolve(answer(403, { error: MSG }));
  };

  navigator.sendBeacon = () => true;

  if (navigator.serviceWorker) {
    try { navigator.serviceWorker.register = () => Promise.reject(new Error("demo")); } catch { /* read-only */ }
  }

  // Forms that app.js leaves to the browser would POST to a server that is not there
  window.addEventListener("submit", (event) => {
    const form = event.target;
    if (!(form instanceof HTMLFormElement)) return;
    const off = form.dataset.demoOff;
    const blocked = form.method.toLowerCase() === "post" && !form.matches(".rate, .readform, .readall, [data-inplace]");
    if (!off && !blocked) return;
    event.preventDefault();
    event.stopImmediatePropagation();
    say(off || MSG);
  }, true);

  window.addEventListener("click", (event) => {
    const link = event.target instanceof Element ? event.target.closest("a[data-demo-off]") : null;
    if (!link) return;
    event.preventDefault();
    event.stopImmediatePropagation();
    say(link.dataset.demoOff);
  }, true);
})();
