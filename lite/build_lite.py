"""Build what the website serves.

    python lite/build_lite.py              (YTC_SITE_URL=... to point at a test server)

1. site/YTConvert-Setup.zip - what visitors download. Every executable in it
   is signed by the Python Software Foundation or Microsoft, so browsers and
   Windows have nothing to warn about:

       YTConvert Setup/
           YTConvert Setup.exe   <- Python's signed pythonw.exe, renamed
           python311.dll ...     <- the rest of the official embeddable Python
           python311._pth        <- puts app/ and lib/ on sys.path, enables site
           app/                  <- app.py, setup.py, sitecustomize.py, web/, setup_web/
           lib/                  <- yt_dlp + yt_dlp_ejs (pure Python)

2. site/components/engine-win64-<hash>.zip - FFmpeg (shared build) and
   QuickJS. They are unsigned, so they are not in the zip above; setup.py
   downloads this during install and checks the SHA-256 recorded in
   app/components.json.

Inputs are cached in lite-build/dl.
"""

import hashlib
import json
import os
import shutil
import subprocess
import sys
import urllib.request
import zipfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
WORK = ROOT / "lite-build"
DL = WORK / "dl"
SITE = ROOT / "site"
PKG_NAME = "YTConvert Setup"
STAGE = WORK / PKG_NAME
SETUP_ZIP = SITE / "YTConvert-Setup.zip"
SITE_URL = os.environ.get("YTC_SITE_URL", "https://ytconvert-app.vercel.app").rstrip("/")

PY_VERSION = "3.11.9"  # must match the local Python so pip picks the same wheels
PY_URL = f"https://www.python.org/ftp/python/{PY_VERSION}/python-{PY_VERSION}-embed-amd64.zip"
QJS_URL = "https://github.com/quickjs-ng/quickjs/releases/download/v0.17.0/qjs-windows-x86_64.exe"
FFMPEG_URL = "https://github.com/BtbN/FFmpeg-Builds/releases/download/latest/ffmpeg-n8.1-latest-win64-gpl-shared-8.1.zip"


def fetch(url, dest):
    if not dest.exists():
        print("download", url)
        with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "YTConvert-build"})) as r:
            dest.write_bytes(r.read())
    return dest


def zip_dir(src, out, arc_root):
    out.unlink(missing_ok=True)
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as z:
        for p in sorted(src.rglob("*")):
            if p.is_file():
                z.write(p, (Path(arc_root) / p.relative_to(src)).as_posix() if arc_root else p.relative_to(src).as_posix())


def assert_all_signed(folder):
    """Refuse to ship a download a browser would flag: every PE file must
    carry a valid signature (PSF or Microsoft)."""
    pes = [str(p) for p in folder.rglob("*") if p.suffix.lower() in (".exe", ".dll", ".pyd")]
    script = ("$bad=@(); foreach($f in $env:YTC_FILES.Split('|')){ $s=Get-AuthenticodeSignature -LiteralPath $f;"
              " if($s.Status -ne 'Valid'){ $bad+=\"$($s.Status) $f\" } }; $bad -join \"`n\"")
    r = subprocess.run(["powershell", "-NoProfile", "-Command", script], capture_output=True, text=True,
                       env={**os.environ, "YTC_FILES": "|".join(pes)})
    bad = r.stdout.strip()
    if bad:
        raise SystemExit(f"unsigned executables in the download:\n{bad}")
    print(f"all {len(pes)} executables signed")


def build_components():
    """FFmpeg + QuickJS, flat, as the app expects them in app/bin."""
    comp = WORK / "components"
    shutil.rmtree(comp, ignore_errors=True)
    comp.mkdir(parents=True)
    with zipfile.ZipFile(fetch(FFMPEG_URL, DL / "ffmpeg-shared.zip")) as z:
        for n in z.namelist():
            name = n.rsplit("/", 1)[-1]
            if "/bin/" in n and name and name != "ffplay.exe":
                (comp / name).write_bytes(z.read(n))
            elif n.endswith("/LICENSE.txt"):
                (comp / "FFMPEG-LICENSE.txt").write_bytes(z.read(n))
    shutil.copy2(fetch(QJS_URL, DL / "qjs.exe"), comp / "qjs.exe")
    tmp = WORK / "engine.zip"
    zip_dir(comp, tmp, "")
    digest = hashlib.sha256(tmp.read_bytes()).hexdigest()
    name = f"engine-win64-{digest[:10]}.zip"
    out_dir = SITE / "components"
    shutil.rmtree(out_dir, ignore_errors=True)  # only the current engine is served
    out_dir.mkdir(parents=True)
    out = out_dir / name
    shutil.move(tmp, out)
    print(f"{out}  {out.stat().st_size / 1e6:.1f} MB")
    return {"url": f"{SITE_URL}/components/{name}", "size": out.stat().st_size, "sha256": digest}


def build_setup(components):
    shutil.rmtree(STAGE, ignore_errors=True)
    STAGE.mkdir(parents=True)

    with zipfile.ZipFile(fetch(PY_URL, DL / "python-embed.zip")) as z:
        z.extractall(STAGE)
    (STAGE / "python.exe").unlink()
    (STAGE / "pythonw.exe").rename(STAGE / f"{PKG_NAME}.exe")
    pth = next(STAGE.glob("python3*._pth"))
    pth.write_text(f"{pth.stem}.zip\n.\napp\nlib\nimport site\n", encoding="ascii")

    subprocess.run([sys.executable, "-m", "pip", "install", "--quiet", "--disable-pip-version-check",
                    "--no-deps", "--no-compile", "--target", str(STAGE / "lib"), "yt-dlp", "yt-dlp-ejs"], check=True)
    for junk in (STAGE / "lib").glob("*.dist-info"):
        shutil.rmtree(junk)
    shutil.rmtree(STAGE / "lib" / "bin", ignore_errors=True)

    app = STAGE / "app"
    app.mkdir()
    for f in ("app.py",):
        shutil.copy2(ROOT / f, app / f)
    for f in ("setup.py", "sitecustomize.py"):
        shutil.copy2(HERE / f, app / f)
    shutil.copytree(ROOT / "web", app / "web")
    shutil.copytree(HERE / "setup_web", app / "setup_web")
    shutil.copy2(ROOT / "web" / "icon.svg", app / "setup_web" / "icon.svg")
    shutil.copy2(ROOT / "YTConvert.ico", app / "YTConvert.ico")
    (app / "components.json").write_text(json.dumps(components, indent=2), encoding="utf-8")
    shutil.copy2(HERE / "README.txt", STAGE / "README.txt")

    assert_all_signed(STAGE)
    zip_dir(STAGE, SETUP_ZIP, PKG_NAME)
    print(f"{SETUP_ZIP}  {SETUP_ZIP.stat().st_size / 1e6:.1f} MB")


def main():
    DL.mkdir(parents=True, exist_ok=True)
    for old in ("YTConvert-win64.zip", "YTConvert-Setup.exe"):  # earlier download formats
        (SITE / old).unlink(missing_ok=True)
    build_setup(build_components())
    total = sum(p.stat().st_size for p in SITE.rglob("*") if p.is_file() and ".vercel" not in p.parts)
    print(f"site total {total / 1e6:.1f} MB (Vercel Hobby cap: 100 MB)")


if __name__ == "__main__":
    main()
