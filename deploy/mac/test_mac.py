"""Smoke-test a built YTConvert.app on a real Mac (GitHub Actions).

    python3 deploy/mac/test_mac.py dist-mac/arm64/YTConvert.app

Starts the app headless through its own launcher, then drives the same HTTP
API the UI uses: video info, a full download, an MP4 cut and an MP3 cut, and
checks every output with the bundled ffprobe. GitHub's runners are datacenter
machines YouTube sometimes refuses ("confirm you're not a bot"); if that
happens the app is tested against a plain video file instead, and the run
says so.
"""

import json
import os
import subprocess
import sys
import tempfile
import time
import urllib.request

APP = os.path.abspath(sys.argv[1])
BIN = os.path.join(APP, "Contents/Resources/app/bin")
BASE = "http://127.0.0.1:47821"
OUT = tempfile.mkdtemp(prefix="ytc-test-")
YOUTUBE = "https://www.youtube.com/watch?v=jNQXAC9IVRw"  # "Me at the zoo", 19 s
PLAIN = "https://raw.githubusercontent.com/top51977pro-ship-it/ytconvert/main/deploy/mac/sample.mp4"  # 12 s, ours, serves byte ranges
failures = []


def api(path, body=None):
    req = urllib.request.Request(BASE + path, headers={"Content-Type": "application/json", "Origin": BASE},
                                 data=None if body is None else json.dumps(body).encode(),
                                 method="GET" if body is None else "POST")
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        return json.loads(e.read() or b"{}")


def probe(path):
    r = subprocess.run([os.path.join(BIN, "ffprobe"), "-v", "error", "-show_entries",
                        "format=duration:stream=codec_name,height", "-of", "compact", path],
                       capture_output=True, text=True)
    return r.stdout.strip().replace("\n", " | ")


def run_job(name, req):
    req = {**req, "output_dir": OUT, "title": name}
    job = api("/api/download", req)
    if "id" not in job:
        failures.append(f"{name}: not started: {job}")
        return
    for _ in range(300):
        time.sleep(1)
        j = next((x for x in api("/api/state")["jobs"] if x["id"] == job["id"]), None)
        if j and j["status"] not in ("queued", "preparing", "downloading", "processing"):
            break
    if not j or j["status"] != "done":
        failures.append(f"{name}: {j and j['status']} {j and j.get('error')}")
        print(f"FAIL {name}: {j}")
        return
    print(f"ok   {name}: {j['filename']}  {j['size']:,} bytes  ->  {probe(j['file'])}")


def main():
    app = subprocess.Popen([os.path.join(APP, "Contents/MacOS/YTConvert"), "--no-window"])
    for _ in range(60):
        try:
            if api("/api/ping").get("app"):
                break
        except Exception:
            time.sleep(1)
    else:
        sys.exit("the app never started")
    print("app is up; engine", api("/api/state").get("version"))

    info = api("/api/info", {"url": YOUTUBE})
    source = "YouTube"
    if "error" in info:
        print(f"YouTube refused this runner: {info['error']}")
        print("-> testing the app with a plain video file instead")
        source = "plain file"
        info = api("/api/info", {"url": PLAIN})
        if "error" in info:
            sys.exit(f"info failed for the plain file too: {info['error']}")
    v = info["video"][0]
    a = info["audio"]
    print(f"info ok ({source}): {info['title']!r}  {info['duration']} s  best {v['label']} {v['codec']}")

    url = info["url"]
    mp4 = {"url": url, "mode": "mp4", "video_fid": v["fid"], "audio_fid": a.get("mp4_fid"),
           "height": v["height"], "label": v["label"], "muxed": bool(v.get("muxed"))}
    run_job("full-mp4", mp4)
    run_job("clip-mp4", {**mp4, "clip": {"start": 2, "end": 6}})
    run_job("clip-mp3", {"url": url, "mode": "mp3", "bitrate": 320, "audio_fid": a.get("fid"),
                         "clip": {"start": 2, "end": 7}})
    run_job("full-mp3", {"url": url, "mode": "mp3", "bitrate": 192, "audio_fid": a.get("fid")})

    app.terminate()
    log = os.path.expanduser("~/Library/Application Support/YTConvert/ytconvert.log")
    if os.path.exists(log):
        print("--- app log (tail)")
        print("".join(open(log, encoding="utf-8", errors="replace").readlines()[-15:]))
    print(f"source: {source}")
    if failures:
        sys.exit("FAILED:\n  " + "\n  ".join(failures))
    print("ALL TESTS PASSED")


if __name__ == "__main__":
    main()
