#!/usr/bin/env python3
"""
Aux Fetcher — tiny helper that runs on your home network.
It asks the Aux server for jobs ("download this YouTube page" / "search for this"), does them with yt-dlp
from your residential IP (YouTube doesn't bot-check those), and posts the result back.
Outbound HTTPS only. Nothing listens. Nothing touches Home Assistant.
"""
import json, os, socket, subprocess, sys, tempfile, time, urllib.request, urllib.parse, urllib.error, hashlib, shutil, glob

VERSION = "1.2.0"

def load_options():
    opts = {}
    try:
        with open("/data/options.json") as f:
            opts = json.load(f)
    except Exception:
        pass
    server = (os.environ.get("AUX_SERVER") or opts.get("server_url") or "").rstrip("/")
    key = os.environ.get("AUX_KEY") or opts.get("key") or ""
    store = os.environ.get("AUX_STORE") or opts.get("store_dir") or "/share/aux"
    keep_days = int(os.environ.get("AUX_BACKUP_DAYS") or opts.get("backup_days") or 14)
    return server, key, store, keep_days

SERVER, KEY, STORE, BACKUP_DAYS = load_options()
# Song store (every download is kept here forever = instant replays even after the cloud cache forgets) + nightly DB backups.
CACHE_DIR = os.path.join(STORE, "songs"); BACKUP_DIR = os.path.join(STORE, "backups")
for d in (CACHE_DIR, BACKUP_DIR):
    try: os.makedirs(d, exist_ok=True)
    except Exception as e: print(f"can't create {d}: {e}", flush=True)
def cache_path(url): return os.path.join(CACHE_DIR, hashlib.sha1(url.encode()).hexdigest() + ".audio")
def store_stats():
    try:
        files = glob.glob(os.path.join(CACHE_DIR, "*.audio"))
        return len(files), sum(os.path.getsize(f) for f in files)
    except Exception: return 0, 0
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

def post_result(job, payload):
    req("POST", f"/fetch/jobs/{job['id']}/result", data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"})

def do_download(job):
    url = job["url"]
    kept = cache_path(url)
    keep_only = bool(job.get("keep_only"))   # "just keep a copy here, don't send it up" (pre-downloading someone's library)
    if os.path.exists(kept) and os.path.getsize(kept) > 50_000:
        if keep_only:
            os.utime(kept, None)
            post_result(job, {"kept": True, "cached": True, "bytes": os.path.getsize(kept)})
            print(f"✓ already kept {url}", flush=True)
            return
        with open(kept, "rb") as f: data = f.read()
        req("PUT", f"/fetch/jobs/{job['id']}?url={urllib.parse.quote(url, safe='')}", data=data,
            headers={"Content-Type": "application/octet-stream"}, timeout=180)
        os.utime(kept, None)
        print(f"✓ served from Pi store {url} ({len(data)//1024} KB)", flush=True)
        return
    with tempfile.TemporaryDirectory() as d:
        out = os.path.join(d, "audio")
        p = ytdlp(["-f", "bestaudio[acodec=opus]/bestaudio/best", "--quiet", "--no-part", "-o", out, url], timeout=180)
        if p.returncode != 0 or not os.path.exists(out) or os.path.getsize(out) < 50_000:
            raise RuntimeError((p.stderr or "yt-dlp failed").strip().splitlines()[-1][:300])
        try: shutil.copyfile(out, kept)
        except Exception as e:
            print(f"couldn't keep a copy on the Pi: {e}", flush=True)
            if keep_only: raise
        if keep_only:
            post_result(job, {"kept": True, "bytes": os.path.getsize(out)})
            print(f"✓ kept {url} ({os.path.getsize(out)//1024} KB) for later", flush=True)
            return
        with open(out, "rb") as f:
            data = f.read()
    req("PUT", f"/fetch/jobs/{job['id']}?url={urllib.parse.quote(url, safe='')}", data=data,
        headers={"Content-Type": "application/octet-stream"}, timeout=180)
    print(f"✓ downloaded {url} ({len(data)//1024} KB) and kept it", flush=True)

def do_backup():
    """Pull a consistent copy of the Aux database and keep BACKUP_DAYS days of them."""
    try:
        r = urllib.request.Request(SERVER + "/fetch/backup", headers={"Authorization": f"Bearer {KEY}", "User-Agent": f"aux-fetcher/{VERSION}"})
        with urllib.request.urlopen(r, timeout=120) as resp:
            dest = os.path.join(BACKUP_DIR, f"aux-{time.strftime('%Y-%m-%d')}.sqlite")
            with open(dest + ".part", "wb") as f: shutil.copyfileobj(resp, f)
            os.replace(dest + ".part", dest)
        files = sorted(glob.glob(os.path.join(BACKUP_DIR, "aux-*.sqlite")))
        for old in files[:-BACKUP_DAYS]: os.remove(old)
        n, b = store_stats()
        print(f"✓ database backed up to {dest} ({os.path.getsize(dest)//1024} KB) · Pi store: {n} songs, {b//1048576} MB", flush=True)
    except Exception as e:
        print(f"backup failed: {e} (will retry in an hour)", flush=True)
        return False
    return True

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

n0, b0 = store_stats()
print(f"Aux Fetcher {VERSION} → {SERVER} (host {HOST}) · store {STORE}: {n0} songs, {b0//1048576} MB", flush=True)
last_update = time.time()
last_backup = 0  # first backup right after boot, then daily
backoff = 2
while True:
    try:
        job = req("GET", f"/fetch/jobs?wait=25&v={VERSION}&host={urllib.parse.quote(HOST)}&songs={store_stats()[0]}", timeout=40)
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
    if time.time() - last_backup > 86400:
        last_backup = time.time() if do_backup() else time.time() - 86400 + 3600
