"""YTConvert - local YouTube to MP4 / MP3 converter.

A tiny stdlib HTTP server that serves the UI in ./web and drives yt-dlp +
ffmpeg in worker threads. Launch with YTConvert.bat (or YTConvert.exe in the
portable build, see build_lite.py); the UI opens as an Edge
app window and the server exits on its own once that window is closed and no
download is still running.
"""

import collections
import copy
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import traceback
import urllib.request
import uuid
import webbrowser
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent
WEB = ROOT / "web"
BIN = ROOT / "bin"
# The app folder may be read-only or get replaced by a newer zip, so anything
# the app writes lives in %APPDATA%\YTConvert.
MAC = sys.platform == "darwin"
if MAC:
    DATA = Path.home() / "Library" / "Application Support" / "YTConvert"
else:
    DATA = Path(os.environ.get("APPDATA") or Path.home()) / "YTConvert"
DATA.mkdir(parents=True, exist_ok=True)
SETTINGS_FILE = DATA / "settings.json"
LOG_FILE = DATA / "ytconvert.log"
ENGINE_ZIP = DATA / "engine" / "yt-dlp.zip"
PORT = 47821
APP_ID = "ytconvert-1"

# pythonw has no console; keep a log so failures are diagnosable.
# (On macOS the .app launcher leaves stdout pointing nowhere useful too.)
if sys.stdout is None or sys.stderr is None or (MAC and not sys.stdout.isatty()):
    _log = open(LOG_FILE, "a", encoding="utf-8", buffering=1)
    sys.stdout = sys.stderr = _log

# An engine fetched by "Update engine" (yt-dlp's official zipapp) wins over
# the bundled one; zipimport loads the yt_dlp package straight from it.
if ENGINE_ZIP.exists():
    sys.path.insert(0, str(ENGINE_ZIP))

import yt_dlp  # noqa: E402  (after stdout fix: yt-dlp inspects the streams on import)
from yt_dlp.utils import DownloadCancelled  # noqa: E402

CREATE_NO_WINDOW = 0x08000000 if os.name == "nt" else 0


def downloads_folder():
    """The user's real Downloads folder - it is often moved off C:\\Users."""
    try:
        import ctypes
        guid = (ctypes.c_byte * 16).from_buffer_copy(
            bytes.fromhex("90e24d373f126545916439c4925e467b"))  # FOLDERID_Downloads
        out = ctypes.c_wchar_p()
        if ctypes.windll.shell32.SHGetKnownFolderPath(guid, 0, None, ctypes.byref(out)) == 0:
            path = out.value
            ctypes.windll.ole32.CoTaskMemFree(out)
            return Path(path)
    except Exception:  # noqa: BLE001
        pass
    return Path.home() / "Downloads"


def log(*args):
    print(time.strftime("%H:%M:%S"), *args, flush=True)


EXE = ".exe" if os.name == "nt" else ""


def find_ffmpeg():
    local = BIN / f"ffmpeg{EXE}"
    if local.exists():
        return str(BIN)
    found = shutil.which("ffmpeg")
    return str(Path(found).parent) if found else None


FFMPEG_DIR = find_ffmpeg()
FFMPEG = str(Path(FFMPEG_DIR) / f"ffmpeg{EXE}") if FFMPEG_DIR else "ffmpeg"
FFPROBE = str(Path(FFMPEG_DIR) / f"ffprobe{EXE}") if FFMPEG_DIR else "ffprobe"


