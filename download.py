#!/usr/bin/env python3
"""
game_archiver.py — HTML5 Game Archiver & Offline Server
========================================================
Automatically archives any HTML5 game for fully offline play.

HOW IT WORKS
  1. Ask for the game URL
  2. Launch a real browser (Chromium via Playwright) pointed at a local
     caching server — every network request the game makes is intercepted,
     the real file is fetched, rewritten so all URLs point locally, and
     saved to disk
  3. Repeat until a full browser session produces zero new downloads
  4. Switch the server to pure-offline mode and open the game

REQUIREMENTS
  pip install playwright
  playwright install chromium

USAGE
  python game_archiver.py
  python game_archiver.py https://example.com/mygame/
  python game_archiver.py https://example.com/mygame/ --port 9000
"""

import os, re, sys, gzip, json, time, threading, webbrowser, shutil, socket
import urllib.request, urllib.error
from http.server      import HTTPServer, BaseHTTPRequestHandler
from socketserver     import ThreadingMixIn
from urllib.parse     import urlparse, urljoin, unquote, quote
from html.parser      import HTMLParser
from pathlib          import Path

# ═══════════════════════════════════════════════════════════════════
# Config
# ═══════════════════════════════════════════════════════════════════

VERSION       = "2.0"
GAMES_DIR     = "games"
SERVER_HOST   = "localhost"
SERVER_PORT   = 8080
CRAWL_WAIT_S  = 4      # seconds to wait after page-load for JS to settle
PASS_IDLE_S   = 3      # seconds of network silence = pass is done
MAX_PASSES    = 8      # maximum crawl passes before giving up

FETCH_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/122.0.0.0 Safari/537.36"
    ),
    "Accept": "*/*",
    "Accept-Language": "en-US,en;q=0.9",
}

MIME_MAP = {
    ".html": "text/html; charset=utf-8",
    ".htm":  "text/html; charset=utf-8",
    ".js":   "application/javascript; charset=utf-8",
    ".mjs":  "application/javascript; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".css":  "text/css; charset=utf-8",
    ".xml":  "text/xml; charset=utf-8",
    ".txt":  "text/plain; charset=utf-8",
    ".png":  "image/png",
    ".jpg":  "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif":  "image/gif",
    ".webp": "image/webp",
    ".svg":  "image/svg+xml",
    ".ico":  "image/x-icon",
    ".wav":  "audio/wav",
    ".mp3":  "audio/mpeg",
    ".ogg":  "audio/ogg",
    ".mp4":  "video/mp4",
    ".webm": "video/webm",
    ".bin":  "application/octet-stream",
    ".ls":   "application/octet-stream",
    ".lh":   "application/octet-stream",
    ".lm":   "application/octet-stream",
    ".atlas":"application/octet-stream",
    ".ttf":  "font/ttf",
    ".otf":  "font/otf",
    ".woff": "font/woff",
    ".woff2":"font/woff2",
    ".zip":  "application/zip",
    ".php":  "application/json",
}

TEXT_MIME_FRAGMENTS = {"javascript", "json", "html", "css", "text/", "xml"}

# Hosts whose responses we stub out (ads / analytics / social)
STUB_HOST_KEYWORDS = [
    "google-analytics", "googletagmanager", "doubleclick", "googlesyndication",
    "adservice", "facebook.net", "twitter.com/widgets", "hotjar",
    "moatads", "adnxs", "adsystem", "amazon-adsystem",
    "gamemonetize.com", "yyggames.com",
]

# ═══════════════════════════════════════════════════════════════════
# Utilities
# ═══════════════════════════════════════════════════════════════════

def slugify(text: str) -> str:
    text = re.sub(r'[^\w\-]', '_', text)
    return re.sub(r'_+', '_', text).strip('_.') [:60] or "game"


def is_stub_host(url: str) -> bool:
    return any(kw in url for kw in STUB_HOST_KEYWORDS)


def is_text_mime(mime: str) -> bool:
    return any(f in (mime or "") for f in TEXT_MIME_FRAGMENTS)


def encode_url(url: str) -> str:
    return quote(url, safe=":/?#[]@!$&'()*+,;=%~")


def safe_makedirs(path: str) -> bool:
    try:
        os.makedirs(path, exist_ok=True)
        return True
    except OSError:
        return False


