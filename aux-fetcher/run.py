#!/usr/bin/env python3
"""
Aux Fetcher — tiny helper that runs on your home network.
It asks the Aux server for jobs ("download this YouTube page" / "search for this"), does them with yt-dlp
from your residential IP (YouTube doesn't bot-check those), and posts the result back.
Outbound HTTPS only. Nothing listens. Nothing touches Home Assistant.
"""
import json, os, socket, subprocess, sys, tempfile, time, urllib.request, urllib.parse, urllib.error

VERSION = "1.0.0"

def load_options():
    opts = {}
    try:
        with open("/data/options.json") as f:
            opts = json.load(f)
    except Exception:
        pass
    server = (os.environ.get("AUX_SERVER") or opts.get("server_url") or "").rstrip("/")
    key = os.environ.get("AUX_KEY") or opts.get("key") or ""
    return server, key

SERVER, KEY = load_options()
if not SERVER or not KEY:
    print("Set server_url and key in the add-on options (or AUX_SERVER / AUX_KEY).", flush=True)
    sys.exit(1)
HOST = socket.gethostname()

def req(method, path, data=None, headers=None, timeout=60):
    h = {"Authorization": f"Bearer {KEY}", "User-Agent": f"aux-fetcher/{VERSION}"}
    if headers: h.update(headers)
    r = urllib.request.Request(SERVER + path, data=data, method=method, headers=h)
    with urllib.request.urlopen(r, timeout=timeout) as resp:
        body = resp.read()
        return json.loads(body) if body else {}

def ytdlp(args, timeout):
    return subprocess.run(["yt-dlp", "--no-warnings", "--no-playlist", "--js-runtimes", "node", *args],
                          capture_output=True, text=True, timeout=timeout)

def do_download(job):
    url = job["url"]
    with tempfile.TemporaryDirectory() as d:
        out = os.path.join(d, "audio")
        p = ytdlp(["-f", "bestaudio[acodec=opus]/bestaudio/best", "--quiet", "--no-part", "-o", out, url], timeout=180)
        if p.returncode != 0 or not os.path.exists(out) or os.path.getsize(out) < 50_000:
            raise RuntimeError((p.stderr or "yt-dlp failed").strip().splitlines()[-1][:300])
        with open(out, "rb") as f:
            data = f.read()
    req("PUT", f"/fetch/jobs/{job['id']}?url={urllib.parse.quote(url, safe='')}", data=data,
        headers={"Content-Type": "application/octet-stream"}, timeout=180)
    print(f"✓ downloaded {url} ({len(data)//1024} KB)", flush=True)

def do_search(job):
    q, n = job["query"], int(job.get("count") or 6)
    p = ytdlp(["--dump-json", "--flat-playlist", f"ytsearch{n}:{q}"], timeout=60)
    rows = []
    for line in p.stdout.splitlines():
        try:
            j = json.loads(line)
        except Exception:
            continue
        if j.get("id") and len(j["id"]) == 11:
            rows.append({"id": j["id"], "title": j.get("title"), "duration": j.get("duration"),
                         "channel": j.get("channel") or j.get("uploader"), "uploader": j.get("uploader"),
                         "view_count": j.get("view_count")})
    if not rows:
        raise RuntimeError((p.stderr or "no results").strip().splitlines()[-1][:300] if p.stderr else "no results")
    req("POST", f"/fetch/jobs/{job['id']}/result", data=json.dumps({"rows": rows}).encode(),
        headers={"Content-Type": "application/json"})
    print(f"✓ search '{q}' → {len(rows)} results", flush=True)

def fail(job, msg):
    try:
        req("POST", f"/fetch/jobs/{job['id']}/fail", data=json.dumps({"error": msg}).encode(),
            headers={"Content-Type": "application/json"})
    except Exception:
        pass
    print(f"✗ {job.get('kind')} failed: {msg}", flush=True)

def self_update():
    try:
        subprocess.run([sys.executable, "-m", "pip", "install", "-q", "--upgrade", "--break-system-packages", "yt-dlp[default]"],
                       capture_output=True, timeout=300)
        print("yt-dlp up to date", flush=True)
    except Exception:
        pass

print(f"Aux Fetcher {VERSION} → {SERVER} (host {HOST})", flush=True)
last_update = time.time()
backoff = 2
while True:
    try:
        job = req("GET", f"/fetch/jobs?wait=25&v={VERSION}&host={urllib.parse.quote(HOST)}", timeout=40)
        backoff = 2
        if job and job.get("id"):
            try:
                if job.get("kind") == "download":
                    do_download(job)
                elif job.get("kind") == "search":
                    do_search(job)
                else:
                    fail(job, f"unknown job kind {job.get('kind')}")
            except Exception as e:
                fail(job, str(e))
    except urllib.error.HTTPError as e:
        if e.code == 401:
            print("Server rejected the key — check the add-on options.", flush=True); time.sleep(30)
        elif e.code == 404:
            print("Server has no FETCHER_KEY set yet (404). Retrying in 30s.", flush=True); time.sleep(30)
        else:
            print(f"HTTP {e.code}; retrying", flush=True); time.sleep(backoff); backoff = min(backoff * 2, 60)
    except Exception as e:
        print(f"connection problem ({e}); retrying in {backoff}s", flush=True)
        time.sleep(backoff); backoff = min(backoff * 2, 60)
    if time.time() - last_update > 86400:
        self_update(); last_update = time.time()