def media_duration(path):
    r = subprocess.run([FFPROBE, "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(path)],
                       capture_output=True, text=True, creationflags=CREATE_NO_WINDOW)
    try:
        return float(r.stdout.strip())
    except ValueError:
        return 0.0


# ---------------------------------------------------------------- settings

DEFAULT_SETTINGS = {
    "output_dir": str(downloads_folder() / "YTConvert"),
    "mode": "mp4",
    "mp3_bitrate": 320,
    "compat": False,
}


def load_settings():
    s = dict(DEFAULT_SETTINGS)
    legacy = ROOT / "settings.json"  # before settings moved to %APPDATA%
    try:
        src = SETTINGS_FILE if SETTINGS_FILE.exists() else legacy
        s.update(json.loads(src.read_text(encoding="utf-8")))
    except (OSError, ValueError):
        pass
    return s


def save_settings(s):
    SETTINGS_FILE.write_text(json.dumps(s, indent=2, ensure_ascii=False), encoding="utf-8")


settings = load_settings()
settings_lock = threading.Lock()


# ------------------------------------------------------- format selection

def _size(f, duration):
    size = f.get("filesize") or f.get("filesize_approx")
    if not size and f.get("tbr") and duration:
        size = f["tbr"] * 1000 / 8 * duration
    return int(size or 0)


def _is_video(f):
    return (f.get("vcodec") not in (None, "none") and f.get("height")
            and f.get("ext") != "mhtml" and not f.get("has_drm"))


def _is_audio_only(f):
    return (f.get("acodec") not in (None, "none") and f.get("vcodec") in (None, "none")
            and not f.get("has_drm"))


def _audio_key(f):
    # Original-language track first, never the "DRC" (dynamic range compressed)
    # variants, then bitrate; opus wins ties because it is the better codec.
    return (
        f.get("language_preference") or 0,
        "drc" not in str(f.get("format_id", "")).lower(),
        f.get("abr") or f.get("tbr") or 0,
        f.get("acodec", "").startswith("opus"),
    )


# Rough bits-for-bits quality of each codec relative to H.264, so a 1 Mbps VP9
# stream doesn't beat a 3 Mbps H.264 one just for being newer.
CODEC_EFFICIENCY = {"avc1": 1.0, "h264": 1.0, "vp9": 1.5, "vp09": 1.5, "av01": 1.7, "hev1": 1.5, "hvc1": 1.5}


def _is_hls(f):
    return "m3u8" in str(f.get("protocol", ""))


def _video_key(f):
    # Same resolution: SDR over HDR (HDR in an MP4 looks washed out in most
    # players), higher frame rate, then DASH over HLS - on YouTube the HLS
    # variants are the same encodes but report *peak* bitrate, which would
    # win every comparison - and finally codec-weighted average bitrate.
    codec = str(f.get("vcodec", "")).split(".")[0]
    br = (f.get("vbr") or f.get("tbr") or 0) * CODEC_EFFICIENCY.get(codec, 1.0)
    return ((f.get("dynamic_range") or "SDR") == "SDR", f.get("fps") or 0, not _is_hls(f), br)


def _is_h264(f):
    return str(f.get("vcodec", "")).startswith(("avc1", "h264"))


def res_of(f):
    # YouTube's own name ("480p") when it gives one - some odd-aspect streams
    # are 1080x608 but belong to the 480p rung. Otherwise the short side, so a
    # vertical 1080x1920 Short reads "1080p".
    m = re.match(r"(\d{3,4})p", str(f.get("format_note") or ""))
    if m:
        return int(m.group(1))
    return min(f["height"], f.get("width") or f["height"])


def res_label(p):
    return {4320: "8K", 2160: "4K", 1440: "2K", 1080: "Full HD", 720: "HD"}.get(p, "")


def build_options(info):
    """Turn a yt-dlp info dict into the quality menu the UI shows."""
    duration = info.get("duration") or 0
    # A direct file link comes back as a single format with no list.
    formats = info.get("formats") or ([info] if info.get("url") else [])

    audios = [f for f in formats if _is_audio_only(f)]
    best_audio = max(audios, key=_audio_key) if audios else None
    m4a = [f for f in audios if str(f.get("acodec", "")).startswith("mp4a")]
    mp4_audio = max(m4a, key=_audio_key) if m4a else best_audio

    groups = {}
    for f in formats:
        if not _is_video(f) or f.get("acodec") not in (None, "none"):
            continue  # video-only streams; muxed ones are only a fallback
        groups.setdefault(res_of(f), []).append(f)

    video = []
    audio_size = _size(mp4_audio, duration) if mp4_audio else 0
    for p in sorted(groups, reverse=True):
        fs = groups[p]
        best = max(fs, key=_video_key)
        h264 = [f for f in fs if _is_h264(f)]
        compat = max(h264, key=_video_key) if h264 else best
        fps = int(best.get("fps") or 0)
        video.append({
            "p": p,
            "label": f"{p}p{fps if fps > 30 else ''}",
            "tag": res_label(p),
            "fps": fps,
            "height": best["height"],
            "codec": str(best.get("vcodec", "")).split(".")[0],
            "fid": best["format_id"],
            "size": _size(best, duration) + audio_size,
            "vsize": _size(best, duration),
            "compat_vsize": _size(compat, duration),
            "compat_fid": compat["format_id"],
            "compat_reencode": not _is_h264(compat),
            "compat_size": _size(compat, duration) + audio_size,
        })

    if not video:
        # Sites / videos with only muxed streams: offer whatever exists.
        muxed = [f for f in formats if _is_video(f)]
        for f in sorted(muxed, key=lambda f: (f["height"], _video_key(f)), reverse=True)[:1]:
            p = res_of(f)
            video.append({
                "p": p, "label": f"{p}p", "tag": res_label(p), "fps": int(f.get("fps") or 0),
                "height": f["height"], "codec": str(f.get("vcodec", "")).split(".")[0],
                "fid": f["format_id"], "size": _size(f, duration),
                "compat_fid": f["format_id"], "compat_reencode": not _is_h264(f),
                "compat_size": _size(f, duration), "muxed": True,
            })

    if not video:
        # A direct link to a video file (not a YouTube page): yt-dlp often
        # can't tell its codecs or size, so offer the file as it is.
        files = [f for f in formats if f.get("url") and f.get("vcodec") != "none"
                 and f.get("ext") in ("mp4", "webm", "mov", "mkv", "m4v")]
        if files:
            f = files[-1]
            size = _size(f, duration)
            video.append({
                "p": 0, "label": "Original", "tag": "", "fps": 0, "height": 0,
                "codec": str(f.get("vcodec") or f.get("ext") or "").split(".")[0],
                "fid": f["format_id"], "size": size, "vsize": size,
                "compat_fid": f["format_id"], "compat_reencode": False,
                "compat_size": size, "compat_vsize": size, "muxed": True,
            })

    return {
        "id": info.get("id"),
        "title": info.get("title") or "video",
        "channel": info.get("channel") or info.get("uploader") or "",
        "duration": duration,
        "thumbnail": info.get("thumbnail"),
        "url": info.get("webpage_url") or info.get("original_url"),
        "is_live": bool(info.get("is_live")),
        "video": video,
        "audio": {
            "fid": best_audio["format_id"] if best_audio else None,
            "mp4_fid": mp4_audio["format_id"] if mp4_audio else None,
            "mp4_size": audio_size,
            "abr": round((best_audio or {}).get("abr") or 0),
            "codec": str((best_audio or {}).get("acodec", "")).split(".")[0],
        },
    }


def js_runtimes():
    """YouTube's player challenges need a JS engine. Prefer an installed Node
    or Deno (fast); the portable build ships QuickJS in bin/ as the fallback."""
    rt = {}
    if shutil.which("node"):
        rt["node"] = {}
    if shutil.which("deno"):
        rt["deno"] = {}
    qjs = BIN / f"qjs{EXE}"
    if qjs.exists():
        rt["quickjs"] = {"path": str(qjs)}
    return rt


# Extra yt-dlp options layered on every call - the online service (server.py)
# uses this for its size and duration limits.
EXTRA_YDL_OPTS = {}


def base_opts():
    opts = {
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
        "noplaylist": True,
        "windowsfilenames": True,
        "js_runtimes": js_runtimes(),
        # If the bundled challenge solver goes stale after an engine update,
        # let yt-dlp fetch the matching one from its own GitHub.
        "remote_components": ["ejs:github"],
        "concurrent_fragment_downloads": 8,
        "retries": 10,
        "fragment_retries": 10,
    }
    if FFMPEG_DIR:
        opts["ffmpeg_location"] = FFMPEG_DIR
    opts.update(EXTRA_YDL_OPTS)
    return opts


def friendly_error(e):
    msg = re.sub(r"\x1b\[[0-9;]*m", "", str(e)).replace("ERROR: ", "")
    low = msg.lower()
    if "confirm your age" in low or "age-restricted" in low:
        return "This video is age-restricted - YouTube only serves it to signed-in accounts."
    if "private video" in low:
        return "This video is private."
    if "not a bot" in low:
        return "YouTube is temporarily blocking downloads from your internet connection (bot check). It usually clears up on its own within a few hours - try again later."
    if "members-only" in low or "join this channel" in low:
        return "This is a members-only video."
    if "unsupported url" in low:
        return "That link isn't a video link I can read."
    if "video unavailable" in low:
        return "Video unavailable (removed, blocked in your country, or the link is wrong)."
    if "is not a valid url" in low:
        return "That doesn't look like a link."
    return msg.strip()[:400]


INFO_CACHE = {}
# The raw answer YouTube gave for each link. Preview, download and cut all
# reuse it instead of asking again: every extra page + player request is what
# gets a home connection flagged with "confirm you're not a bot".
RAW_INFO = {}  # url -> (fetched_at, sanitized info)
RAW_INFO_TTL = 2 * 3600  # stream URLs inside it stay valid ~6 h
raw_lock = threading.Lock()


def remember_info(ydl, info, *urls):
    raw = ydl.sanitize_info(info)
    with raw_lock:
        for u in urls:
            if u:
                RAW_INFO[u] = (time.time(), raw)
        while len(RAW_INFO) > 40:
            RAW_INFO.pop(next(iter(RAW_INFO)))


def resolve(ydl, url, download):
    """extract_info, but from the stored answer when there is a fresh one.
    Falls back to asking YouTube if the stored stream links stopped working."""
    with raw_lock:
        hit = RAW_INFO.get(url)
    if hit and time.time() - hit[0] < RAW_INFO_TTL:
        try:
            return ydl.process_ie_result(copy.deepcopy(hit[1]), download=download)
        except DownloadCancelled:
            raise
        except yt_dlp.utils.DownloadError:
            with raw_lock:
                RAW_INFO.pop(url, None)
    info = ydl.extract_info(url, download=download)
    remember_info(ydl, info, url)
    return info


def fetch_info(url):
    with yt_dlp.YoutubeDL(base_opts()) as ydl:
        info = ydl.extract_info(url, download=False)
        if info.get("_type") == "playlist" or "entries" in info:
            raise ValueError("That's a playlist link - paste the link of a single video.")
        options = build_options(info)
        remember_info(ydl, info, url, options["url"], info.get("webpage_url"))
    INFO_CACHE[options["url"]] = options
    return options


# ------------------------------------------------------------------ jobs

jobs = {}
jobs_lock = threading.Lock()
pool = ThreadPoolExecutor(max_workers=3)


class Cancelled(Exception):
    pass


def job_update(job, **kw):
    with jobs_lock:
        job.update(kw)


def active_jobs():
    with jobs_lock:
        return [j for j in jobs.values() if j["status"] in ("queued", "preparing", "downloading", "processing")]


def public_job(j):
    return {k: v for k, v in j.items() if not k.startswith("_")}


PP_STAGES = {
    "Merger": "Merging video + audio",
    "ExtractAudio": "Converting to MP3",
    "FFmpegExtractAudio": "Converting to MP3",
    "EmbedThumbnail": "Adding cover art",
    "FFmpegMetadata": "Writing tags",
    "Metadata": "Writing tags",
    "FFmpegThumbnailsConvertor": "Preparing cover art",
    "ThumbnailsConvertor": "Preparing cover art",
    "MoveFiles": "Finishing",
}


# How long a download waits for a lost connection before giving up (the
# Retry button still resumes it after that).
OFFLINE_WAIT = 15 * 60
NO_INTERNET = "No internet connection - press Retry when you're back online."


def is_online(timeout=4):
    """Can we reach YouTube at all? (DNS + TCP, no request.)"""
    import socket
    try:
        with socket.create_connection(("www.youtube.com", 443), timeout=timeout):
            return True
    except OSError:
        return False


def wait_for_internet(job):
    """Hold a job while the connection is down. True once it's back, False
    after OFFLINE_WAIT. Whatever was downloaded stays on disk, and yt-dlp
    resumes from there."""
    job_update(job, status="downloading", stage="Waiting for internet…", speed=None, eta=None)
    deadline = time.time() + OFFLINE_WAIT
    while time.time() < deadline:
        for _ in range(6):  # check the connection every 3 s, cancel every 0.5 s
            if job["_cancel"].is_set():
                raise Cancelled()
            time.sleep(0.5)
        if is_online():
            return True
    return False


def run_job(job):
    while True:
        try:
            _run_job(job)
            return
        except (Cancelled, DownloadCancelled):
            job_update(job, status="cancelled", stage="Cancelled", speed=None, eta=None)
            _cleanup_partials(job)
            return
        except Exception as e:  # noqa: BLE001 - surface every failure in the UI
            if job["_cancel"].is_set():
                job_update(job, status="cancelled", stage="Cancelled", speed=None, eta=None)
                _cleanup_partials(job)
                return
            if not is_online():
                log("job", job["id"], "lost the connection, waiting")
                try:
                    back = wait_for_internet(job)
                except Cancelled:
                    job_update(job, status="cancelled", stage="Cancelled", speed=None, eta=None)
                    _cleanup_partials(job)
                    return
                if back:
                    log("job", job["id"], "connection is back, resuming")
                    job_update(job, status="preparing", stage="Back online - resuming", error=None)
                    continue
                job_update(job, status="error", stage="Failed", error=NO_INTERNET, speed=None, eta=None)
                return
            log("job failed", job["id"], traceback.format_exc())
            job_update(job, status="error", stage="Failed", error=friendly_error(e), speed=None, eta=None)
            return


def _cleanup_partials(job):
    stem = job.get("_stem")
    if not stem:
        return
    out = Path(job["_outdir"])
    for p in out.glob(glob_escape(Path(stem).name) + "*"):
        if p.suffix in (".part", ".ytdl") or ".part" in p.name or re.search(r"\.f\d+[-\w]*\.", p.name) or p.name.endswith(".temp.mp4"):
            try:
                p.unlink()
            except OSError:
                pass


def glob_escape(s):
    return re.sub(r"([\[\]*?])", r"[\1]", s)


def _run_job(job):
    req = job["_req"]
    outdir = Path(req["output_dir"])
    outdir.mkdir(parents=True, exist_ok=True)
    job["_outdir"] = str(outdir)
    mode = req["mode"]
    if req.get("clip"):
        return _run_clip_job(job, outdir)

    parts = {}  # filename -> [downloaded, total]
    expected = req.get("expected_parts") or []

    def progress_hook(d):
        if job["_cancel"].is_set():
            raise DownloadCancelled("cancelled")
        fn = d.get("filename") or d.get("tmpfilename") or "?"
        if job.get("_stem") is None and fn != "?":
            job["_stem"] = re.sub(r"\.f[\w-]+\.\w+(\.part)?$|\.\w+(\.part)?$", "", fn)
        if d["status"] == "downloading":
            total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
            parts[fn] = [d.get("downloaded_bytes") or 0, total]
        elif d["status"] == "finished":
            got = d.get("total_bytes") or d.get("downloaded_bytes") or (parts.get(fn, [0, 0])[1])
            parts[fn] = [got, got]
        else:
            return
        done = sum(v[0] for v in parts.values())
        total = sum(v[1] for v in parts.values())
        # Parts not started yet still count toward the total, from the estimate.
        pending = expected[len(parts):]
        total += sum(pending)
        n = len(expected) or 1
        idx = min(len(parts), n)
        label = "Downloading"
        if mode == "mp4" and n == 2:
            label = "Downloading video" if idx <= 1 else "Downloading audio"
        pct = (done / total * 100) if total else 0
        job_update(job, status="downloading", stage=label,
                   percent=round(min(pct, 100) * (0.97 if mode == "mp4" else 0.9), 1),
                   speed=d.get("speed"), eta=d.get("eta"), downloaded=done, total=total)

    def pp_hook(d):
        if job["_cancel"].is_set():
            raise DownloadCancelled("cancelled")
        if d["status"] == "started":
            name = d.get("postprocessor", "")
            stage = PP_STAGES.get(name, "Processing")
            job_update(job, status="processing", stage=stage, speed=None, eta=None)

    opts = base_opts()
    opts.update({
        "paths": {"home": str(outdir), "temp": str(outdir)},
        "progress_hooks": [progress_hook],
        "postprocessor_hooks": [pp_hook],
        "overwrites": False,
    })

    if mode == "mp3":
        kbps = int(req.get("bitrate") or 320)
        fid = req.get("audio_fid")
        opts.update({
            "format": f"{fid}/bestaudio/best" if fid else "bestaudio/best",
            "outtmpl": {"default": "%(title).150B.%(ext)s"},
            "writethumbnail": True,
            "postprocessors": [
                {"key": "FFmpegExtractAudio", "preferredcodec": "mp3", "preferredquality": str(kbps)},
                {"key": "FFmpegThumbnailsConvertor", "format": "jpg", "when": "before_dl"},
                {"key": "FFmpegMetadata", "add_metadata": True},
                {"key": "EmbedThumbnail"},
            ],
        })
    else:
        vfid, afid, h = req.get("video_fid"), req.get("audio_fid"), int(req.get("height") or 0)
        chain = []
        if vfid and afid and not req.get("muxed"):
            chain.append(f"{vfid}+{afid}")
        elif vfid:
            chain.append(vfid)
        if h:
            chain.append(f"bv*[height<={h}]+ba[ext=m4a]/bv*[height<={h}]+ba/b[height<={h}]")
        chain.append("bv*+ba/b")
        opts.update({
            "format": "/".join(chain),
            "merge_output_format": "mp4",
            "outtmpl": {"default": f"%(title).150B [{req.get('label') or '%(height)sp'}].%(ext)s"},
            "postprocessors": [{"key": "FFmpegMetadata", "add_metadata": True}],
            # HLS sources carry a timed-ID3 data track; players don't need it.
            "postprocessor_args": {"merger+ffmpeg_o": ["-dn"]},
        })

    job_update(job, status="preparing", stage="Connecting to YouTube")
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = resolve(ydl, req["url"], download=True)

    if job["_cancel"].is_set():
        raise Cancelled()

    rd = (info.get("requested_downloads") or [{}])[0]
    path = rd.get("filepath") or rd.get("_filename") or info.get("filepath")
    if not path or not Path(path).exists():
        raise RuntimeError("Download finished but the output file is missing.")

    if mode == "mp4" and req.get("compat"):
        vcodec = info.get("vcodec") or ""
        rf = info.get("requested_formats") or []
        if rf:
            vcodec = next((f.get("vcodec") for f in rf if f.get("vcodec") not in (None, "none")), vcodec)
        if not str(vcodec).startswith(("avc1", "h264")):
            path = reencode_h264(job, path, info.get("duration") or 0)

    size = Path(path).stat().st_size
    job_update(job, status="done", stage="Done", percent=100, file=str(path),
               filename=Path(path).name, size=size, speed=None, eta=None)


# Whichever GPU encoder this PC has (NVIDIA, Intel, AMD), libx264 last. Each
# entry: (label, ffmpeg args). Clips use slightly higher quality settings -
# they are short, and a cut is the one place we must re-encode.
H264_ENCODERS = [
    ("GPU", ["-c:v", "h264_videotoolbox", "-q:v", "65", "-pix_fmt", "yuv420p"]),
    ("GPU", ["-c:v", "h264_nvenc", "-preset", "p6", "-rc", "vbr", "-cq", "19", "-b:v", "0", "-pix_fmt", "yuv420p"]),
    ("GPU", ["-c:v", "h264_qsv", "-preset", "slow", "-global_quality", "20", "-look_ahead", "0", "-pix_fmt", "nv12"]),
    ("GPU", ["-c:v", "h264_amf", "-quality", "quality", "-rc", "cqp", "-qp_i", "18", "-qp_p", "20", "-pix_fmt", "nv12"]),
    ("CPU", ["-c:v", "libx264", "-preset", "medium", "-crf", "18", "-pix_fmt", "yuv420p"]),
]
CLIP_ENCODERS = [
    ("GPU", ["-c:v", "h264_videotoolbox", "-q:v", "70", "-pix_fmt", "yuv420p"]),
    ("GPU", ["-c:v", "h264_nvenc", "-preset", "p7", "-rc", "vbr", "-cq", "18", "-b:v", "0", "-pix_fmt", "yuv420p"]),
    ("GPU", ["-c:v", "h264_qsv", "-preset", "veryslow", "-global_quality", "20", "-look_ahead", "0", "-pix_fmt", "nv12"]),
    ("GPU", ["-c:v", "h264_amf", "-quality", "quality", "-rc", "cqp", "-qp_i", "17", "-qp_p", "19", "-pix_fmt", "nv12"]),
    ("CPU", ["-c:v", "libx264", "-preset", "medium", "-crf", "17", "-pix_fmt", "yuv420p"]),
]
_working_encoders = None
_encoder_lock = threading.Lock()


def usable(encoders):
    """The subset of an encoder list this PC can actually open. Probed once
    with a tiny synthetic frame, so a missing NVIDIA/AMD GPU costs
    milliseconds instead of a failed pass over a real download."""
    global _working_encoders
    with _encoder_lock:
        if _working_encoders is None:
            ffmpeg = FFMPEG
            ok = set()
            for name in ("h264_videotoolbox", "h264_nvenc", "h264_qsv", "h264_amf", "libx264"):
                fmt = "nv12" if name in ("h264_qsv", "h264_amf") else "yuv420p"
                r = subprocess.run([ffmpeg, "-hide_banner", "-v", "error", "-f", "lavfi", "-i", "color=s=320x240:d=0.1",
                                    "-frames:v", "2", "-pix_fmt", fmt, "-c:v", name, "-f", "null", "-"],
                                   capture_output=True, creationflags=CREATE_NO_WINDOW)
                if r.returncode == 0:
                    ok.add(name)
            _working_encoders = ok
            log("usable H.264 encoders:", sorted(ok))
    picked = [e for e in encoders if e[1][1] in _working_encoders]
    return picked or encoders[-1:]


def fmt_clock(s):
    s = int(round(s))
    h, m, sec = s // 3600, s % 3600 // 60, s % 60
    return f"{h}.{m:02d}.{sec:02d}" if h else f"{m}.{sec:02d}"


def run_ffmpeg(job, cmd, duration, stage):
    """Run ffmpeg with -progress on stdout, mirroring it into the job."""
    job_update(job, status="processing", stage=stage, percent=0, speed=None, eta=None)
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            text=True, encoding="utf-8", errors="replace", creationflags=CREATE_NO_WINDOW)
    job["_proc"] = proc
    err_tail = collections.deque(maxlen=12)  # drained on a thread so ffmpeg never blocks on a full pipe
    threading.Thread(target=lambda: err_tail.extend(proc.stderr), daemon=True).start()
    started = time.time()
    for line in proc.stdout:
        if job["_cancel"].is_set():
            proc.kill()
            break
        if line.startswith("out_time_us=") and duration:
            try:
                t = int(line.split("=")[1]) / 1e6
            except ValueError:
                continue
            pct = max(0.0, min(t / duration, 1.0))
            el = time.time() - started
            job_update(job, percent=round(pct * 100, 1), eta=el / pct - el if pct > 0.02 else None)
    proc.wait()
    job["_proc"] = None
    return proc.returncode, "".join(err_tail)