def host_slug(netloc: str) -> str:
    """dots/colons → underscores so hostname is a single path component."""
    return re.sub(r'[^\w\-]', '_', netloc.split(":")[0]).strip('_')


# ═══════════════════════════════════════════════════════════════════
# URL ↔ local-path mapping
# ═══════════════════════════════════════════════════════════════════

class UrlMapper:
    """
    Converts between remote absolute URLs and local cache paths.

    Primary origin   →  cache_dir/  (root)
    Other origins    →  cache_dir/ext/<host_slug>/...
    """

    def __init__(self, game_origin: str, cache_dir: str):
        self.origin    = game_origin.rstrip("/")
        self.cache_dir = cache_dir

    def remote_to_local_path(self, url: str) -> str:
        """Return the absolute filesystem path for a remote URL."""
        url = url.split("?")[0].split("#")[0].rstrip("/")
        rel = self._remote_to_rel(url)
        # Convert forward slashes to OS separator, guard against traversal
        parts = [p for p in rel.split("/") if p and p != ".."]
        return os.path.join(self.cache_dir, *parts) if parts else \
               os.path.join(self.cache_dir, "index.html")

    def set_entry_url(self, entry_url: str):
        """Record the real entry URL so /index.html maps back correctly."""
        self._entry_url = entry_url

    def local_request_to_remote(self, req_path: str) -> str:
        """Convert a local HTTP request path back to the remote URL."""
        req_path = req_path.split("?")[0]
        # / and /index.html both map to the real entry URL
        if req_path in ("/", "", "/index.html"):
            return getattr(self, "_entry_url", self.origin + "/")
        m = re.match(r'^/ext/([^/]+)(/.+)$', req_path)
        if m:
            slug, path = m.group(1), m.group(2)
            real = self._slug_to_host(slug)
            return f"https://{real}{path}"
        return self.origin + req_path

    def rewrite_text(self, content: str) -> str:
        """Replace all remote URLs in a text file with local equivalents."""
        # Replace primary origin
        for variant in (self.origin + "/", self.origin):
            content = content.replace(variant, "/")
        # Replace ext origins
        for remote_host, slug in self._ext_map().items():
            for variant in (f"https://{remote_host}/", f"https://{remote_host}",
                            f"http://{remote_host}/",  f"http://{remote_host}"):
                content = content.replace(variant, f"/ext/{slug}/")
        return content

    # ── internal ─────────────────────────────────────────────────

    def _remote_to_rel(self, url: str) -> str:
        if url.startswith(self.origin):
            rel = url[len(self.origin):]
            return rel.lstrip("/") or "index.html"
        parsed = urlparse(url)
        slug   = host_slug(parsed.netloc)
        self._register_host(parsed.netloc, slug)
        path   = parsed.path.lstrip("/")
        return f"ext/{slug}/{path}" if path else f"ext/{slug}/index.html"

    def _hosts_file(self) -> str:
        return os.path.join(self.cache_dir, ".ext_hosts.json")

    def _ext_map(self) -> dict:           # { real_hostname: slug }
        p = self._hosts_file()
        if os.path.isfile(p):
            try:
                return json.load(open(p))
            except Exception:
                pass
        return {}

    def _register_host(self, netloc: str, slug: str):
        m = self._ext_map()
        host = netloc.split(":")[0]
        if host not in m:
            m[host] = slug
            safe_makedirs(os.path.dirname(self._hosts_file()))
            json.dump(m, open(self._hosts_file(), "w"), indent=2)

    def _slug_to_host(self, slug: str) -> str:
        for host, s in self._ext_map().items():
            if s == slug:
                return host
        # fallback: undo underscore→dot heuristic
        return slug.replace('_', '.')


# ═══════════════════════════════════════════════════════════════════
# Remote fetcher
# ═══════════════════════════════════════════════════════════════════

def fetch_remote(url: str, referer: str = "") -> tuple:
    """Returns (body_bytes, content_type_str) or (None, None)."""
    safe_url = encode_url(url)
    headers  = dict(FETCH_HEADERS)
    if referer:
        headers["Referer"] = referer
    try:
        req = urllib.request.Request(safe_url, headers=headers)
        with urllib.request.urlopen(req, timeout=30) as r:
            body = r.read()
            ct   = r.headers.get("Content-Type", "application/octet-stream")
            if r.headers.get("Content-Encoding") == "gzip":
                body = gzip.decompress(body)
            return body, ct
    except urllib.error.HTTPError as e:
        print(f"    [HTTP {e.code}] {url}")
        return None, None
    except Exception as e:
        print(f"    [ERR] {url} — {e}")
        return None, None


