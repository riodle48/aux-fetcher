#!/usr/bin/env python3
"""
Aux Fetcher — tiny helper that runs on your home network.
It asks the Aux server for jobs ("download this YouTube page" / "search for this"), does them with yt-dlp
from your residential IP (YouTube doesn't bot-check those), and posts the result back.
Outbound HTTPS only. Nothing listens. Nothing touches Home Assistant.
"""
import json, os, socket, subprocess, sys, tempfile, time, urllib.request, urllib.parse, urllib.error, hashlib, shutil, glob, threading, re

VERSION = "1.5.2"

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
    max_gb = float(os.environ.get("AUX_MAX_STORE_GB") or opts.get("max_store_gb") or 160)
    return server, key, store, keep_days, max_gb

SERVER, KEY, STORE, BACKUP_DAYS, MAX_STORE_GB = load_options()
MAX_STORE_BYTES = int(MAX_STORE_GB * 1024 ** 3)
# Song store (every download is kept here forever = instant replays even after the cloud cache forgets) + nightly DB backups.
CACHE_DIR = os.path.join(STORE, "songs"); BACKUP_DIR = os.path.join(STORE, "backups")
for d in (CACHE_DIR, BACKUP_DIR):
    try: os.makedirs(d, exist_ok=True)
    except Exception as e: print(f"can't create {d}: {e}", flush=True)
def cache_path(url): return os.path.join(CACHE_DIR, hashlib.sha1(url.encode()).hexdigest() + ".audio")
_stats_cache = (0, 0, 0)   # (at, count, bytes) · walking 30k files every poll is pointless
def store_stats(fresh=False):
    global _stats_cache
    if not fresh and time.time() - _stats_cache[0] < 120: return _stats_cache[1], _stats_cache[2]
    try:
        files = glob.glob(os.path.join(CACHE_DIR, "*.audio"))
        n, b = len(files), sum(os.path.getsize(f) for f in files)
    except Exception: n, b = 0, 0
    _stats_cache = (time.time(), n, b)
    return n, b
_evict_lock = threading.Lock()
def make_room(incoming):
    """Keep the store under max_store_gb: drop the least-recently-played songs (mtime = last play/keep) until the new one fits."""
    with _evict_lock:
        n, b = store_stats(fresh=True)
        if b + incoming <= MAX_STORE_BYTES: return
        files = sorted(glob.glob(os.path.join(CACHE_DIR, "*.audio")), key=lambda f: os.path.getmtime(f))
        freed = 0
        for f in files:
            if b + incoming - freed <= MAX_STORE_BYTES: break
            try: sz = os.path.getsize(f); os.remove(f); freed += sz
            except Exception: pass
        store_stats(fresh=True)
        print(f"store at the {MAX_STORE_GB:g} GB cap → dropped the least-played songs ({freed//1048576} MB) to make room", flush=True)
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
        try: make_room(os.path.getsize(out)); shutil.copyfile(out, kept)
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

SWEEP_DAYS = 30
def do_sweep():
    """Once a day: drop songs nobody has played in 30 days (mtime = last play / last library warm, so songs in someone's
    library stay). Skipped when Aux itself has been quiet — a month of silence shouldn't wipe the store."""
    try:
        now = time.time()
        files = [(f, os.path.getmtime(f)) for f in glob.glob(os.path.join(CACHE_DIR, "*.audio"))]
        if not files: return
        recent = [f for f, m in files if now - m < SWEEP_DAYS * 86400]
        newest = max(m for _, m in files)
        if len(recent) < 30 or now - newest > 3 * 86400:
            print(f"sweep skipped: Aux hasn't been used much lately ({len(recent)} songs touched in {SWEEP_DAYS} days)", flush=True); return
        old = [(f, os.path.getsize(f)) for f, m in files if now - m >= SWEEP_DAYS * 86400]
        freed = 0
        for f, sz in old:
            try: os.remove(f); freed += sz
            except Exception: pass
        store_stats(fresh=True)
        if old: print(f"✓ sweep: dropped {len(old)} songs nobody played in {SWEEP_DAYS} days ({freed//1048576} MB freed)", flush=True)
    except Exception as e:
        print(f"sweep failed: {e}", flush=True)

def do_backups_list(job):
    """Admin page: which nightly backups are here."""
    rows = []
    for f in sorted(glob.glob(os.path.join(BACKUP_DIR, "aux-*.sqlite")), reverse=True):
        rows.append({"name": os.path.basename(f), "bytes": os.path.getsize(f), "at": int(os.path.getmtime(f) * 1000)})
    post_result(job, {"backups": rows})