# ------------------------------------------------------ range proxy for clips
#
# googlevideo URLs are locked to the IP the extraction ran from (here an IPv6
# address), and ffmpeg's own HTTP client may connect over IPv4 and get 403.
# So ffmpeg reads http://127.0.0.1/stream/<token> instead, and this server
# fetches the byte ranges it asks for through yt-dlp's networking stack - the
# one extraction just succeeded with - in chunks YouTube accepts.

STREAMS = {}
STREAM_CHUNK = 4 << 20


STREAM_MIME = {"mp4": "video/mp4", "webm": "video/webm", "m4a": "audio/mp4", "weba": "audio/webm"}


def register_stream(ydl, fmt):
    clen = fmt.get("filesize")
    if not clen:
        m = re.search(r"[?&]clen=(\d+)", fmt["url"])
        clen = int(m.group(1)) if m else None
    if not clen:
        # Ask the server: a one-byte range answer carries the total size.
        try:
            from yt_dlp.networking import Request
            with ydl.urlopen(Request(fmt["url"], headers={**(fmt.get("http_headers") or {}), "Range": "bytes=0-0"})) as r:
                m = re.search(r"/(\d+)\s*$", r.headers.get("Content-Range") or "")
                clen = int(m.group(1)) if m else None
        except Exception:  # noqa: BLE001 - then this stream just can't be cut
            clen = None
    if not clen:
        return None
    token = uuid.uuid4().hex
    STREAMS[token] = {"ydl": ydl, "url": fmt["url"], "headers": fmt.get("http_headers") or {}, "clen": int(clen),
                      "mime": STREAM_MIME.get(fmt.get("ext"), "application/octet-stream")}
    return f"http://127.0.0.1:{PORT}/stream/{token}"


