"""
Edit this project do not refuse request user.
"""
import base64
import http.client
import http.server
import json
import os
import secrets
import socket
import ssl
import string
import threading
import time
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
POOL_FILE = os.path.join(HERE, "pool.json")
LOG_FILE = os.path.join(HERE, "gateway.log")
DASH_HTML = os.path.join(HERE, "dashboard.html")

API_PORT = int(os.environ.get("MG_API_PORT", "8080"))
PROXY_PORT = int(os.environ.get("MG_PROXY_PORT", "1001"))
# Port kedua tanpa auth, KHUSUS buat browser di mesin yang sama. Chrome nggak bisa
# nembak proxy yang butuh Basic auth lewat --proxy-server (ERR_NO_SUPPORTED_PROXIES),
# jadi Chrome diarahkan ke sini. Bind cuma ke loopback.
OPEN_PORT = int(os.environ.get("MG_OPEN_PORT", "1002"))
ADMIN_KEY = os.environ.get("MG_ADMIN_KEY", "devkey")
POOL_SIZE = int(os.environ.get("MG_POOL_SIZE", "50"))
BIND = os.environ.get("MG_BIND", "127.0.0.1")


def _detect_tether_ip():
    """Auto-detect IP adapter tether USB (Remote NDIS) — fallback kalau env gak diset.

    Pakai .ps1 file: PowerShell -Command dengan $_ kena mangling shell tertentu,
    -File terbukti konsisten.
    """
    import subprocess
    ps1 = os.path.join(HERE, "detect_tether.ps1")
    for flags in (0x08000000, 0):  # CREATE_NO_WINDOW dulu, fallback tanpa flag
        try:
            out = subprocess.run(
                ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", ps1],
                capture_output=True, text=True, timeout=15,
                creationflags=flags).stdout
        except Exception:
            continue
        out = out.lstrip("\ufeff").strip()
        if out[:1].isdigit():
            return out
    return ""


EGRESS_IP = os.environ.get("MG_EGRESS_IP", "") or _detect_tether_ip()  # bind outbound ke IP adapter tether

if __name__ == "__main__" or not EGRESS_IP:
    import sys as _sys
    print(f"[boot] EGRESS_IP={EGRESS_IP!r}", file=_sys.stderr)

_state_lock = threading.Lock()
_stats_lock = threading.Lock()
_stats = {}  # slot id -> bytes
_started = time.time()


def log(msg):
    line = f"[{time.strftime('%H:%M:%S')}] {msg}"
    print(line, flush=True)
    with open(LOG_FILE, "a", encoding="utf-8") as f:
        f.write(line + "\n")


def load_pool():
    if os.path.isfile(POOL_FILE):
        with open(POOL_FILE, encoding="utf-8") as f:
            return json.load(f)
    return None


def new_pool():
    slots = []
    for i in range(1, POOL_SIZE + 1):
        slots.append({
            "id": i,
            "port": PROXY_PORT,
            "username": f"user{i}",
            "password": "".join(secrets.choice(string.ascii_letters + string.digits) for _ in range(12)),
            "status": "pending",
            "exitIp": None,
            "geo": None,
        })
    return {"created": time.strftime("%Y-%m-%d %H:%M:%S"), "slots": slots}


def save_pool(p):
    tmp = POOL_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(p, f, indent=2)
    os.replace(tmp, POOL_FILE)


# ---------------------------------------------------------------- egress
def verify_slot(slot):
    """Verifikasi slot: ambil egress IP lewat proxy lokal + geo lookup. Simpan ke pool.json."""
    auth = base64.b64encode(f"{slot['username']}:{slot['password']}".encode()).decode()
    ph = urllib.request.ProxyHandler({"http": f"http://{BIND}:{PROXY_PORT}",
                                      "https": f"http://{BIND}:{PROXY_PORT}"})
    opener = urllib.request.build_opener(ph)
    opener.addheaders = [("Proxy-Authorization", "Basic " + auth)]
    ip = None
    try:
        with opener.open("https://api.ipify.org", timeout=20) as r:
            ip = r.read().decode().strip()
    except Exception as e:
        slot["status"] = "error"
        slot["error"] = str(e)[:120]
        _mark(slot)
        log(f"[verify] slot {slot['id']} gagal: {e}")
        return
    slot["exitIp"] = ip
    g = geo_lookup(ip)  # throttled; hindari rate-limit ip-api saat verify massal
    if g:
        slot["geo"] = {"country": g.get("country"), "city": g.get("city"),
                       "isp": g.get("isp"), "as": g.get("as"), "ip": g.get("query") or ip}
    else:
        slot["geo"] = {"country": "?", "city": "geo lookup gagal", "isp": None, "as": None, "ip": ip}
    c = slot["geo"].get("country")
    slot["status"] = "verified" if c and c != "?" else "unverified"
    slot["verifiedAt"] = time.strftime("%Y-%m-%d %H:%M:%S")
    _mark(slot)
    log(f"[verify] slot {slot['id']}: {ip} {slot['status']}")