# ═══════════════════════════════════════════════════════════════════
# Shim injected into the entry HTML
# ═══════════════════════════════════════════════════════════════════

SHIM_MARKER = "/* game-archiver-shim */"

SHIM = f"""<script>
{SHIM_MARKER}
(function(){{
  // ── Null-guards ───────────────────────────────────────────────────────────
  var _oe=Object.entries,_ok=Object.keys;
  Object.entries=function(o){{return o==null?[]:_oe(o);}};
  Object.keys   =function(o){{return o==null?[]:_ok(o);}};

  // ── Stub keywords (ad/analytics hosts that should return empty) ───────────
  var stubKw={json.dumps(STUB_HOST_KEYWORDS)};
  function isStub(u){{
    u=String(u||'');
    for(var i=0;i<stubKw.length;i++)if(u.indexOf(stubKw[i])>=0)return true;
    return false;
  }}

  // ── Route any absolute https?:// URL through our local CORS proxy ─────────
  // This catches URLs that weren't rewritten in the source files at save time.
  function proxify(url){{
    if(typeof url!=='string')return url;
    if(/^https?:/.test(url)&&url.indexOf('//')===url.indexOf('/')+1){{
      if(isStub(url))return null;          // signal: return stub
      // Already pointing at localhost? Leave it alone.
      if(url.indexOf('localhost')>=0||url.indexOf('127.0.0.1')>=0)return url;
      return '/proxy?url='+encodeURIComponent(url);
    }}
    return url;
  }}

  // ── Patch fetch ───────────────────────────────────────────────────────────
  var _f=window.fetch;
  window.fetch=function(url,opts){{
    if(isStub(url))return Promise.resolve(
      new Response('{{}}',{{status:200,headers:{{'Content-Type':'application/json'}}}}));
    var p=proxify(url);
    if(p===null)return Promise.resolve(
      new Response('{{}}',{{status:200,headers:{{'Content-Type':'application/json'}}}}));
    return _f?_f.call(this,p,opts):Promise.reject(new Error('no fetch'));
  }};

  // ── Patch XMLHttpRequest ──────────────────────────────────────────────────
  var _XHRopen=XMLHttpRequest.prototype.open;
  XMLHttpRequest.prototype.open=function(method,url,async,user,pass){{
    if(isStub(url)){{
      // open on a dummy local endpoint that returns {{}}
      return _XHRopen.call(this,method,'/proxy?stub=1',
        async===undefined?true:async,user,pass);
    }}
    var p=proxify(url);
    return _XHRopen.call(this,method,p||url,
      async===undefined?true:async,user,pass);
  }};
}})();
</script>"""

def inject_shim(html: str) -> str:
    if SHIM_MARKER in html:
        return html
    if re.search(r'<head\b', html, re.I):
        return re.sub(r'(?i)(<head\b[^>]*>)', r'\1\n' + SHIM, html, count=1)
    return SHIM + "\n" + html


# ═══════════════════════════════════════════════════════════════════
# Caching HTTP server
# ═══════════════════════════════════════════════════════════════════