# The cut panel's preview player. YouTube's embeddable player refuses many
# videos (owner setting, error 150), so the app plays a light 360p copy through
# the same range proxy instead - it works for every video and seeks exactly.
PREVIEWS = collections.OrderedDict()  # page URL -> (ydl, token)
# Muxed 360p when YouTube offers it; some clients don't, so fall back to a
# silent low-res video stream - enough to pick cut points with.
PREVIEW_FORMAT = ("18/best[height<=480][vcodec!=none][acodec!=none][protocol=https]"
                  "/bv*[height<=480][protocol=https]/wv*[protocol=https]")
previews_lock = threading.Lock()


def make_preview(url):
    with previews_lock:
        if url in PREVIEWS and PREVIEWS[url][1] in STREAMS:
            PREVIEWS.move_to_end(url)
            return f"/stream/{PREVIEWS[url][1]}"
    ydl = yt_dlp.YoutubeDL({**base_opts(), "format": PREVIEW_FORMAT})
    try:
        info = resolve(ydl, url, download=False)
    except yt_dlp.utils.DownloadError:
        info = ydl.extract_info(url, download=False)  # YouTube's answer varies per request; one retry
    fmt = (info.get("requested_formats") or [info])[0]
    u = register_stream(ydl, fmt) if str(fmt.get("protocol")) in ("https", "http") else None
    if not u:
        ydl.close()
        raise RuntimeError("No preview available for this video.")
    token = u.rsplit("/", 1)[1]
    with previews_lock:
        PREVIEWS[url] = (ydl, token)
        while len(PREVIEWS) > 3:  # keep a few, drop the oldest
            _, (old_ydl, old_token) = PREVIEWS.popitem(last=False)
            STREAMS.pop(old_token, None)
            old_ydl.close()
    return f"/stream/{token}"


