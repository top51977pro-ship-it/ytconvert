"""YTConvert's own installer, running inside the signed portable Python.

"YTConvert Setup.exe" is Python's pythonw.exe renamed (sitecustomize.py sends
it here). It serves setup_web/ to an Edge app window and, when asked:

  1. copies this folder to %LOCALAPPDATA%\\Programs\\YTConvert, with the exe
     renamed to YTConvert.exe
  2. downloads the components the download zip leaves out on purpose - FFmpeg
     and QuickJS are unsigned, and browsers warn about zips that carry
     unsigned executables - from components.json, checking the SHA-256
  3. adds Start menu / desktop shortcuts and an Apps & features entry.

`YTConvert.exe --uninstall` (the Apps & features entry) removes it again.
"""

import ctypes
import hashlib
import io
import json
import os
import shutil
import subprocess
import threading
import time
import urllib.request
import winreg
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent          # ...\app
PKG = APP_DIR.parent                                # the extracted setup folder
WEB = APP_DIR / "setup_web"
TARGET = Path(os.environ["LOCALAPPDATA"]) / "Programs" / "YTConvert"
UNINST_KEY = r"Software\Microsoft\Windows\CurrentVersion\Uninstall\YTConvert"
PORT = 47822
NO_WINDOW = 0x08000000
VERSION = "1.1.0"

state = {"phase": "idle", "step": "", "percent": 0, "detail": "", "error": None, "installed": TARGET.exists()}
last_seen = time.time()
bye_at = 0.0


def set_state(**kw):
    state.update(kw)


# ------------------------------------------------------------------ install

def _stop_running_app():
    me = os.getpid()
    subprocess.run(["taskkill", "/F", "/IM", "YTConvert.exe", "/FI", f"PID ne {me}"],
                   capture_output=True, creationflags=NO_WINDOW)
    time.sleep(0.6)


def _copy_package():
    set_state(step="copy", percent=2, detail="Copying YTConvert…")
    if TARGET.exists():
        _stop_running_app()
        keep = TARGET / "app" / "bin"  # components from an earlier install
        tmp_bin = TARGET.parent / "YTConvert.bin.old"
        if keep.exists():
            shutil.rmtree(tmp_bin, ignore_errors=True)
            keep.rename(tmp_bin)
        shutil.rmtree(TARGET, ignore_errors=True)
    files = [p for p in PKG.rglob("*") if p.is_file() and "__pycache__" not in p.parts]
    for i, src in enumerate(files):
        rel = src.relative_to(PKG)
        name = "YTConvert.exe" if rel.as_posix().lower() == "ytconvert setup.exe" else None
        dst = TARGET / (name or rel)
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)
        if i % 20 == 0:
            set_state(percent=2 + int(18 * i / len(files)))
    old_bin = TARGET.parent / "YTConvert.bin.old"
    if old_bin.exists():
        shutil.rmtree(TARGET / "app" / "bin", ignore_errors=True)
        old_bin.rename(TARGET / "app" / "bin")


def _components():
    spec = json.loads((APP_DIR / "components.json").read_text(encoding="utf-8"))
    bin_dir = TARGET / "app" / "bin"
    stamp = bin_dir / "components.sha256"
    if stamp.exists() and stamp.read_text().strip() == spec["sha256"]:
        set_state(step="engine", percent=90, detail="Video engine already installed")
        return
    set_state(step="engine", percent=20, detail="Downloading the video engine…")
    req = urllib.request.Request(spec["url"], headers={"User-Agent": "YTConvert-Setup"})
    buf = io.BytesIO()
    h = hashlib.sha256()
    with urllib.request.urlopen(req, timeout=60) as r:
        total = int(r.headers.get("Content-Length") or spec["size"])
        got = 0
        t0 = time.time()
        while True:
            chunk = r.read(256 * 1024)
            if not chunk:
                break
            buf.write(chunk)
            h.update(chunk)
            got += len(chunk)
            rate = got / max(time.time() - t0, 0.1)
            set_state(percent=20 + int(65 * got / total),
                      detail=f"Downloading the video engine…  {got / 1e6:.0f} of {total / 1e6:.0f} MB"
                             f"  ·  {rate / 1e6:.1f} MB/s")
    if h.hexdigest() != spec["sha256"]:
        raise RuntimeError("The download was damaged. Check your connection and try again.")
    set_state(percent=86, detail="Unpacking…")
    bin_dir.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(buf) as z:
        z.extractall(bin_dir)
    stamp.write_text(spec["sha256"])


def _powershell(script, **env):
    subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
                   capture_output=True, env={**os.environ, **env}, creationflags=NO_WINDOW)


def _shortcuts(desktop):
    set_state(step="finish", percent=92, detail="Adding shortcuts…")
    exe = TARGET / "YTConvert.exe"
    icon = TARGET / "app" / "YTConvert.ico"
    script = (
        "$w=New-Object -ComObject WScript.Shell;"
        "foreach($p in $env:YTC_LINKS.Split('|')){ if($p){"
        "$s=$w.CreateShortcut($p); $s.TargetPath=$env:YTC_EXE; $s.WorkingDirectory=$env:YTC_DIR;"
        "$s.IconLocation=$env:YTC_ICON+',0'; $s.Description='YouTube to MP4 / MP3 in original quality'; $s.Save() } }"
    )
    links = [_start_menu() / "YTConvert.lnk"]
    if desktop:
        links.append(_desktop() / "YTConvert.lnk")
    _powershell(script, YTC_LINKS="|".join(map(str, links)), YTC_EXE=str(exe), YTC_DIR=str(TARGET), YTC_ICON=str(icon))


