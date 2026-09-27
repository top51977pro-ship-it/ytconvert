"use strict";

const $ = (id) => document.getElementById(id);
// The same UI runs in the desktop app and on the website (online.html sets
// YTC_ONLINE): online there is no folder to open, files come back as browser
// downloads, and each visitor is told apart by a random session id.
const ONLINE = !!window.YTC_ONLINE;
const SESSION = ONLINE ? (() => {
  let s = null;
  try { s = localStorage.getItem("ytc.session"); } catch { /* storage off */ }
  if (!s || !/^[A-Za-z0-9_-]{16,64}$/.test(s)) {
    s = Array.from(crypto.getRandomValues(new Uint8Array(18)), (b) => "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_"[b & 63]).join("");
    try { localStorage.setItem("ytc.session", s); } catch { /* per-tab session then */ }
  }
  return s;
})() : null;
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

const MP3_RATES = [320, 256, 192, 128];
const ACTIVE = new Set(["queued", "preparing", "downloading", "processing"]);
const CODEC = { avc1: "H.264", h264: "H.264", vp9: "VP9", vp09: "VP9", av01: "AV1", hev1: "HEVC", hvc1: "HEVC" };

const state = {
  info: null,
  mode: "mp4",
  videoIdx: 0,
  bitrate: 320,
  compat: false,
  settings: null,
  jobs: [],
  lastUrl: "",
};

// ------------------------------------------------------------ helpers