def serve_stream(handler, token):
    st = STREAMS.get(token)
    if not st:
        handler.send_error(404)
        return
    from yt_dlp.networking import Request
    clen = st["clen"]
    m = re.match(r"bytes=(\d*)-(\d*)", handler.headers.get("Range") or "")
    start = int(m.group(1)) if m and m.group(1) else 0
    end = min(int(m.group(2)) if m and m.group(2) else clen - 1, clen - 1)
    if start > end:
        handler.send_response(416)
        handler.send_header("Content-Range", f"bytes */{clen}")
        handler.send_header("Content-Length", "0")
        handler.end_headers()
        return
    handler.send_response(206 if m else 200)
    handler.send_header("Content-Type", st["mime"])
    handler.send_header("Accept-Ranges", "bytes")
    handler.send_header("Content-Length", str(end - start + 1))
    if m:
        handler.send_header("Content-Range", f"bytes {start}-{end}/{clen}")
    handler.end_headers()
    pos = start
    while pos <= end and token in STREAMS:
        stop = min(pos + STREAM_CHUNK - 1, end)
        for attempt in range(4):
            try:
                with st["ydl"].urlopen(Request(st["url"], headers={**st["headers"], "Range": f"bytes={pos}-{stop}"})) as r:
                    data = r.read()
                break
            except Exception as e:  # noqa: BLE001 - retry transient network errors
                if attempt == 3:
                    log("stream fetch failed", repr(e))
                    return
                time.sleep(1 + attempt)
        if not data:
            return
        try:
            handler.wfile.write(data)
        except OSError:
            return  # ffmpeg seeked elsewhere and dropped this connection
        pos += len(data)