def _register():
    set_state(percent=97, detail="Registering with Windows…")
    size_kb = sum(p.stat().st_size for p in TARGET.rglob("*") if p.is_file()) // 1024
    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, UNINST_KEY) as k:
        for name, val in {
            "DisplayName": "YTConvert", "DisplayVersion": VERSION, "Publisher": "YTConvert",
            "DisplayIcon": str(TARGET / "app" / "YTConvert.ico"), "InstallLocation": str(TARGET),
            "UninstallString": f'"{TARGET / "YTConvert.exe"}" --uninstall',
        }.items():
            winreg.SetValueEx(k, name, 0, winreg.REG_SZ, val)
        for name, val in {"NoModify": 1, "NoRepair": 1, "EstimatedSize": size_kb}.items():
            winreg.SetValueEx(k, name, 0, winreg.REG_DWORD, val)


def _known_folder(guid_hex):
    guid = (ctypes.c_byte * 16).from_buffer_copy(bytes.fromhex(guid_hex))
    out = ctypes.c_wchar_p()
    ctypes.windll.shell32.SHGetKnownFolderPath(guid, 0, None, ctypes.byref(out))
    path = out.value
    ctypes.windll.ole32.CoTaskMemFree(out)
    return Path(path)


def _desktop():
    return _known_folder("3accbfb42cdb4c42b0297fe99a87c641")  # FOLDERID_Desktop


def _start_menu():
    return _known_folder("775d7fa72b2ec344a6a2aba601054a51")  # FOLDERID_Programs


def install(desktop=True):
    try:
        set_state(phase="installing", error=None)
        _copy_package()
        _components()
        _shortcuts(desktop)
        _register()
        set_state(phase="done", step="done", percent=100, detail="YTConvert is ready", installed=True)
    except Exception as e:  # noqa: BLE001 - shown in the window
        import traceback
        traceback.print_exc()
        set_state(phase="error", error=str(e) or e.__class__.__name__)


def launch():
    subprocess.Popen([str(TARGET / "YTConvert.exe")], cwd=str(TARGET), creationflags=NO_WINDOW)


# ---------------------------------------------------------------- uninstall

def uninstall():
    MB_YESNO, MB_ICONQUESTION, IDYES = 0x4, 0x20, 6
    box = ctypes.windll.user32.MessageBoxW
    if box(None, "Remove YTConvert from this PC?\n\nYour downloaded files are kept.", "Uninstall YTConvert",
           MB_YESNO | MB_ICONQUESTION) != IDYES:
        return
    _stop_running_app()
    for link in (_start_menu() / "YTConvert.lnk", _desktop() / "YTConvert.lnk"):
        link.unlink(missing_ok=True)
    try:
        winreg.DeleteKey(winreg.HKEY_CURRENT_USER, UNINST_KEY)
    except OSError:
        pass
    # This exe lives in the folder being removed, so let cmd finish the job
    # once we have exited.
    subprocess.Popen(f'cmd /c ping -n 3 127.0.0.1 >nul & rmdir /s /q "{TARGET}"',
                     creationflags=NO_WINDOW | 0x00000008)  # DETACHED_PROCESS
    box(None, "YTConvert was removed.", "Uninstall YTConvert", 0x40)


# ------------------------------------------------------------------- server

MIME = {".html": "text/html; charset=utf-8", ".svg": "image/svg+xml", ".png": "image/png",
        ".css": "text/css; charset=utf-8", ".js": "text/javascript; charset=utf-8"}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _json(self, obj):
        body = json.dumps(obj).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        global last_seen
        path = self.path.split("?")[0]
        if path == "/api/state":
            last_seen = time.time()
            return self._json(state)
        f = (WEB / (path.lstrip("/") or "index.html")).resolve()
        if WEB not in f.parents or not f.is_file():
            return self.send_error(404)
        data = f.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", MIME.get(f.suffix, "application/octet-stream"))
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_POST(self):
        global bye_at
        origin = self.headers.get("Origin")
        if origin and origin != f"http://127.0.0.1:{PORT}":
            return self.send_error(403)
        n = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(n) or b"{}") if n else {}
        path = self.path.split("?")[0]
        if path == "/api/install" and state["phase"] in ("idle", "error"):
            threading.Thread(target=install, args=(bool(body.get("desktop", True)),), daemon=True).start()
        elif path == "/api/launch" and state["phase"] == "done":
            launch()
        elif path == "/api/bye":
            bye_at = time.time()
        return self._json({"ok": True})


def _open_window():
    url = f"http://127.0.0.1:{PORT}/"
    for exe in (os.path.expandvars(r"%ProgramFiles(x86)%\Microsoft\Edge\Application\msedge.exe"),
                os.path.expandvars(r"%ProgramFiles%\Microsoft\Edge\Application\msedge.exe"),
                os.path.expandvars(r"%ProgramFiles%\Google\Chrome\Application\chrome.exe")):
        if os.path.exists(exe):
            subprocess.Popen([exe, f"--app={url}", "--window-size=640,520"], creationflags=NO_WINDOW)
            return
    import webbrowser
    webbrowser.open(url)


def main():
    try:
        server = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    except OSError:  # setup already open - just bring up another window
        _open_window()
        return
    server.daemon_threads = True
    threading.Timer(0.2, _open_window).start()

    def watchdog():
        while True:
            time.sleep(1)
            closed = bye_at and last_seen <= bye_at and time.time() - bye_at > 4
            if (closed or time.time() - last_seen > 600) and state["phase"] != "installing":
                server.shutdown()
                os._exit(0)
    threading.Thread(target=watchdog, daemon=True).start()
    server.serve_forever()