async function api(path, body) {
  const headers = SESSION ? { "X-YTC-Session": SESSION } : {};
  const res = await fetch(path, body === undefined ? { headers } : {
    method: "POST",
    headers: { ...headers, "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.error || `Request failed (${res.status})`);
  return data;
}

function fmtBytes(n) {
  if (!n) return "";
  if (n >= 1e9) return (n / 1e9).toFixed(2) + " GB";
  if (n >= 1e6) return (n / 1e6).toFixed(n >= 1e8 ? 0 : 1) + " MB";
  return Math.max(1, Math.round(n / 1e3)) + " KB";
}

function fmtTime(s) {
  if (s == null || !isFinite(s)) return "";
  s = Math.round(s);
  const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60), sec = s % 60;
  return h ? `${h}:${String(m).padStart(2, "0")}:${String(sec).padStart(2, "0")}` : `${m}:${String(sec).padStart(2, "0")}`;
}

function thumbUrl(u) {
  return u ? `/api/thumb?u=${encodeURIComponent(u)}` : "icon.svg";
}

let toastTimer;
function toast(msg, ms = 3200) {
  const t = $("toast");
  t.textContent = msg;
  t.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => (t.hidden = true), ms);
}

function looksLikeUrl(s) {
  return /^(https?:\/\/)?([\w-]+\.)*(youtube\.com|youtu\.be|youtube-nocookie\.com)\//i.test(s.trim())
      || /^https?:\/\//i.test(s.trim());
}

// ------------------------------------------------------------ fetch info

async function fetchInfo(url) {
  url = url.trim();
  if (!url) return;
  const btn = $("fetchBtn");
  $("urlError").hidden = true;
  btn.disabled = true;
  btn.classList.add("loading");
  try {
    const info = await api("/api/info", { url });
    if (info.is_live) throw new Error("This is a live stream - wait until it ends, then download it.");
    if (!info.video.length && !info.audio.fid) throw new Error("No downloadable streams were found.");
    state.info = info;
    state.lastUrl = url;
    state.videoIdx = 0; // highest quality first
    showPicker();
  } catch (e) {
    $("urlError").textContent = e.message;
    $("urlError").hidden = false;
  } finally {
    btn.disabled = false;
    btn.classList.remove("loading");
  }
}

// ------------------------------------------------------------ picker

function showPicker() {
  const i = state.info;
  $("picker").hidden = false;
  $("thumb").src = thumbUrl(i.thumbnail);
  $("duration").textContent = fmtTime(i.duration);
  $("duration").hidden = !i.duration;
  $("title").textContent = i.title;
  $("title").title = i.title;
  $("channel").textContent = i.channel;
  if (!i.video.length) state.mode = "mp3";
  Cut.load(i);
  renderOptions();
}

function currentVideo() {
  return state.info.video[state.videoIdx] || state.info.video[0];
}

// A cut is re-encoded to H.264, which needs more bits than YouTube's VP9/AV1
// for the same picture - scale the estimate by how efficient the source was.
const H264_COST = { av01: 1.7 * 1.25, vp9: 1.5 * 1.25, vp09: 1.5 * 1.25, avc1: 1.25, h264: 1.25 };

function clipVideoSize(v) {
  return v.vsize * clipShare() * (H264_COST[v.codec] || 1.5) + (state.info.audio.mp4_size || 0) * clipShare();
}

// Share of the video that will be downloaded (1 = all of it).
function clipShare() {
  const r = Cut.range();
  return r && state.info.duration ? (r.end - r.start) / state.info.duration : 1;
}

function renderOptions() {
  const i = state.info;
  const share = clipShare();
  const cutting = Cut.active();
  document.querySelectorAll(".seg-btn").forEach((b) => b.classList.toggle("on", b.dataset.mode === state.mode));
  const chips = $("chips");
  chips.innerHTML = "";

  if (state.mode === "mp4") {
    $("optTitle").textContent = "Video quality";
    const top = i.video[0];
    $("optHint").textContent = top && top.p < 2160
      ? `YouTube's highest for this video is ${top.label} - no 4K upload exists`
      : "Original YouTube streams - no re-compression";
    i.video.forEach((v, idx) => {
      const size = share < 1 ? clipVideoSize(v) : (state.compat ? v.compat_size : v.size);
      const codec = state.compat || share < 1 ? "H.264" : (CODEC[v.codec] || v.codec.toUpperCase());
      const reenc = (state.compat && v.compat_reencode) || share < 1;
      const b = document.createElement("button");
      b.className = "chip" + (idx === state.videoIdx ? " on" : "");
      b.innerHTML = `
        <span class="row"><span class="q">${esc(v.label)}</span>${v.tag ? `<span class="tag">${esc(v.tag)}</span>` : ""}</span>
        <span class="sub">${idx === 0 ? '<span class="best">Best</span> · ' : ""}${esc(codec)}${size ? " · " + (reenc ? "~" : "") + fmtBytes(size) : ""}</span>`;
      b.onclick = () => { state.videoIdx = idx; renderOptions(); };
      chips.appendChild(b);
    });
    // A cut is re-encoded frame-exact to H.264 anyway, so the switch is moot.
    $("compatRow").hidden = cutting;
    $("compat").checked = state.compat;
    const v = currentVideo();
    const note = $("compatNote");
    if (state.compat && v.compat_reencode) {
      note.innerHTML = `<span class="warn">YouTube has no H.264 at ${esc(v.label)} - it will be re-encoded on your GPU after downloading (takes a few minutes).</span>`;
    } else {
      note.textContent = "For WhatsApp, iPhone, TVs and editing apps. Off = the untouched original stream.";
    }
  } else {
    $("optTitle").textContent = "MP3 bitrate";
    const a = i.audio;
    $("optHint").textContent = a.abr ? `Source audio: ${a.codec.toUpperCase()} ${a.abr} kbps - 320 keeps every bit of it` : "";
    MP3_RATES.forEach((r) => {
      const b = document.createElement("button");
      b.className = "chip" + (r === state.bitrate ? " on" : "");
      const size = i.duration ? (r * 1000 / 8) * i.duration * share : 0;
      b.innerHTML = `
        <span class="row"><span class="q">${r} kbps</span>${r === 320 ? '<span class="tag">MAX</span>' : ""}</span>
        <span class="sub">${r === 320 ? '<span class="best">Best</span> · ' : ""}${size ? "~" + fmtBytes(size) : "MP3"}</span>`;
      b.onclick = () => { state.bitrate = r; renderOptions(); };
      chips.appendChild(b);
    });
    $("compatRow").hidden = true;
  }
  renderSummary();
}

function renderSummary() {
  const i = state.info;
  const r = Cut.range();
  const share = clipShare();
  const part = r ? ` · <span class="cut-tag">✂ ${Cut.fmt(r.start)}–${Cut.fmt(r.end)}</span>` : "";
  let html;
  if (state.mode === "mp4") {
    const v = currentVideo();
    const size = r ? clipVideoSize(v) : (state.compat ? v.compat_size : v.size);
    const codec = state.compat || r ? "H.264" : (CODEC[v.codec] || v.codec);
    html = `<b>MP4</b> · ${esc(v.label)} · ${esc(codec)} + AAC${part}${size ? ` · <b>${r ? "~" : ""}${fmtBytes(size)}</b>` : ""}`;
  } else {
    const size = i.duration ? (state.bitrate * 1000 / 8) * i.duration * share : 0;
    html = `<b>MP3</b> · ${state.bitrate} kbps · cover art${part}${size ? ` · <b>~${fmtBytes(size)}</b>` : ""}`;
  }
  $("summary").innerHTML = html;
}

async function startDownload() {
  const i = state.info;
  if (!i) return;
  const req = {
    url: i.url || state.lastUrl,
    mode: state.mode,
    title: i.title,
    thumbnail: i.thumbnail,
    compat: state.compat,
  };
  if (state.mode === "mp4") {
    const v = currentVideo();
    const fid = state.compat ? v.compat_fid : v.fid;
    const size = state.compat ? v.compat_size : v.size;
    const vsize = (state.compat ? v.compat_vsize : v.vsize) || 1;
    Object.assign(req, {
      video_fid: fid,
      audio_fid: i.audio.mp4_fid,
      height: v.height,
      label: v.label,
      muxed: !!v.muxed,
      display: `${v.label}${state.compat ? " · H.264" : ""}`,
      expected_parts: v.muxed ? [size || 1] : [vsize, i.audio.mp4_size || 1],
    });
  } else {
    Object.assign(req, {
      audio_fid: i.audio.fid,
      bitrate: state.bitrate,
      display: `${state.bitrate} kbps`,
      expected_parts: [1],
    });
  }
  const r = Cut.range();
  if (r) {
    req.clip = r;
    req.display += ` · ✂ ${Cut.fmt(r.start)}–${Cut.fmt(r.end)}`;
  }
  try {
    await api("/api/download", req);
    toast(`Added: ${i.title}`);
    poll(true);
  } catch (e) {
    toast(e.message, 5000);
  }
}

// ------------------------------------------------------------ jobs

const jobEls = new Map();

function jobActions(j) {
  if (ACTIVE.has(j.status)) {
    return `<button data-act="cancel" title="Cancel">
      <svg viewBox="0 0 24 24"><path d="M6 6l12 12M18 6L6 18"/></svg>Cancel</button>`;
  }
  if (j.status === "done" && ONLINE) {
    return j.download ? `<a class="play" href="${esc(j.download)}" download title="Save to your computer">
        <svg viewBox="0 0 24 24"><path d="M12 4v11M7 10l5 5 5-5M5 20h14"/></svg>Save</a>` : "";
  }
  if (j.status === "done") {
    return `<button class="play" data-act="open" title="Open file">
        <svg viewBox="0 0 24 24"><path d="M7 5v14l11-7z" class="fill"/></svg>Open</button>
      <button data-act="reveal" title="Show in folder">
        <svg viewBox="0 0 24 24"><path d="M3 7a2 2 0 0 1 2-2h4l2 2h8a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z"/></svg>Folder</button>`;
  }
  return `<button data-act="retry" title="Try again">
    <svg viewBox="0 0 24 24"><path d="M4 12a8 8 0 1 0 2.3-5.6M4 4v4h4"/></svg>Retry</button>`;
}

function jobRight(j) {
  if (j.status === "downloading") {
    const parts = [];
    if (j.speed) parts.push(fmtBytes(j.speed) + "/s");
    if (j.eta != null) parts.push(fmtTime(j.eta) + " left");
    return parts.join(" · ");
  }
  if (j.status === "processing" && j.eta) return fmtTime(j.eta) + " left";
  if (j.status === "done") return fmtBytes(j.size);
  return "";
}

function renderJobs() {
  const list = $("jobs");
  const seen = new Set();
  state.jobs.forEach((j, idx) => {
    seen.add(j.id);
    let el = jobEls.get(j.id);
    if (!el) {
      el = document.createElement("div");
      el.innerHTML = `
        <img class="job-thumb" alt="">
        <div class="job-body">
          <div class="job-title"></div>
          <div class="job-line"><span class="badge"></span><span class="stage"></span><span class="right"></span></div>
          <div class="bar"><i></i></div>
        </div>
        <div class="job-actions"></div>`;
      el.querySelector(".job-thumb").src = thumbUrl(j.thumbnail);
      el.querySelector(".job-actions").addEventListener("click", (e) => {
        const b = e.target.closest("button");
        if (b) jobAction(el.dataset.id, b.dataset.act);
      });
      jobEls.set(j.id, el);
    }
    el.dataset.id = j.id;
    el.className = `job ${j.status}${/^Waiting for internet/.test(j.stage || "") ? " offline" : ""}`;
    el.querySelector(".job-title").textContent = j.title;
    el.querySelector(".job-title").title = j.filename || j.title;
    const badge = el.querySelector(".badge");
    badge.className = `badge ${j.mode}`;
    badge.textContent = `${j.mode.toUpperCase()}${j.label ? " · " + j.label : ""}`;
    let stage = j.status === "error" ? (j.error || "Failed") : j.stage;
    if (j.status === "downloading" || j.status === "processing") stage += ` ${Math.floor(j.percent)}%`;
    el.querySelector(".stage").textContent = stage;
    el.querySelector(".right").textContent = jobRight(j);
    el.querySelector(".bar i").style.width = `${j.status === "done" ? 100 : j.percent || 0}%`;
    const actions = jobActions(j);
    const box = el.querySelector(".job-actions");
    if (box.dataset.sig !== j.status) { box.innerHTML = actions; box.dataset.sig = j.status; }
    if (list.children[idx] !== el) list.insertBefore(el, list.children[idx] || null);
  });
  for (const [id, el] of jobEls) {
    if (!seen.has(id)) { el.remove(); jobEls.delete(id); }
  }
  $("empty").hidden = state.jobs.length > 0;
  $("clearBtn").hidden = !state.jobs.some((j) => !ACTIVE.has(j.status));
}

async function jobAction(id, act) {
  const j = state.jobs.find((x) => x.id === id);
  if (!j) return;
  try {
    if (act === "cancel") await api("/api/cancel", { id });
    if (act === "open") await api("/api/open", { file: j.file });
    if (act === "reveal") await api("/api/reveal", { file: j.file });
    if (act === "retry" && j.request) { await api("/api/download", j.request); toast("Trying again…"); }
  } catch (e) { toast(e.message, 5000); }
  poll(true);
}

// ------------------------------------------------------------ polling

let pollTimer;
const prevStatus = new Map();

async function poll(immediate) {
  clearTimeout(pollTimer);
  try {
    const s = await api("/api/state");
    state.jobs = s.jobs;
    if (!ONLINE) {
      if (!state.settings) {
        state.mode = s.settings.mode === "mp3" ? "mp3" : "mp4";
        state.bitrate = s.settings.mp3_bitrate || 320;
        state.compat = !!s.settings.compat;
      }
      state.settings = s.settings;
      $("folderPath").textContent = s.settings.output_dir;
      $("folderChip").title = `Saving to ${s.settings.output_dir} - click to change`;
      if (!$("updateBtn").classList.contains("busy")) $("engineVer").textContent = `yt-dlp ${s.version}`;
    }
    for (const j of s.jobs) {
      const before = prevStatus.get(j.id);
      if (before && ACTIVE.has(before) && j.status === "done") {
        if (ONLINE && j.download) {
          // Hand the finished file straight to the browser's downloads.
          const a = document.createElement("a");
          a.href = j.download; a.download = ""; document.body.appendChild(a); a.click(); a.remove();
          toast(`Saving ${j.filename}`);
        } else {
          toast(`Done: ${j.filename}`);
        }
      }
      prevStatus.set(j.id, j.status);
    }
    renderJobs();
  } catch {
    // server restarting / gone - keep trying quietly
  }
  const busy = state.jobs.some((j) => ACTIVE.has(j.status));
  pollTimer = setTimeout(poll, busy ? 600 : 2500);
}

// ------------------------------------------------------------ wiring

$("urlForm").addEventListener("submit", (e) => { e.preventDefault(); fetchInfo($("url").value); });

$("url").addEventListener("paste", () => {
  setTimeout(() => { const v = $("url").value; if (looksLikeUrl(v)) fetchInfo(v); }, 0);
});

$("pasteBtn").addEventListener("click", async () => {
  try {
    const text = (await navigator.clipboard.readText()).trim();
    if (!text) return toast("Clipboard is empty.");
    $("url").value = text;
    fetchInfo(text);
  } catch {
    $("url").focus();
    toast("Press Ctrl+V to paste the link.");
  }
});

// Ctrl+V anywhere on the page drops the link into the box.
document.addEventListener("paste", (e) => {
  if (document.activeElement === $("url")) return;
  const text = (e.clipboardData?.getData("text") || "").trim();
  if (text && looksLikeUrl(text)) { $("url").value = text; fetchInfo(text); }
});

document.querySelectorAll(".seg-btn").forEach((b) => b.addEventListener("click", () => {
  state.mode = b.dataset.mode;
  if (state.mode === "mp4" && !state.info.video.length) { state.mode = "mp3"; toast("This link has no video stream."); }
  renderOptions();
}));

$("compat").addEventListener("change", (e) => { state.compat = e.target.checked; renderOptions(); });
Cut.onChange(() => { if (state.info) renderOptions(); });
$("downloadBtn").addEventListener("click", startDownload);
$("clearBtn").addEventListener("click", async () => { await api("/api/clear", {}); poll(true); });
if (!ONLINE) $("openFolder").addEventListener("click", () => api("/api/reveal", {}));

if (!ONLINE) $("folderChip").addEventListener("click", async () => {
  toast("Choose a folder in the window that opened…");
  try {
    const r = await api("/api/pick-folder", {});
    $("folderPath").textContent = r.output_dir;
    toast(`Saving to ${r.output_dir}`);
    poll(true);
  } catch (e) { toast(e.message, 5000); }
});

if (!ONLINE) $("updateBtn").addEventListener("click", async () => {
  const b = $("updateBtn");
  if (b.classList.contains("busy")) return;
  b.classList.add("busy");
  $("engineVer").textContent = "Updating…";
  try {
    const r = await api("/api/update", {});
    if (!r.ok) toast("Update failed - check your internet connection.", 5000);
    else if (r.restart) toast(`Updated to yt-dlp ${r.version}. Close and reopen YTConvert to use it.`, 7000);
    else toast(`Already the latest engine (yt-dlp ${r.version}).`);
  } catch (e) { toast(e.message, 5000); }
  b.classList.remove("busy");
  poll(true);
});

document.addEventListener("keydown", (e) => {
  if (e.key === "Enter" && e.ctrlKey && !$("picker").hidden) startDownload();
});

// Tell the server when the window goes away so it can shut down.
if (!ONLINE) {
  window.addEventListener("pagehide", () => navigator.sendBeacon("/api/bye", "{}"));
  window.addEventListener("beforeunload", (e) => {
    if (state.jobs.some((j) => ACTIVE.has(j.status))) { e.preventDefault(); e.returnValue = ""; }
  });
  api("/api/hello", {}).catch(() => {});
}
poll(true);