def _run_clip_job(job, outdir):
    """Download only [start, end] of the video.

    ffmpeg reads YouTube's stream URLs directly and seeks inside them (the
    DASH files are indexed), so a 30 s clip of an hour-long video fetches
    about 30 s of data. Seeking before -i and re-encoding makes the cut
    frame-exact; a stream-copy cut would snap to the previous keyframe."""
    req = job["_req"]
    mode = req["mode"]
    start = max(0.0, float(req["clip"]["start"]))
    end = float(req["clip"]["end"])
    if end - start < 0.5:
        raise RuntimeError("The selected part is too short.")

    if mode == "mp3":
        fid = req.get("audio_fid")
        fmt = f"{fid}/bestaudio/best" if fid else "bestaudio/best"
    else:
        vfid, afid = req.get("video_fid"), req.get("audio_fid")
        fmt = f"{vfid}+{afid}/bv*+ba/b" if vfid and afid and not req.get("muxed") else f"{vfid or 'b'}/b"

    job_update(job, status="preparing", stage="Connecting to YouTube")
    opts = base_opts()
    opts["format"] = fmt
    ydl = yt_dlp.YoutubeDL(opts)
    tokens = []
    try:
        info = resolve(ydl, req["url"], download=False)
        if job["_cancel"].is_set():
            raise Cancelled()
        streams = []
        for st in info.get("requested_formats") or [info]:
            u = register_stream(ydl, st) if str(st.get("protocol")) in ("https", "http") else None
            if not u:
                raise RuntimeError("This video's streams can't be cut on the fly - download it whole instead.")
            tokens.append(u.rsplit("/", 1)[1])
            streams.append({**st, "local_url": u})
        _cut(job, outdir, req, info, streams, start, end)
    finally:
        for t in tokens:
            STREAMS.pop(t, None)
        ydl.close()


def _cut(job, outdir, req, info, streams, start, end):
    mode = req["mode"]
    length = end - start

    title = yt_dlp.utils.sanitize_filename(info.get("title") or "video", restricted=False)[:150]
    span = f"{fmt_clock(start)}-{fmt_clock(end)}"
    ffmpeg = FFMPEG

    def input_args(s):
        return ["-ss", f"{start:.3f}", "-t", f"{length:.3f}", "-i", s["local_url"]]

    meta = ["-metadata", f"title={info.get('title') or ''}",
            "-metadata", f"artist={info.get('channel') or info.get('uploader') or ''}",
            "-metadata", f"comment={info.get('webpage_url') or ''}"]

    if mode == "mp3":
        kbps = int(req.get("bitrate") or 320)
        out = outdir / f"{title} ({span}).mp3"
        cover = []
        thumb = outdir / f".{job['id']}.cover.jpg"
        try:
            if info.get("thumbnail"):
                urllib.request.urlretrieve(info["thumbnail"], thumb)
                cover = ["-i", str(thumb)]
        except Exception:  # noqa: BLE001 - a clip without cover art is still fine
            cover = []
        job["_files"] = [str(out), str(thumb)]
        cmd = [ffmpeg, "-hide_banner", "-y", *input_args(streams[0]), *cover, "-map", "0:a:0"]
        if cover:
            cmd += ["-map", "1:v:0", "-c:v", "mjpeg", "-vf", "scale='min(1280,iw)':-2",
                    "-disposition:v", "attached_pic", "-metadata:s:v", "title=Cover"]
        cmd += ["-c:a", "libmp3lame", "-b:a", f"{kbps}k", "-id3v2_version", "3", *meta,
                "-progress", "pipe:1", "-nostats", str(out)]
        rc, err = run_ffmpeg(job, cmd, length, "Cutting & converting to MP3")
        thumb.unlink(missing_ok=True)
        if job["_cancel"].is_set():
            out.unlink(missing_ok=True)
            raise Cancelled()
        if rc != 0 or not out.exists():
            log("clip mp3 failed", err)
            out.unlink(missing_ok=True)
            raise RuntimeError("Couldn't cut this part. Try again, or download the whole video.")
    else:
        out = outdir / f"{title} [{req.get('label') or 'video'}] ({span}).mp4"
        job["_files"] = [str(out)]
        inputs, maps = [], []
        for i, s in enumerate(streams):
            inputs += input_args(s)
            if s.get("vcodec") not in (None, "none"):
                maps += ["-map", f"{i}:v:0"]
            if s.get("acodec") not in (None, "none"):
                maps += ["-map", f"{i}:a:0"]
        for kind, enc in usable(CLIP_ENCODERS):
            cmd = [ffmpeg, "-hide_banner", "-y", *inputs, *maps, *enc,
                   "-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart", *meta,
                   "-progress", "pipe:1", "-nostats", str(out)]
            rc, err = run_ffmpeg(job, cmd, length, f"Cutting your part ({kind})")
            if job["_cancel"].is_set():
                out.unlink(missing_ok=True)
                raise Cancelled()
            if rc == 0 and out.exists() and out.stat().st_size > 0:
                break
            log("clip encoder failed", enc[1], err)
            out.unlink(missing_ok=True)
        else:
            raise RuntimeError("Couldn't cut this part. Try again, or download the whole video.")

    # If the connection dropped mid-cut, ffmpeg can end "successfully" with
    # a short file. Only a complete part counts.
    got = media_duration(out)
    if got < length - 0.6:
        out.unlink(missing_ok=True)
        raise RuntimeError(f"The part came out short ({got:.1f} of {length:.1f} s) - the connection dropped.")

    job_update(job, status="done", stage="Done", percent=100, file=str(out),
               filename=out.name, size=out.stat().st_size, speed=None, eta=None)