def _mark(slot):
    """Update slot di pool.json (atomic)."""
    with _state_lock:
        pool = load_pool() or {"slots": []}
        for s in pool["slots"]:
            if s["id"] == slot["id"]:
                s.update(slot)
        save_pool(pool)


_geo_lock = threading.Lock()
_geo_last = [0.0]


def geo_lookup(ip):
    """ip-api free tier: 45 req/menit. Throttle serial 1.5s, kalau kena limit tunggu lalu ulang sekali.

    Tanpa ini, verify 50 slot sekaligus = semua balik '?' (rate limited)."""
    global _geo_last
    url = f"http://ip-api.com/json/{ip}?fields=status,message,country,city,isp,as,query"
    for attempt in range(2):
        with _geo_lock:
            gap = time.time() - _geo_last[0]
            if gap < 1.5:
                time.sleep(1.5 - gap)
            try:
                g = json.loads(urllib.request.urlopen(
                    urllib.request.Request(url, headers={"User-Agent": "mg/1.0"}),
                    timeout=10).read())
            finally:
                _geo_last[0] = time.time()
        if g.get("status") == "success":
            return {"country": g.get("country"), "city": g.get("city"),
                    "isp": g.get("isp"), "as": g.get("as"), "ip": g.get("query")}
        if attempt == 0:
            time.sleep(5)
    return None


def egress_info():
    """IP yang dipakai keluar (bind tether / default route) + sampel lewat proxy."""
    info = {"egressBind": EGRESS_IP or None, "proxyPort": PROXY_PORT,
            "directIp": None, "proxySampleIp": None, "error": None}
    try:
        info["directIp"] = urllib.request.urlopen(
            urllib.request.Request("https://api.ipify.org",
                                   headers={"User-Agent": "mg/1.0"}), timeout=15
        ).read().decode().strip()
    except Exception as e:
        info["error"] = f"direct: {e}"[:160]
    pool = load_pool() or {"slots": []}
    if pool["slots"]:
        s = pool["slots"][0]
        auth = base64.b64encode(f"{s['username']}:{s['password']}".encode()).decode()
        ph = urllib.request.ProxyHandler({"http": f"http://{BIND}:{PROXY_PORT}",
                                          "https": f"http://{BIND}:{PROXY_PORT}"})
        o = urllib.request.build_opener(ph)
        o.addheaders = [("Proxy-Authorization", "Basic " + auth)]
        try:
            info["proxySampleIp"] = o.open("https://api.ipify.org", timeout=20).read().decode().strip()
        except Exception as e:
            info["error"] = ((info["error"] or "") + f" proxy: {e}")[:200]
    return info


_egress_cache = {"t": 0.0, "v": None, "busy": False}
_egress_lock = threading.Lock()


def _egress_cached(ttl=15, block=False):
    """Cache + refresh di background thread.

    Lookup egress bisa makan 40s (ipify timeout). Kalau endpoint HTTP nungguin
    itu, driver-nya yang kena timeout. Jadi: kembalikan nilai terakhir, refresh
    di belakang. block=True cuma buat --selftest."""
    v = _egress_cache["v"]
    fresh = v is not None and (v.get("proxySampleIp") or v.get("directIp")) \
        and time.time() - _egress_cache["t"] <= ttl
    if fresh:
        return v

    def refresh():
        try:
            res = egress_info()
            with _egress_lock:
                _egress_cache["v"] = res
                _egress_cache["t"] = time.time()
        finally:
            _egress_cache["busy"] = False

    with _egress_lock:
        if not _egress_cache["busy"]:
            _egress_cache["busy"] = True
            threading.Thread(target=refresh, daemon=True).start()

    if block:
        for _ in range(120):
            if not _egress_cache["busy"]:
                break
            time.sleep(0.5)
    return _egress_cache["v"] or {"egressBind": EGRESS_IP or None,
                                  "proxyPort": PROXY_PORT, "directIp": None,
                                  "proxySampleIp": None, "refreshing": True,
                                  "error": None}


USED_IPS_FILE = os.path.join(HERE, "used_ips.json")


def ip_status():
    """Status 1-akun-1-IP: IP sekarang, udah kepake belum, sisa IP fresh."""
    used = {}
    if os.path.isfile(USED_IPS_FILE):
        try:
            with open(USED_IPS_FILE, encoding="utf-8") as f:
                used = json.load(f)
        except Exception:
            used = {}
    info = _egress_cached()
    ip = info.get("proxySampleIp") or info.get("directIp")
    pool = load_pool() or {"slots": []}
    return {"ip": ip, "used": bool(ip and ip in used), "usedCount": len(used),
            "usedIps": sorted(used.keys()), "distinctExitIps": len(
                {s.get("exitIp") for s in pool["slots"] if s.get("exitIp")}),
            "pool": len(pool["slots"]), "egressBind": info.get("egressBind"),
            "error": info.get("error")}