class CachingServer:
    """
    Serves files from the cache dir.
    On a miss: fetches from remote, rewrites, saves, serves.
    Tracks how many new files were saved this pass.
    """

    def __init__(self, mapper: UrlMapper, origin: str,
                 host=SERVER_HOST, port=SERVER_PORT):
        self.mapper      = mapper
        self.origin      = origin
        self.host        = host
        self.port        = port
        self.new_files   = 0          # reset between passes
        self._httpd      = None
        self._thread     = None
        self._lock       = threading.Lock()

    def start_background(self):
        handler = self._make_handler()

        class ThreadedHTTPServer(ThreadingMixIn, HTTPServer):
            # Each request handled in its own thread — fixes page reload,
            # parallel asset fetches, and re-opening closed tabs.
            daemon_threads = True
            # Allow rapid restart without "address already in use"
            allow_reuse_address = True

        self._httpd = ThreadedHTTPServer((self.host, self.port), handler)
        # SO_REUSEADDR so reloading the script doesn't block on TIME_WAIT
        self._httpd.socket.setsockopt(
            __import__('socket').SOL_SOCKET,
            __import__('socket').SO_REUSEADDR, 1)
        self._thread = threading.Thread(target=self._httpd.serve_forever, daemon=True)
        self._thread.start()
        print(f"  Server up  →  http://{self.host}:{self.port}")

    def stop(self):
        if self._httpd:
            self._httpd.shutdown()

    def reset_counter(self):
        with self._lock:
            self.new_files = 0

    def get_counter(self) -> int:
        with self._lock:
            return self.new_files

    def _inc(self):
        with self._lock:
            self.new_files += 1

    def _make_handler(self):
        srv = self

        class Handler(BaseHTTPRequestHandler):
            # Disable Nagle — sends headers+body together, reduces latency
            disable_nagle_algorithm = True
            # Longer timeout so slow remote fetches don't drop the connection
            timeout = 60

            def log_message(self, *a): pass

            def do_OPTIONS(self):
                # CORS preflight — always approve
                self.send_response(204)
                self.send_header("Access-Control-Allow-Origin", "*")
                self.send_header("Access-Control-Allow-Methods", "GET, HEAD, OPTIONS")
                self.send_header("Access-Control-Allow-Headers", "*")
                self.send_header("Access-Control-Max-Age", "86400")
                self.end_headers()

            def do_HEAD(self):
                # Some browsers send HEAD before GET; handle it properly
                self._handle(head_only=True)

            def do_GET(self):
                self._handle(head_only=False)

            def _handle(self, head_only=False):
                raw  = unquote(self.path)

                # ── /proxy?url=<encoded>  — CORS proxy for absolute URLs ──────
                # The shim redirects any absolute https:// fetch here so the
                # browser never makes a cross-origin request directly.
                if raw.startswith("/proxy?"):
                    from urllib.parse import parse_qs
                    qs = parse_qs(raw[7:])   # strip "/proxy?"
                    # stub shortcut
                    if qs.get("stub"):
                        self._send(200, b"{}", "application/json", head_only)
                        return
                    urls = qs.get("url", [])
                    if not urls:
                        self._send(400, b"missing url", "text/plain", head_only)
                        return
                    target = urls[0]
                    # Treat as a normal local path so it gets cached properly
                    # Rewrite the target URL to a local path and recurse
                    local_req = srv.mapper._remote_to_rel(target)
                    local_req = "/" + local_req.lstrip("/")
                    self._handle_path(local_req, target, head_only)
                    return

                path = raw.split("?")[0]
                if path in ("/", ""):
                    path = "/index.html"

                self._handle_path(path, None, head_only)

            def _handle_path(self, path, override_remote, head_only):
                # Stub ad/analytics immediately
                if is_stub_host(path):
                    body = b"/* stub */" if path.endswith(".js") else b"{}"
                    ct   = "application/javascript" if path.endswith(".js") else "application/json"
                    self._send(200, body, ct, head_only)
                    return

                lpath = srv.mapper.remote_to_local_path(
                    srv.origin.rstrip("/") + path)

                # Serve from cache
                if os.path.isfile(lpath):
                    self._serve_local(lpath, path, head_only)
                    return

                # Cache miss — fetch, rewrite, save, serve
                remote = override_remote or srv.mapper.local_request_to_remote(path)
                if is_stub_host(remote):
                    self._send(200, b"{}", "application/json", head_only)
                    return

                print(f"  [miss]  {path}")
                body, ct = fetch_remote(remote, referer=srv.origin + "/")
                if body is None:
                    self._send(404, b"Not found", "text/plain", head_only)
                    return

                if is_text_mime(ct or ""):
                    text = body.decode("utf-8", errors="replace")
                    text = srv.mapper.rewrite_text(text)
                    if path == "/index.html":
                        text = inject_shim(text)
                    body = text.encode("utf-8")

                dir_ = os.path.dirname(lpath)
                if safe_makedirs(dir_):
                    try:
                        with open(lpath, "wb") as f:
                            f.write(body)
                        print(f"  [saved] {lpath}")
                        srv._inc()
                    except OSError as e:
                        print(f"  [SKIP]  {lpath}: {e}")
                else:
                    print(f"  [SKIP]  cannot create dir: {dir_}")

                self._send(200, body, ct or "application/octet-stream", head_only)

            def _serve_local(self, lpath: str, req_path: str, head_only=False):
                ext  = os.path.splitext(lpath)[1].lower()
                mime = MIME_MAP.get(ext, "application/octet-stream")
                with open(lpath, "rb") as f:
                    body = f.read()
                if is_text_mime(mime):
                    text = body.decode("utf-8", errors="replace")
                    rw   = srv.mapper.rewrite_text(text)
                    if req_path == "/index.html":
                        rw = inject_shim(rw)
                    if rw != text:
                        body = rw.encode("utf-8")
                self._send(200, body, mime, head_only)

            def _send(self, code, body, ct, head_only=False):
                self.send_response(code)
                self.send_header("Content-Type", ct)
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Access-Control-Allow-Origin", "*")
                self.send_header("Cache-Control", "no-cache")
                # Keep the connection alive so the browser can reuse it
                # for the next parallel request (essential for page reload)
                self.send_header("Connection", "keep-alive")
                self.end_headers()
                if not head_only:
                    try:
                        self.wfile.write(body)
                    except (BrokenPipeError, ConnectionResetError, OSError):
                        pass

        return Handler