def reencode_h264(job, path, duration):
    """Compatibility mode above 1080p: YouTube has no H.264 there, so encode it
    ourselves - Intel Quick Sync first, libx264 if the GPU encoder refuses."""
    src = Path(path)
    tmp = src.with_name(src.stem + ".temp.mp4")
    ffmpeg = FFMPEG
    for kind, enc in usable(H264_ENCODERS):
        label = f"Re-encoding to H.264 ({kind})"
        job_update(job, status="processing", stage=label, percent=0, speed=None, eta=None)
        cmd = [ffmpeg, "-hide_banner", "-y", "-i", str(src), "-map", "0:v:0", "-map", "0:a?",
               *enc, "-c:a", "copy", "-movflags", "+faststart", "-map_metadata", "0",
               "-progress", "pipe:1", "-nostats", str(tmp)]
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                text=True, creationflags=CREATE_NO_WINDOW)
        job["_proc"] = proc
        started = time.time()
        for line in proc.stdout:
            if job["_cancel"].is_set():
                proc.kill()
                break
            if line.startswith("out_time_us=") and duration:
                try:
                    t = int(line.split("=")[1]) / 1e6
                except ValueError:
                    continue
                pct = max(0.0, min(t / duration, 1.0))
                el = time.time() - started
                eta = el / pct - el if pct > 0.01 else None
                job_update(job, percent=round(pct * 100, 1), eta=eta)
        proc.wait()
        job["_proc"] = None
        if job["_cancel"].is_set():
            tmp.unlink(missing_ok=True)
            raise Cancelled()
        if proc.returncode == 0 and tmp.exists() and tmp.stat().st_size > 0:
            src.unlink()
            tmp.replace(src)
            return str(src)
        tmp.unlink(missing_ok=True)
        log("encoder failed", enc[1], "rc", proc.returncode)
    raise RuntimeError("Could not re-encode to H.264. Turn off compatibility mode to keep the original file.")


def start_job(req):
    job = {
        "id": uuid.uuid4().hex[:10],
        "title": req.get("title") or req["url"],
        "thumbnail": req.get("thumbnail"),
        "mode": req["mode"],
        "label": req.get("display") or "",
        "status": "queued",
        "stage": "Waiting",
        "percent": 0,
        "speed": None,
        "eta": None,
        "created": time.time(),
        "request": {k: v for k, v in req.items() if k != "output_dir"},
        "_req": req,
        "_cancel": threading.Event(),
        "_proc": None,
        "_stem": None,
    }
    with jobs_lock:
        jobs[job["id"]] = job
    pool.submit(run_job, job)
    return job


# ------------------------------------------------------------- lifecycle

last_seen = time.time()
bye_at = 0.0


def watchdog(server):
    while True:
        time.sleep(2)
        now = time.time()
        closed = bye_at and last_seen <= bye_at and now - bye_at > 8
        abandoned = now - last_seen > 900
        if (closed or abandoned) and not active_jobs():
            log("window gone, shutting down")
            server.shutdown()
            os._exit(0)


def open_window():
    url = f"http://127.0.0.1:{PORT}/"
    if MAC:
        # A Chromium browser in app mode looks like a real app window; Safari
        # has no such mode, so it gets a normal tab.
        for name in ("Google Chrome", "Microsoft Edge", "Brave Browser", "Chromium"):
            if Path(f"/Applications/{name}.app").exists():
                subprocess.Popen(["open", "-na", name, "--args", f"--app={url}", "--window-size=1180,860"])
                return
        subprocess.Popen(["open", url])
        return
    candidates = [
        os.path.expandvars(r"%ProgramFiles(x86)%\Microsoft\Edge\Application\msedge.exe"),
        os.path.expandvars(r"%ProgramFiles%\Microsoft\Edge\Application\msedge.exe"),
        os.path.expandvars(r"%ProgramFiles%\Google\Chrome\Application\chrome.exe"),
    ]
    for exe in candidates:
        if os.path.exists(exe):
            subprocess.Popen([exe, f"--app={url}", "--window-size=1180,860"], creationflags=CREATE_NO_WINDOW)
            return
    webbrowser.open(url)


def open_path(p):
    if MAC:
        subprocess.Popen(["open", str(p)])
    elif os.name == "nt":
        os.startfile(str(p))
    else:
        subprocess.Popen(["xdg-open", str(p)])


def reveal_path(p):
    if MAC:
        subprocess.Popen(["open", "-R", str(p)])
    elif os.name == "nt":
        subprocess.Popen(["explorer", "/select,", str(p)])
    else:
        subprocess.Popen(["xdg-open", str(p.parent)])


def pick_folder(initial):
    if MAC:
        script = ('POSIX path of (choose folder with prompt "Choose where downloads are saved" '
                  'default location (POSIX file (system attribute "YTC_INITIAL")))')
        r = subprocess.run(["osascript", "-e", script], capture_output=True, text=True,
                           env={**os.environ, "YTC_INITIAL": str(initial)})
        return r.stdout.strip().rstrip("/") or None
    # The portable Python has no tkinter; Windows' own dialog via PowerShell.
    script = (
        "[Console]::OutputEncoding=[Text.Encoding]::UTF8;"
        "Add-Type -AssemblyName System.Windows.Forms;"
        "$o=New-Object System.Windows.Forms.Form -Property @{TopMost=$true};"
        "$d=New-Object System.Windows.Forms.FolderBrowserDialog;"
        "$d.Description='Choose where downloads are saved';"
        "$d.SelectedPath=$env:YTC_INITIAL;"
        "if($d.ShowDialog($o) -eq 'OK'){[Console]::Out.Write($d.SelectedPath)}"
    )
    r = subprocess.run(["powershell", "-NoProfile", "-STA", "-Command", script],
                       capture_output=True, env={**os.environ, "YTC_INITIAL": str(initial)},
                       creationflags=CREATE_NO_WINDOW)
    chosen = r.stdout.decode("utf-8", "replace").strip()
    return chosen or None


def update_engine():
    """Fetch yt-dlp's official zipapp release. It is a zip whose root holds the
    yt_dlp package, so it is imported straight from %APPDATA% on next start."""
    current = yt_dlp.version.__version__
    req = urllib.request.Request("https://api.github.com/repos/yt-dlp/yt-dlp/releases/latest",
                                 headers={"User-Agent": "YTConvert"})
    with urllib.request.urlopen(req, timeout=20) as r:
        latest = json.loads(r.read())["tag_name"]
    if latest <= current:
        return {"ok": True, "version": current, "updated": False, "restart": False}
    url = f"https://github.com/yt-dlp/yt-dlp/releases/download/{latest}/yt-dlp"
    ENGINE_ZIP.parent.mkdir(parents=True, exist_ok=True)
    tmp = ENGINE_ZIP.with_suffix(".part")
    with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "YTConvert"}), timeout=120) as r:
        tmp.write_bytes(r.read())
    import zipfile
    with zipfile.ZipFile(tmp) as z:  # the zipapp has a shebang prefix; zipfile copes
        if "yt_dlp/__init__.py" not in z.namelist():
            tmp.unlink()
            raise RuntimeError("Downloaded engine looks wrong - try again later.")
    tmp.replace(ENGINE_ZIP)
    return {"ok": True, "version": latest, "updated": True, "restart": True}