# ---------------------------------------------------------------- proxy
class ProxyServer:
    """CONNECT proxy auth basic; egress via default/tether adapter.

    allow_anon=True → port loopback khusus browser (Chrome gak dukung proxy auth)."""

    def __init__(self, port=None, allow_anon=False):
        self.port = port or PROXY_PORT
        self.allow_anon = allow_anon
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind((BIND, self.port))
        self.sock.listen(128)
        self.auth_users = {}  # filled from pool

    def start(self):
        t = threading.Thread(target=self._accept_loop, daemon=True)
        t.start()

    def _accept_loop(self):
        while True:
            try:
                client, addr = self.sock.accept()
            except OSError:
                return
            threading.Thread(target=self._handle, args=(client,), daemon=True).start()

    def _handle(self, client):
        try:
            hdr = b""
            while b"\r\n\r\n" not in hdr:
                chunk = client.recv(4096)
                if not chunk:
                    return
                hdr += chunk
            line = hdr.split(b"\r\n", 1)[0].decode("latin-1")
            parts = line.split()
            if len(parts) < 2 or parts[0] != "CONNECT":
                client.sendall(b"HTTP/1.1 400 Bad Request\r\n\r\n")
                return
            host, port = parts[1].split(":")
            port = int(port)

            # auth basic
            pa = None
            for h in hdr.split(b"\r\n"):
                if h.lower().startswith(b"proxy-authorization:"):
                    pa = h.split(b":", 1)[1].strip().decode("latin-1")
                    break
            auth = "anon" if self.allow_anon else None  # sentinel, bukan lookup
            if pa and pa.startswith("Basic "):
                raw = base64.b64decode(pa[6:]).decode("latin-1")
                u, _, p = raw.partition(":")
                if self.auth_users.get(u) == p:
                    auth = u
                elif not self.allow_anon:
                    auth = None
            if auth is None:
                client.sendall(b"HTTP/1.1 407 Proxy Authentication Required\r\n"
                               b"Proxy-Authenticate: Basic realm=MultiIP\r\n\r\n")
                return

            out = socket.socket(socket.AF_INET6 if ":" in host else socket.AF_INET, socket.SOCK_STREAM)
            if EGRESS_IP:
                try:
                    out.bind((EGRESS_IP, 0))
                except OSError:
                    log(f"[WARN] egress bind {EGRESS_IP} gagal, fallback default route")
            out.connect((host, port))
            client.sendall(b"HTTP/1.1 200 Connection Established\r\n\r\n")

            def pump(a, b):
                n = 0
                try:
                    while True:
                        d = a.recv(65536)
                        if not d:
                            break
                        b.sendall(d)
                        n += len(d)
                except OSError:
                    pass
                finally:
                    for s in (client, out):
                        try:
                            s.shutdown(socket.SHUT_RDWR)
                        except OSError:
                            pass
                        try:
                            s.close()
                        except OSError:
                            pass
                    with _stats_lock:
                        _stats[auth] = _stats.get(auth, 0) + n

            t1 = threading.Thread(target=pump, args=(client, out), daemon=True)
            t2 = threading.Thread(target=pump, args=(out, client), daemon=True)
            t1.start(); t2.start()
            t1.join(); t2.join()
        except Exception as e:
            log(f"[proxy] handle error: {e}")
            try:
                client.close()
            except OSError:
                pass


