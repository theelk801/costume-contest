/* Shared helpers for the contest pages. Exposes window.Contest. */
(function () {
  "use strict";

  function cleanUrl(u) { return String(u || "").replace(/\/+$/, ""); }
  var API = cleanUrl(window.CONTEST_API);
  var mem = {};

  /* Where is the backend? A backend.json next to the pages ({"api": "https://..."}) wins, so the address can
     change without touching config.js or the QR code. If it's missing, config.js's CONTEST_API is used. */
  var resolving = null;
  function resolveApi(force) {
    if (resolving && !force) return resolving;
    resolving = (async function () {
      try {
        var r = await fetch("backend.json?t=" + Date.now(), { cache: "no-store" });
        if (r.ok) {
          var j = await r.json();
          if (j && typeof j.api === "string" && /^https?:\/\//i.test(j.api)) return cleanUrl(j.api);
        }
      } catch (e) { /* no backend.json: fall back to config.js */ }
      return cleanUrl(window.CONTEST_API);
    })().then(function (a) { API = a; return a; });
    return resolving;
  }

  function store(key) {
    return {
      get: function () {
        var v = null;
        try { v = localStorage.getItem(key); } catch (e) {}
        return v || mem[key] || null;
      },
      set: function (v) {
        try { localStorage.setItem(key, v); } catch (e) { mem[key] = v; }
      },
      clear: function () {
        try { localStorage.removeItem(key); } catch (e) {}
        delete mem[key];
      }
    };
  }
  var keyStore = store("contest:adminKey");
  var voterStore = store("contest:voter");

  function randomId() {
    if (window.crypto && crypto.randomUUID) return crypto.randomUUID();
    var a = new Uint8Array(16);
    if (window.crypto && crypto.getRandomValues) crypto.getRandomValues(a);
    else for (var i = 0; i < a.length; i++) a[i] = Math.floor(Math.random() * 256);
    return Array.prototype.map.call(a, function (b) { return ("0" + b.toString(16)).slice(-2); }).join("");
  }

  /* One anonymous id per browser. Honor system: clearing site data gives a new id. */
  function voterId() {
    var id = voterStore.get();
    if (!id || !/^[A-Za-z0-9_-]{8,64}$/.test(id)) {
      id = randomId();
      voterStore.set(id);
    }
    return id;
  }

  function apiError(message, status) {
    var e = new Error(message);
    e.status = status;
    return e;
  }

  /* api(path, {method, body, admin}) -> parsed JSON. The host passcode is sent only when admin is true. */
  async function api(path, opts) {
    opts = opts || {};
    var headers = { "ngrok-skip-browser-warning": "1" };
    var init = { method: opts.method || "GET", headers: headers };
    if (opts.body !== undefined) {
      headers["Content-Type"] = "application/json";
      init.body = JSON.stringify(opts.body);
    }
    var key = keyStore.get();
    if (opts.admin && key) headers["X-Admin-Key"] = key;
    await resolveApi();
    var res;
    try { res = await fetch(API + path, init); }
    catch (e) {
      /* Maybe the backend moved (a restarted tunnel gets a new address). Look again; if backend.json now says
         something different, reload so every photo and request uses the new address. */
      var before = API;
      await resolveApi(true);
      if (API !== before) { location.reload(); }
      throw apiError("Can't reach the contest server. Check that it's running and that this page points at it.", 0);
    }
    var data = null;
    try { data = await res.json(); } catch (e) {}
    if (!res.ok) throw apiError((data && data.error) || "Something went wrong (" + res.status + ").", res.status);
    return data;
  }

  function photoUrl(p) { return p ? API + p : ""; }
  function pad(n) { return String(n).padStart(2, "0"); }
  function plural(n, one, many) { return n === 1 ? one : many; }

  /* ["A"] -> "A", ["A","B"] -> "A & B", ["A","B","C"] -> "A, B & C" */
  function peopleLabel(names) {
    names = (names || []).filter(Boolean);
    if (names.length <= 1) return names[0] || "A mystery guest";
    return names.slice(0, -1).join(", ") + " & " + names[names.length - 1];
  }

  /* Tiny DOM builder. Text always goes in as text, never as HTML. */
  function h(tag, attrs) {
    var el = document.createElement(tag);
    if (attrs) {
      Object.keys(attrs).forEach(function (k) {
        var v = attrs[k];
        if (v === null || v === undefined || v === false) return;
        if (k === "class") el.className = v;
        else if (k === "text") el.textContent = v;
        else if (k.indexOf("on") === 0) el.addEventListener(k.slice(2), v);
        else if (v === true) el.setAttribute(k, "");
        else el.setAttribute(k, v);
      });
    }
    for (var i = 2; i < arguments.length; i++) {
      var kid = arguments[i];
      if (kid === null || kid === undefined || kid === false) continue;
      el.append(kid.nodeType ? kid : document.createTextNode(String(kid)));
    }
    return el;
  }

  var toastTimer, toastEl;
  function toast(msg) {
    if (!toastEl) {
      toastEl = h("div", { class: "toast", role: "status", hidden: true });
      document.body.append(h("div", { class: "toast-wrap" }, toastEl));
    }
    toastEl.textContent = msg;
    toastEl.hidden = false;
    clearTimeout(toastTimer);
    toastTimer = setTimeout(function () { toastEl.hidden = true; }, 3800);
  }

  /* Re-run fn every `ms` while the tab is visible, and again when the tab comes back. */
  function poll(fn, ms) {
    var t;
    function tick() {
      clearTimeout(t);
      if (document.hidden) { t = setTimeout(tick, ms); return; }
      Promise.resolve().then(fn).catch(function () {}).then(function () { t = setTimeout(tick, ms); });
    }
    document.addEventListener("visibilitychange", function () { if (!document.hidden) tick(); });
    tick();
  }

  function loadImage(file) {
    return new Promise(function (resolve, reject) {
      var url = URL.createObjectURL(file);
      var im = new Image();
      im.onload = function () { URL.revokeObjectURL(url); resolve(im); };
      im.onerror = function () { URL.revokeObjectURL(url); reject(new Error("Couldn't read that photo. Try another one.")); };
      im.src = url;
    });
  }

  /* Turn a camera photo into {photo, thumb}: two JPEG data URLs (full size and a small one for the ballot). */
  async function resizePhoto(file) {
    if (!file.type || file.type.indexOf("image/") !== 0) throw new Error("That file is not an image.");
    var src;
    try { src = await createImageBitmap(file, { imageOrientation: "from-image" }); }
    catch (e) { src = await loadImage(file); }
    var sw = src.naturalWidth || src.width, sh = src.naturalHeight || src.height;
    if (!sw || !sh) throw new Error("Couldn't read that photo. Try another one.");

    function render(maxSide, quality) {
      var scale = Math.min(1, maxSide / Math.max(sw, sh));
      var w = Math.max(1, Math.round(sw * scale)), hh = Math.max(1, Math.round(sh * scale));
      var cv = document.createElement("canvas");
      cv.width = w; cv.height = hh;
      var ctx = cv.getContext("2d");
      ctx.fillStyle = "#ffffff"; ctx.fillRect(0, 0, w, hh);
      ctx.drawImage(src, 0, 0, w, hh);
      return cv.toDataURL("image/jpeg", quality);
    }

    var sizes = [1400, 1100, 800], photo = null;
    for (var i = 0; i < sizes.length && !photo; i++) {
      var url = render(sizes[i], 0.85);
      if (url.length <= 7000000) photo = url;
    }
    if (!photo) throw new Error("That photo is too large to upload. Try a different one.");
    return { photo: photo, thumb: render(480, 0.8) };
  }

  window.Contest = {
    api: api, photoUrl: photoUrl, pad: pad, plural: plural, peopleLabel: peopleLabel,
    h: h, toast: toast, poll: poll, resizePhoto: resizePhoto, voterId: voterId,
    adminKey: function () { return keyStore.get(); },
    setAdminKey: function (k) { keyStore.set(k); },
    clearAdminKey: function () { keyStore.clear(); }
  };
})();