def do_restore(job):
    """Admin pressed Restore: upload that backup to the server (it checks it and restarts to swap it in)."""
    name = os.path.basename(str(job.get("name") or ""))
    path = os.path.join(BACKUP_DIR, name)
    if not name.startswith("aux-") or not name.endswith(".sqlite") or not os.path.exists(path):
        raise RuntimeError(f"no backup called {name} on the Pi")
    with open(path, "rb") as f: data = f.read()
    req("PUT", f"/fetch/restore/{job['id']}", data=data, headers={"Content-Type": "application/octet-stream"}, timeout=300)
    print(f"✓ uploaded backup {name} ({len(data)//1024} KB) for restore", flush=True)

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

UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"

def parse_len(s):
    """'3:45' → 225, '1:02:03' → 3723."""
    try:
        parts = [int(x) for x in str(s or "").split(":")]
        out = 0
        for x in parts: out = out * 60 + x
        return out or None
    except Exception:
        return None

def search_fast(q, n):
    """YouTube's own search endpoint (what the website calls) · ~0.3s instead of a 3-5s yt-dlp run."""
    body = json.dumps({"context": {"client": {"clientName": "WEB", "clientVersion": "2.20250101.00.00", "hl": "en", "gl": "US"}},
                       "query": q, "params": "EgIQAQ%3D%3D"}).encode()
    r = urllib.request.Request("https://www.youtube.com/youtubei/v1/search?prettyPrint=false", data=body, method="POST",
                               headers={"Content-Type": "application/json", "User-Agent": UA, "Accept-Language": "en-US,en;q=0.9",
                                        "X-YouTube-Client-Name": "1", "X-YouTube-Client-Version": "2.20250101.00.00", "Origin": "https://www.youtube.com"})
    with urllib.request.urlopen(r, timeout=8) as resp:
        j = json.load(resp)
    rows = []
    def walk(o):
        if len(rows) >= n: return
        if isinstance(o, dict):
            v = o.get("videoRenderer")
            if v and v.get("videoId"):
                owner = ((v.get("ownerText") or {}).get("runs") or [{}])[0].get("text", "")
                views = re.sub(r"\D", "", ((v.get("viewCountText") or {}).get("simpleText") or ""))
                rows.append({"id": v["videoId"], "title": "".join(x.get("text", "") for x in (v.get("title") or {}).get("runs") or []),
                             "duration": parse_len((v.get("lengthText") or {}).get("simpleText")), "channel": owner, "uploader": owner,
                             "view_count": int(views) if views else 0})
                return
            for val in o.values(): walk(val)
        elif isinstance(o, list):
            for val in o: walk(val)
    walk(j)
    if not rows: raise RuntimeError("innertube returned no videos")
    return rows