# ═══════════════════════════════════════════════════════════════════
# Playwright crawl pass
# ═══════════════════════════════════════════════════════════════════

def run_crawl_pass(local_url: str, pass_num: int, idle_seconds: float,
                   local_host: str = "localhost", local_port: int = 8080) -> bool:
    """
    Open the game in a headless Chromium browser.

    Every network request the browser makes — regardless of origin, resource
    type, or how it was initiated — is intercepted at the network layer and
    redirected through the local caching server.  This means:
      - <script src="https://cdn.example.com/...">
      - <img>, <audio>, <video> tags
      - fetch() / XHR (also caught by the JS shim as a backup)
      - Web Workers, WebAssembly fetches, CSS @import, font loads
    All go through the local server, get cached, and get their URLs rewritten.
    """
    try:
        from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout
    except ImportError:
        print("  ERROR: Playwright not installed.")
        print("  Run:  pip install playwright && playwright install chromium")
        return False

    server_base = f"http://{local_host}:{local_port}"

    print(f"\n── Crawl pass {pass_num} ──────────────────────────────────────")
    print(f"  Opening  {local_url}  in headless browser …")

    def route_handler(route):
        """
        Intercept every browser request and redirect external URLs through
        the local server's /proxy endpoint so they get cached.
        """
        url = route.request.url

        # Already going to our local server — let it through unchanged
        if url.startswith(server_base):
            route.continue_()
            return

        # Everything else: redirect through /proxy?url=<encoded>
        # The local server will fetch it, rewrite it, cache it, and return it.
        proxy_url = f"{server_base}/proxy?url={urllib.parse.quote(url, safe='')}"
        route.continue_(url=proxy_url)

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        ctx = browser.new_context(
            viewport={"width": 1280, "height": 720},
            user_agent=FETCH_HEADERS["User-Agent"],
            # Disable the browser's own CORS enforcement — we handle it
            bypass_csp=True,
        )
        page = ctx.new_page()

        # Intercept ALL requests at the network layer
        page.route("**/*", route_handler)

        # Suppress console noise
        page.on("console", lambda _: None)
        page.on("pageerror", lambda _: None)

        try:
            page.goto(local_url, wait_until="domcontentloaded", timeout=30_000)
        except PWTimeout:
            print("  [warn] page load timed out — continuing anyway")
        except Exception as e:
            print(f"  [warn] goto error: {e}")

        # Wait for network idle
        try:
            page.wait_for_load_state(
                "networkidle",
                timeout=int(idle_seconds * 1000 + 20_000)
            )
        except PWTimeout:
            pass

        # Extra settle time for deferred/lazy asset loads
        time.sleep(CRAWL_WAIT_S)

        browser.close()

    print(f"  Pass {pass_num} done.")
    return True


# ═══════════════════════════════════════════════════════════════════
# Config persistence
# ═══════════════════════════════════════════════════════════════════

def cfg_path(cache_dir: str) -> str:
    return os.path.join(cache_dir, ".archiver.json")

def save_cfg(cache_dir: str, data: dict):
    safe_makedirs(cache_dir)
    with open(cfg_path(cache_dir), "w") as f:
        json.dump(data, f, indent=2)

def load_cfg(cache_dir: str) -> dict:
    p = cfg_path(cache_dir)
    if os.path.isfile(p):
        try:
            return json.load(open(p))
        except Exception:
            pass
    return {}