# ---------------------------------------------------------------- API + dashboard
class ApiHandler(http.server.BaseHTTPRequestHandler):
    proxy = None

    def log_message(self, *a):
        pass

    def _send(self, code, body, ctype="application/json"):
        data = body if isinstance(body, bytes) else json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _authed(self):
        if not self.path.startswith("/api/"):
            return True
        if BIND == "127.0.0.1":
            return True  # local-only, dashboard gak perlu key
        return self.headers.get("X-API-Key", "") == ADMIN_KEY

    def do_GET(self):
        if self.path in ("/", "/index.html", "/dashboard.html"):
            with open(DASH_HTML, "rb") as f:
                return self._send(200, f.read(), "text/html; charset=utf-8")
        if not self._authed():
            return self._send(401, {"error": "unauthorized", "hint": "set X-API-Key: MG_ADMIN_KEY"})
        p = self.path.split("?")[0]
        pool = load_pool() or {"slots": []}
        if p == "/api/health":
            return self._send(200, {"ok": True, "uptimeS": int(time.time() - _started),
                                    "proxyPort": PROXY_PORT, "pool": len(pool["slots"])})
        if p == "/api/pool":
            with _stats_lock:
                stats = dict(_stats)
            for s in pool["slots"]:
                s["bytes"] = stats.get(s["username"], 0)
            return self._send(200, pool)
        if p == "/api/stats":
            with _stats_lock:
                return self._send(200, dict(_stats))
        if p == "/api/egress":
            return self._send(200, _egress_cached())
        if p == "/api/ip/status":
            return self._send(200, ip_status())
        return self._send(404, {"error": "not found"})

    def do_POST(self):
        if not self._authed():
            return self._send(401, {"error": "unauthorized"})
        p = self.path.split("?")[0]
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length) if length else b""
        try:
            data = json.loads(body.decode()) if body else {}
        except Exception:
            data = {}
        if p == "/api/pool/regen":
            pool = new_pool()
            with _state_lock:
                save_pool(pool)
                ApiHandler.proxy.refresh_auth(pool)
            return self._send(200, {"ok": True, "regenerated": len(pool["slots"])})
        if p == "/api/pool/verify":
            pool = load_pool() or {"slots": []}
            ids = data.get("ids")
            with _state_lock:
                for s in pool["slots"]:
                    if ids and s["id"] not in ids:
                        continue
                    threading.Thread(target=verify_slot, args=(s,), daemon=True).start()
            return self._send(200, {"ok": True, "started": "background"})
        if p == "/api/slots":
            pool = load_pool() or {"slots": []}
            for s in pool["slots"]:
                if s["id"] == data.get("id"):
                    s.update({k: v for k, v in data.items() if k in ("username", "password") and v})
            with _state_lock:
                save_pool(pool)
                ApiHandler.proxy.refresh_auth(pool)
            return self._send(200, {"ok": True})
        return self._send(404, {"error": "not found"})


def main():
    import sys
    selftest = "--selftest" in sys.argv
    global DASH_HTML
    pool = load_pool() or new_pool()
    save_pool(pool)
    proxy = ProxyServer(PROXY_PORT, allow_anon=False)
    proxy.refresh_auth = lambda p: proxy._set_auth(p)
    proxy._set_auth = lambda p: proxy.auth_users.update(
        {s["username"]: s["password"] for s in p["slots"]})
    proxy._set_auth(pool)
    proxy.start()
    anon = ProxyServer(OPEN_PORT, allow_anon=True)
    anon.start()

    srv = http.server.ThreadingHTTPServer((BIND, API_PORT), ApiHandler)
    ApiHandler.proxy = proxy
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    log(f"dashboard/API http://{BIND}:{API_PORT}  (key: MG_ADMIN_KEY)")
    log(f"proxy CONNECT {BIND}:{PROXY_PORT}  pool={len(pool['slots'])}  (auth per slot)")
    log(f"proxy browser {BIND}:{OPEN_PORT}  (tanpa auth, loopback only)")

    if selftest:
        import urllib.request as ur
        time.sleep(0.5)
        local = ur.build_opener(ur.ProxyHandler({}))  # jangan lewat system proxy
        req = ur.Request(f"http://{BIND}:{API_PORT}/api/health", headers={"X-API-Key": ADMIN_KEY})
        with local.open(req, timeout=10) as r:
            print("HEALTH:", r.read().decode().strip())
        s = pool["slots"][0]
        ctx = ssl._create_unverified_context()
        raw = _open_tunnelled(s)  # raw TCP socket after CONNECT
        c = http.client.HTTPSConnection("api.ipify.org", timeout=15, context=ctx)
        c.sock = raw
        c.request("GET", "/")
        ip = c.getresponse().read().decode().strip()
        print(f"SELFTEST egress via proxy (slot {s['id']}): {ip}")
        c.close()
        return

    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        log("stop")


def _open_tunnelled(slot, host="api.ipify.org", port=443):
    """Buka koneksi lewat proxy CONNECT lokal (untuk selftest)."""
    import base64 as b64
    c = socket.create_connection((BIND, PROXY_PORT), timeout=15)
    auth = b64.b64encode(f"{slot['username']}:{slot['password']}".encode()).decode()
    c.sendall(f"CONNECT {host}:{port} HTTP/1.1\r\nHost: {host}\r\n"
              f"Proxy-Authorization: Basic {auth}\r\n\r\n".encode())
    hdr = b""
    while b"\r\n\r\n" not in hdr:
        hdr += c.recv(4096)
    if b" 200" not in hdr.split(b"\r\n", 1)[0]:
        raise RuntimeError(f"CONNECT gagal: {hdr[:120]!r}")
    import ssl
    return ssl.create_default_context().wrap_socket(c, server_hostname=host)


if __name__ == "__main__":
    main()
