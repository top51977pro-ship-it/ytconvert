"use strict";
// "Cut a part": a preview player plus a two-handle timeline. Exposes
// window.Cut for app.js - load(info) for a new video, range() for the
// selection (or null when the whole video is wanted) and onChange(fn).

(() => {
  const $ = (id) => document.getElementById(id);
  const MIN_LEN = 1;

  const st = { on: false, duration: 0, start: 0, end: 0, url: null, loadedUrl: null, ready: false, stopAt: null, listeners: [] };

  function fmt(s) {
    s = Math.max(0, s);
    const whole = Math.floor(s);
    const h = Math.floor(whole / 3600), m = Math.floor((whole % 3600) / 60), sec = whole % 60;
    const tenth = Math.floor((s - whole) * 10 + 1e-6);
    const base = h ? `${h}:${String(m).padStart(2, "0")}:${String(sec).padStart(2, "0")}` : `${m}:${String(sec).padStart(2, "0")}`;
    return tenth ? `${base}.${tenth}` : base;
  }

  // "1:05", "65", "1:02:03", "1:05.5" -> seconds
  function parse(text) {
    const t = String(text).trim().replace(",", ".");
    if (!/^\d+(\.\d+)?(:\d{1,2}(\.\d+)?){0,2}$/.test(t)) return null;
    return t.split(":").reduce((acc, p) => acc * 60 + parseFloat(p), 0);
  }

  const emit = () => st.listeners.forEach((fn) => fn());

  // ------------------------------------------------------------ timeline

  function paint() {
    const d = st.duration || 1;
    const a = (st.start / d) * 100, b = (st.end / d) * 100;
    $("hStart").style.left = a + "%";
    $("hEnd").style.left = b + "%";
    $("tlRange").style.left = a + "%";
    $("tlRange").style.width = Math.max(0, b - a) + "%";
    $("bStart").textContent = fmt(st.start);
    $("bEnd").textContent = fmt(st.end);
    if (document.activeElement !== $("tStart")) $("tStart").value = fmt(st.start);
    if (document.activeElement !== $("tEnd")) $("tEnd").value = fmt(st.end);
    $("cutLen").textContent = fmt(st.end - st.start);
  }

  function set(which, t, { seek = false } = {}) {
    const d = st.duration;
    t = Math.round(Math.min(Math.max(t, 0), d) * 10) / 10;
    if (which === "start") st.start = Math.min(t, st.end - MIN_LEN);
    else st.end = Math.max(t, st.start + MIN_LEN);
    st.start = Math.max(0, st.start);
    st.end = Math.min(d, st.end);
    paint();
    if (seek) seekTo(which === "start" ? st.start : st.end);
    emit();
  }

  function timeAt(clientX) {
    const r = $("timeline").getBoundingClientRect();
    return ((clientX - r.left) / r.width) * st.duration;
  }

  function drag(which) {
    return (e) => {
      e.preventDefault();
      const handle = which === "start" ? $("hStart") : $("hEnd");
      handle.classList.add("dragging");
      handle.setPointerCapture(e.pointerId);
      const move = (ev) => set(which, timeAt(ev.clientX), { seek: true });
      const up = () => {
        handle.classList.remove("dragging");
        handle.removeEventListener("pointermove", move);
        handle.removeEventListener("pointerup", up);
        handle.removeEventListener("pointercancel", up);
      };
      handle.addEventListener("pointermove", move);
      handle.addEventListener("pointerup", up);
      handle.addEventListener("pointercancel", up);
    };
  }

  // Keyboard: arrows nudge by 1 s, shift+arrows by 0.1 s.
  function keys(which) {
    return (e) => {
      const step = e.shiftKey ? 0.1 : 1;
      const cur = which === "start" ? st.start : st.end;
      if (e.key === "ArrowLeft" || e.key === "ArrowDown") { e.preventDefault(); set(which, cur - step, { seek: true }); }
      if (e.key === "ArrowRight" || e.key === "ArrowUp") { e.preventDefault(); set(which, cur + step, { seek: true }); }
    };
  }

  // ------------------------------------------------------------- player
  //
  // A light 360p copy streamed through the app (/api/preview). YouTube's
  // embeddable player refuses many videos, this works for all of them.

  const pv = $("pv");

  function note(text, spin) {
    $("playerNote").innerHTML = spin ? `<span class="spin"></span>${text}` : text || "";
    $("playerNote").hidden = !text;
  }

  async function ensurePlayer() {
    if (!st.url || st.loadedUrl === st.url) return;
    st.loadedUrl = st.url;
    st.ready = false;
    pv.removeAttribute("src");
    note("Loading preview…", true);
    try {
      const res = await fetch("/api/preview", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ url: st.url }) });
      const data = await res.json();
      if (!res.ok) throw new Error(data.error || "no preview");
      if (st.loadedUrl !== st.url) return; // another video was pasted meanwhile
      pv.src = data.src;
    } catch {
      st.loadedUrl = null;
      note("Preview isn't available for this video - drag the handles or type the times.");
    }
  }

  pv.addEventListener("loadedmetadata", () => { st.ready = true; note(""); pv.currentTime = st.start; });
  pv.addEventListener("error", () => { if (pv.getAttribute("src")) { st.loadedUrl = null; note("The preview couldn't play - drag the handles or type the times."); } });
  pv.addEventListener("pause", () => { st.stopAt = null; $("previewCut").classList.remove("playing"); });

  function seekTo(t) {
    if (st.ready && !isNaN(pv.duration)) pv.currentTime = t;
  }

  function now() {
    return st.ready ? pv.currentTime : null;
  }

  // Playhead + "stop at the end of the selection" while previewing.
  setInterval(() => {
    if (!st.on) return;
    const t = now();
    const head = $("tlHead");
    if (t == null) { head.style.opacity = 0; return; }
    head.style.opacity = 1;
    head.style.left = (t / (st.duration || 1)) * 100 + "%";
    if (st.stopAt != null && t >= st.stopAt) {
      pv.pause();
      pv.currentTime = st.stopAt;
    }
  }, 100);

  function preview() {
    if (!st.ready) return;
    pv.currentTime = st.start;
    pv.play().then(() => {
      st.stopAt = st.end;
      $("previewCut").classList.add("playing");
    }).catch(() => {});
  }

  // --------------------------------------------------------------- wiring

  $("hStart").addEventListener("pointerdown", drag("start"));
  $("hEnd").addEventListener("pointerdown", drag("end"));
  $("hStart").addEventListener("keydown", keys("start"));
  $("hEnd").addEventListener("keydown", keys("end"));

  // Clicking the bare track moves whichever handle is closer.
  $("timeline").addEventListener("pointerdown", (e) => {
    if (e.target.closest(".tl-handle")) return;
    const t = timeAt(e.clientX);
    set(Math.abs(t - st.start) <= Math.abs(t - st.end) ? "start" : "end", t, { seek: true });
  });

  for (const which of ["start", "end"]) {
    const input = which === "start" ? $("tStart") : $("tEnd");
    const commit = () => {
      const t = parse(input.value);
      input.classList.toggle("bad", t == null);
      if (t != null) set(which, t, { seek: true });
    };
    input.addEventListener("change", commit);
    input.addEventListener("keydown", (e) => { if (e.key === "Enter") { commit(); input.blur(); } });
    input.addEventListener("blur", () => { if (!input.classList.contains("bad")) paint(); });
  }

  $("setStart").addEventListener("click", () => { const t = now(); if (t != null) set("start", t); });
  $("setEnd").addEventListener("click", () => { const t = now(); if (t != null) set("end", t); });
  $("previewCut").addEventListener("click", preview);

  $("cutOn").addEventListener("change", (e) => {
    st.on = e.target.checked;
    $("cutPanel").hidden = !st.on;
    if (st.on) {
      ensurePlayer();
      requestAnimationFrame(() => $("cutPanel").classList.add("open"));
    } else {
      $("cutPanel").classList.remove("open");
      pv.pause();
    }
    emit();
  });

  // ------------------------------------------------------------------ api

  window.Cut = {
    load(info) {
      st.duration = info.duration || 0;
      st.start = 0;
      st.end = st.duration;
      st.stopAt = null;
      st.url = info.url || null;
      pv.pause();
      $("cutRow").hidden = !st.duration;
      if (!st.duration) { $("cutOn").checked = false; st.on = false; $("cutPanel").hidden = true; }
      $("tlTotal").textContent = fmt(st.duration);
      paint();
      if (st.on) ensurePlayer();
    },
    // null = the whole video
    range() {
      if (!st.on || !st.duration) return null;
      if (st.start <= 0.05 && st.end >= st.duration - 0.05) return null;
      return { start: st.start, end: st.end };
    },
    // the switch is on (the range may still be the whole video)
    active() { return st.on && st.duration > 0; },
    fmt,
    onChange(fn) { st.listeners.push(fn); },
  };
})();