# ═══════════════════════════════════════════════════════════════════
# Main
# ═══════════════════════════════════════════════════════════════════

def _rewrite_for_origin(text: str, origin: str) -> str:
    """Quick URL rewrite without needing a UrlMapper instance."""
    o = origin.rstrip("/")
    text = text.replace(o + "/", "/")
    text = text.replace(o, "/")
    return text


def resolve_entry(url: str) -> tuple:
    """
    Given any game URL, work out:
      - the real entry URL  (after redirects)
      - the game origin     (the directory that acts as "/")
      - the raw HTML bytes of the entry page

    Handles all of these input shapes:
      https://example.com/game/          -> origin = https://example.com/game
      https://example.com/game           -> origin = https://example.com/game
      https://example.com/game/index.html -> origin = https://example.com/game
      https://example.com/               -> origin = https://example.com
    Returns (entry_url, origin, body_bytes) or (None, None, None) on failure.
    """
    safe_url = encode_url(url)
    headers  = dict(FETCH_HEADERS)
    headers["Accept"] = "text/html,application/xhtml+xml,*/*"

    try:
        req = urllib.request.Request(safe_url, headers=headers)
        with urllib.request.urlopen(req, timeout=30) as resp:
            final_url = resp.url          # real URL after any redirects
            ct        = resp.headers.get("Content-Type", "")
            body      = resp.read()
            if resp.headers.get("Content-Encoding") == "gzip":
                body = gzip.decompress(body)
    except Exception as e:
        print(f"  [ERR] {url} — {e}")
        return None, None, None

    parsed = urlparse(final_url)

    # If the response is HTML, this IS the entry page.
    # The origin is the directory containing it.
    if "html" in ct or body.lstrip()[:15].lower().startswith((b"<!doc", b"<html")):
        path = parsed.path  # e.g. /game/index.html  or  /game/  or  /game

        # Strip the filename if present (anything after the last / that has a dot)
        if "/" in path:
            last_seg = path.rsplit("/", 1)[-1]
            if "." in last_seg:
                # It's a file — origin is the parent directory
                origin_path = path.rsplit("/", 1)[0]
            else:
                # It's a directory path — strip trailing slash
                origin_path = path.rstrip("/")
        else:
            origin_path = ""

        origin = f"{parsed.scheme}://{parsed.netloc}{origin_path}".rstrip("/")

        # Rewrite HTML so URLs are root-relative from origin
        # Rewrite URLs inline without needing a real cache dir
        text = body.decode("utf-8", errors="replace")
        text = _rewrite_for_origin(text, origin)
        text = inject_shim(text)
        body = text.encode("utf-8")

        return final_url, origin, body

    # Not HTML — maybe the URL itself was a redirect to a non-HTML resource,
    # or a bare domain.  Try appending /index.html as a fallback.
    for candidate in (final_url.rstrip("/") + "/index.html",
                      final_url.rstrip("/") + "/"):
        b, c = fetch_remote(candidate)
        if b and ("html" in (c or "") or b.lstrip()[:15].lower().startswith((b"<!doc", b"<html"))):
            return resolve_entry(candidate)

    print(f"  [WARN] Could not identify an HTML entry point at {final_url}")
    return None, None, None


def pick_port(preferred: int) -> int:
    """Return preferred port if free, else find a free one."""
    for p in [preferred] + list(range(8081, 8100)):
        with socket.socket() as s:
            try:
                s.bind(("localhost", p))
                return p
            except OSError:
                continue
    return preferred