def do_fetch(job):
    """Plain GET from the house (lyrics sites etc. that block datacenter IPs). Returns status + body text (capped)."""
    url = job["url"]
    r = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": job.get("accept") or "*/*", "Accept-Language": "en-US,en;q=0.9"})
    try:
        with urllib.request.urlopen(r, timeout=12) as resp:
            body = resp.read(900_000).decode("utf-8", "replace")
            post_result(job, {"status": resp.status, "body": body})
            print(f"✓ fetch {url[:80]} → {resp.status} ({len(body)//1024} KB)", flush=True)
    except urllib.error.HTTPError as e:
        post_result(job, {"status": e.code, "body": ""})
        print(f"✓ fetch {url[:80]} → HTTP {e.code}", flush=True)

# ---------------------------------------------------------------- Spotify dashboard (allow-list)
def spotify_cookie_jar():
    """Netscape cookies.txt → Playwright cookies. From /share/aux/spotify-cookies.txt, or the add-on option."""
    path = os.path.join(STORE, "spotify-cookies.txt")
    text = ""
    if os.path.exists(path):
        with open(path) as f: text = f.read()
    else:
        try:
            with open("/data/options.json") as f: text = (json.load(f).get("spotify_cookies") or "")
        except Exception: text = ""
    out = []
    for line in text.replace("\\n", "\n").splitlines():
        if not line.strip() or line.startswith("#"): continue
        f = line.split("\t")
        if len(f) < 7: continue
        domain, _flag, cpath, secure, expires, name, value = f[:7]
        if "spotify.com" not in domain: continue
        c = {"name": name, "value": value.strip(), "domain": domain, "path": cpath or "/", "secure": secure.upper() == "TRUE", "httpOnly": False}
        try:
            if int(float(expires)) > 0: c["expires"] = int(float(expires))
        except Exception: pass
        out.append(c)
    return out

def have_spotify_cookies():
    """Cheap check for the poll: are the owner's dashboard cookies on this box at all?"""
    try:
        if os.path.getsize(os.path.join(STORE, "spotify-cookies.txt")) > 50: return True
    except Exception: pass
    try:
        with open("/data/options.json") as f: return len((json.load(f).get("spotify_cookies") or "").strip()) > 50
    except Exception: return False

def do_spotify_allowlist(job):
    """Open the Spotify developer dashboard as the owner and add one person to the app's User Management list."""
    from playwright.sync_api import sync_playwright
    client_id, name, email = job["client_id"], (job.get("name") or "Aux user")[:60], job["email"].strip()
    cookies = spotify_cookie_jar()
    if not cookies:
        raise RuntimeError("no Spotify cookies on the Pi yet (put cookies.txt at /share/aux/spotify-cookies.txt or in the add-on option)")
    with sync_playwright() as pw:
        browser = pw.chromium.launch(executable_path=os.environ.get("AUX_CHROMIUM") or None, headless=True, args=["--no-sandbox", "--disable-dev-shm-usage"])
        ctx = browser.new_context(user_agent=UA, viewport={"width": 1280, "height": 900})
        ctx.add_cookies(cookies)
        page = ctx.new_page()
        try:
            page.goto(f"https://developer.spotify.com/dashboard/{client_id}/users", wait_until="domcontentloaded", timeout=45000)
            page.wait_for_timeout(2500)
            if "accounts.spotify.com" in page.url or page.locator("text=Log in").count() and page.locator("#email").count() == 0:
                raise RuntimeError("Spotify dashboard login expired · the owner needs to paste fresh cookies")
            # cookie banner, if it shows up, would eat the click
            for sel in ["#onetrust-accept-btn-handler", "button:has-text('Confirm My Choices')"]:
                try:
                    if page.locator(sel).count(): page.locator(sel).first.click(timeout=2000)
                except Exception: pass
            page.wait_for_selector("#email", timeout=20000)
            if page.locator(f"text={email}").count():
                post_result(job, {"added": True, "already": True}); print(f"✓ {email} already on the Spotify allow-list", flush=True); return
            page.fill("#name", name)
            page.fill("#email", email)
            page.get_by_role("button", name="Add user").click(timeout=10000)
            # success = the email shows up in the table
            ok = False
            for _ in range(20):
                page.wait_for_timeout(500)
                if page.locator(f"text={email}").count(): ok = True; break
            if not ok:
                err = ""
                for sel in ["[role=alert]", ".error", "text=/already|invalid|limit|maximum/i"]:
                    try:
                        if page.locator(sel).count(): err = page.locator(sel).first.inner_text()[:200]; break
                    except Exception: pass
                raise RuntimeError(f"dashboard didn't confirm the add{(': ' + err) if err else ''}")
            post_result(job, {"added": True})
            print(f"✓ added {email} to the Spotify allow-list", flush=True)
        finally:
            try: browser.close()
            except Exception: pass

def do_search(job):
    q, n = job["query"], int(job.get("count") or 6)
    try:
        rows = search_fast(q, n)
        post_result(job, {"rows": rows})
        print(f"✓ search '{q}' → {len(rows)} results (fast)", flush=True)
        return
    except Exception as e:
        print(f"fast search failed ({e}); using yt-dlp", flush=True)
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
print(f"Aux Fetcher {VERSION} → {SERVER} (host {HOST}) · store {STORE}: {n0} songs, {b0//1048576} MB (cap {MAX_STORE_GB:g} GB)", flush=True)
last_update = time.time()
last_backup = 0  # first backup right after boot, then daily
backoff = 2
MAX_JOBS = 3          # a play request never waits behind a library pre-download
active = threading.Semaphore(MAX_JOBS)

def run_job(job):
    try:
        try:
            if job.get("kind") == "download":
                do_download(job)
            elif job.get("kind") == "search":
                do_search(job)
            elif job.get("kind") == "fetch":
                do_fetch(job)
            elif job.get("kind") == "spotify_allowlist":
                do_spotify_allowlist(job)
            elif job.get("kind") == "backups":
                do_backups_list(job)
            elif job.get("kind") == "restore":
                do_restore(job)
            else:
                fail(job, f"unknown job kind {job.get('kind')}")
        except Exception as e:
            fail(job, str(e))
    finally:
        active.release()

while True:
    try:
        active.acquire()           # wait for a free worker slot before asking for more work
        try:
            job = req("GET", f"/fetch/jobs?wait=25&v={VERSION}&host={urllib.parse.quote(HOST)}&songs={store_stats()[0]}&bytes={store_stats()[1]}&cap_gb={MAX_STORE_GB:g}&sc={1 if have_spotify_cookies() else 0}", timeout=40)
        except Exception:
            active.release(); raise
        backoff = 2
        if job and job.get("id"):
            threading.Thread(target=run_job, args=(job,), daemon=True).start()
        else:
            active.release()
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
        do_sweep()