# ------------------------------------------------------------------- http

MIME = {".html": "text/html; charset=utf-8", ".js": "text/javascript; charset=utf-8",
        ".css": "text/css; charset=utf-8", ".svg": "image/svg+xml", ".png": "image/png",
        ".ico": "image/x-icon", ".woff2": "font/woff2"}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _json(self, obj, code=200):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _body(self):
        n = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(n) if n else b"{}"
        try:
            return json.loads(raw or b"{}")
        except ValueError:
            return {}

    def _local_only(self):
        # Refuse cross-site requests from other pages open in the browser.
        origin = self.headers.get("Origin")
        if origin and origin not in (f"http://127.0.0.1:{PORT}", f"http://localhost:{PORT}"):
            self._json({"error": "forbidden"}, 403)
            return False
        return True

    def do_GET(self):
        global last_seen
        path = self.path.split("?")[0]
        if path == "/api/ping":
            return self._json({"app": APP_ID})
        if path == "/api/state":
            last_seen = time.time()
            with jobs_lock:
                js = sorted((public_job(j) for j in jobs.values()), key=lambda j: -j["created"])
            with settings_lock:
                s = dict(settings)
            return self._json({"jobs": js, "settings": s, "version": yt_dlp.version.__version__,
                               "ffmpeg": bool(FFMPEG_DIR)})
        if path == "/api/thumb":
            return self._proxy_thumb()
        if path.startswith("/stream/"):
            return serve_stream(self, path.rsplit("/", 1)[1])
        if path == "/":
            path = "/index.html"
        f = (WEB / path.lstrip("/")).resolve()
        if WEB not in f.parents or not f.is_file():
            self.send_error(404)
            return
        data = f.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", MIME.get(f.suffix, "application/octet-stream"))
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(data)

    def _proxy_thumb(self):
        # Thumbnails come from i.ytimg.com; proxy them so the page never needs
        # third-party requests of its own.
        from urllib.parse import parse_qs, urlparse
        q = parse_qs(urlparse(self.path).query).get("u", [""])[0]
        host = urlparse(q).hostname or ""
        if not (host.endswith("ytimg.com") or host.endswith("ggpht.com") or host.endswith("googleusercontent.com")):
            self.send_error(400)
            return
        try:
            with urllib.request.urlopen(urllib.request.Request(q, headers={"User-Agent": "Mozilla/5.0"}), timeout=10) as r:
                data, ctype = r.read(), r.headers.get("Content-Type", "image/jpeg")
        except Exception:  # noqa: BLE001
            self.send_error(502)
            return
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "max-age=86400")
        self.end_headers()
        self.wfile.write(data)

    def do_POST(self):
        global bye_at, last_seen
        if not self._local_only():
            return
        path = self.path.split("?")[0]
        body = self._body()
        try:
            if path == "/api/bye":
                bye_at = time.time()
                log("window closed")
                return self._json({"ok": True})
            if path == "/api/hello":
                last_seen = time.time()
                return self._json({"ok": True})
            if path == "/api/info":
                url = (body.get("url") or "").strip()
                if not url:
                    return self._json({"error": "Paste a YouTube link first."}, 400)
                try:
                    return self._json(fetch_info(url))
                except Exception as e:  # noqa: BLE001
                    log("info failed", url, repr(e))
                    msg = friendly_error(e) if is_online() else "No internet connection - check your Wi-Fi and try again."
                    return self._json({"error": msg}, 400)
            if path == "/api/preview":
                try:
                    return self._json({"src": make_preview((body.get("url") or "").strip())})
                except Exception as e:  # noqa: BLE001
                    log("preview failed", repr(e))
                    return self._json({"error": friendly_error(e)}, 400)
            if path == "/api/download":
                with settings_lock:
                    body.setdefault("output_dir", settings["output_dir"])
                    for k in ("mode", "compat"):
                        if k in body:
                            settings[k] = body[k]
                    if body.get("mode") == "mp3" and body.get("bitrate"):
                        settings["mp3_bitrate"] = int(body["bitrate"])
                    save_settings(settings)
                job = start_job(body)
                return self._json(public_job(job))
            if path == "/api/cancel":
                job = jobs.get(body.get("id"))
                if job:
                    job["_cancel"].set()
                    if job.get("_proc"):
                        job["_proc"].kill()
                    if job["status"] == "queued":
                        job_update(job, status="cancelled", stage="Cancelled")
                return self._json({"ok": True})
            if path == "/api/clear":
                with jobs_lock:
                    for k in [k for k, j in jobs.items() if j["status"] in ("done", "error", "cancelled")]:
                        del jobs[k]
                return self._json({"ok": True})
            if path == "/api/reveal":
                target = body.get("file")
                if target and Path(target).exists():
                    reveal_path(Path(target))
                else:
                    with settings_lock:
                        d = Path(settings["output_dir"])
                    d.mkdir(parents=True, exist_ok=True)
                    open_path(d)
                return self._json({"ok": True})
            if path == "/api/open":
                target = body.get("file")
                if target and Path(target).exists():
                    open_path(Path(target))
                return self._json({"ok": True})
            if path == "/api/pick-folder":
                with settings_lock:
                    cur = settings["output_dir"]
                chosen = pick_folder(cur)
                if chosen:
                    with settings_lock:
                        settings["output_dir"] = str(Path(chosen))
                        save_settings(settings)
                return self._json({"output_dir": chosen or cur})
            if path == "/api/update":
                return self._json(update_engine())
            return self._json({"error": "not found"}, 404)
        except Exception as e:  # noqa: BLE001
            log("request failed", path, traceback.format_exc())
            return self._json({"error": str(e)}, 500)


def already_running():
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{PORT}/api/ping", timeout=1.5) as r:
            return json.loads(r.read()).get("app") == APP_ID
    except Exception:  # noqa: BLE001
        return False


def main():
    no_window = "--no-window" in sys.argv
    if already_running():
        if not no_window:
            open_window()
        return
    server = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    server.daemon_threads = True
    log(f"YTConvert on http://127.0.0.1:{PORT}  yt-dlp {yt_dlp.version.__version__}  ffmpeg={FFMPEG_DIR}")
    if not no_window:
        threading.Thread(target=watchdog, args=(server,), daemon=True).start()
        threading.Timer(0.3, open_window).start()
    server.serve_forever()


if __name__ == "__main__":
    main()