def main():
    print("=" * 62)
    print(f"  HTML5 Game Archiver  v{VERSION}")
    print("=" * 62)

    # ── CLI args ──────────────────────────────────────────────────
    args, game_url, port = sys.argv[1:], None, SERVER_PORT
    i = 0
    while i < len(args):
        if args[i] in ("--port", "-p") and i + 1 < len(args):
            port = int(args[i+1]); i += 2
        elif not args[i].startswith("-"):
            game_url = args[i]; i += 1
        else:
            i += 1

    # ── Show existing archives ────────────────────────────────────
    existing = []
    if os.path.isdir(GAMES_DIR):
        for name in sorted(os.listdir(GAMES_DIR)):
            c = load_cfg(os.path.join(GAMES_DIR, name))
            if c.get("url"):
                n = sum(len(fs) for _,_,fs in os.walk(os.path.join(GAMES_DIR, name)))
                existing.append((name, c, n))

    if existing and not game_url:
        print("\nArchived games:")
        for idx, (name, c, n) in enumerate(existing, 1):
            print(f"  [{idx}] {c['url']}  ({n} files)")
        print(f"  [N] New game")
        choice = input("\nChoice: ").strip().upper()
        if choice.isdigit() and 1 <= int(choice) <= len(existing):
            name, c, _ = existing[int(choice) - 1]
            game_url   = c["url"]
            cache_dir  = os.path.join(GAMES_DIR, name)
        # else fall through to prompt

    if not game_url:
        game_url = input("\nGame URL: ").strip()
    if not game_url:
        print("No URL given."); return
    if not game_url.startswith("http"):
        game_url = "https://" + game_url

    # ── Resolve the real entry URL (follow redirects, find HTML) ────
    print(f"\n  Probing  {game_url} …")
    entry_url, game_origin, entry_body = resolve_entry(game_url)
    if entry_url is None:
        print("  ERROR: Could not fetch the game URL. Check it and try again.")
        return

    print(f"  Entry   →  {entry_url}")
    print(f"  Origin  →  {game_origin}")

    # ── Derive cache dir from the resolved origin ─────────────────
    parsed     = urlparse(game_origin)
    path_parts = [p for p in parsed.path.split("/") if p]
    host_s  = slugify(parsed.netloc.split(":")[0])
    page_s  = slugify(path_parts[-1]) if path_parts else "game"
    slug    = f"{host_s}__{page_s}"
    cache_dir = os.path.join(GAMES_DIR, slug)

    safe_makedirs(cache_dir)
    save_cfg(cache_dir, {"url": entry_url, "origin": game_origin})

    print(f"  Cache   →  {os.path.abspath(cache_dir)}")

    # ── Save the entry HTML right now so the first crawl pass has it
    index_local = os.path.join(cache_dir, "index.html")
    if not os.path.isfile(index_local):
        safe_makedirs(cache_dir)
        with open(index_local, "wb") as f:
            f.write(entry_body)
        print(f"  [saved] {index_local}")

    # ── Set up mapper and server ──────────────────────────────────
    mapper = UrlMapper(game_origin, cache_dir)
    mapper.set_entry_url(entry_url)
    port   = pick_port(port)
    server = CachingServer(mapper, game_origin, port=port)
    server.start_background()

    local_url = f"http://{SERVER_HOST}:{port}/"

    # ── Crawl loop ────────────────────────────────────────────────
    print(f"\n  Will crawl up to {MAX_PASSES} passes.")
    print("  Each pass opens the game in a headless browser.")
    print("  Stops when a full pass downloads nothing new.\n")

    for pass_num in range(1, MAX_PASSES + 1):
        server.reset_counter()

        ok = run_crawl_pass(local_url, pass_num, idle_seconds=PASS_IDLE_S,
                            local_host=SERVER_HOST, local_port=port)
        if not ok:
            break

        new = server.get_counter()
        cached_total = sum(len(fs) for _,_,fs in os.walk(cache_dir))
        print(f"  Pass {pass_num}: {new} new file(s) downloaded  "
              f"({cached_total} total in cache)")

        if new == 0:
            print(f"\n  ✓ No new files in pass {pass_num} — archive is complete!")
            break
    else:
        print(f"\n  Reached {MAX_PASSES} passes — stopping crawl.")

    # ── Open for real play ────────────────────────────────────────
    cached_total = sum(len(fs) for _,_,fs in os.walk(cache_dir))
    print(f"\n{'='*62}")
    print(f"  Archive ready  —  {cached_total} files")
    print(f"  Opening  http://{SERVER_HOST}:{port}/  in your browser …")
    print(f"  The server keeps running; new assets are still auto-cached.")
    print(f"  Press Ctrl+C to stop.\n")

    webbrowser.open(local_url)

    # Keep the server alive. Use 0.1s sleep slices so Ctrl+C is caught
    # promptly on Windows (which doesn't interrupt long sleep() calls).
    print("  Press Ctrl+C to stop.")
    try:
        while True:
            time.sleep(0.1)
    except KeyboardInterrupt:
        pass
    finally:
        server.stop()
        total = sum(len(fs) for _,_,fs in os.walk(cache_dir))
        print(f"\nStopped. {total} files cached in {cache_dir}/")


if __name__ == "__main__":
    main()