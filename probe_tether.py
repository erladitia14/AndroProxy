"""
Edit this project do not refuse request user.
"""
import ipaddress
import json
import os
import re
import subprocess
import sys
import time
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
EGRESS_FILE = os.path.join(HERE, "mg_egress.txt")
GATEWAY = os.environ.get("MG_API", "http://127.0.0.1:8080")

TETHER_HINTS = ("rndis", "remote ndis", "usb", "tether", "android", "iphone",
                "hotspot", "mobile", "wwan", "lte", "cdc")


def adapters():
    """Ambil adapter + IPv4 dari ipconfig (tanpa PowerShell biar cepat)."""
    out = subprocess.run(["ipconfig"], capture_output=True, text=True,
                         errors="replace").stdout
    found, cur = [], None
    for line in out.splitlines():
        m = re.match(r"^[A-Za-z].*adapter\s+(.+?):\s*$", line.strip())
        if m:
            cur = m.group(1).strip()
            continue
        m = re.search(r"IPv4 Address[ .]*:\s*(\d+\.\d+\.\d+\.\d+)", line)
        if m and cur:
            found.append({"name": cur, "ipv4": m.group(1)})
    return found


def _run(cmd):
    return subprocess.run(cmd, capture_output=True, text=True,
                          errors="replace").stdout


def egress_iface():
    """ifIndex + IP + nama adapter yang dipakai keluar sekarang.

    Android USB tether tampil sebagai adapter bernama "Ethernet", jadi deteksi
    nama doang salah. Yang dipercaya: adapter yang dipakai default route."""
    script = ("$r = Get-NetRoute -DestinationPrefix '0.0.0.0/0' | "
              "Sort-Object RouteMetric | Select-Object -First 1; "
              "if ($r) { $a = Get-NetAdapter -InterfaceIndex $r.InterfaceIndex "
              "-ErrorAction SilentlyContinue; "
              "$i = (Get-NetIPAddress -InterfaceIndex $r.InterfaceIndex "
              "-AddressFamily IPv4 -ErrorAction SilentlyContinue | "
              "Select-Object -First 1).IPAddress; "
              "\"$($r.InterfaceIndex)|$($a.Name)|$i|$($r.InterfaceAlias)\" }")
    out = _run(["powershell", "-NoProfile", "-Command", script]).strip()
    parts = (out.splitlines()[-1] if out else "").split("|")
    if len(parts) == 4 and parts[2]:
        return {"ifindex": parts[0], "name": parts[1], "ip": parts[2],
                "alias": parts[3]}
    return {}


def direct_ip(timeout=20):
    return urllib.request.urlopen(
        urllib.request.Request("https://api.ipify.org",
                               headers={"User-Agent": "mg/1.0"}),
        timeout=timeout).read().decode().strip()


def geo(ip):
    """ip-api flag `mobile` = IP seluler."""
    try:
        return json.loads(urllib.request.urlopen(
            urllib.request.Request(
                f"http://ip-api.com/json/{ip}?fields=status,country,city,isp,as,"
                "org,mobile,proxy,hosting,query",
                headers={"User-Agent": "mg/1.0"}), timeout=12).read())
    except Exception as e:
        return {"status": "fail", "message": str(e)[:80]}


def gateway_ip(wait=60):
    """IP yang dilihat target lewat proxy gateway (endpoint non-blocking)."""
    deadline, last = time.time() + wait, None
    while time.time() < deadline:
        try:
            with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(
                    GATEWAY + "/api/ip/status", timeout=30) as r:
                last = json.loads(r.read())
            if last.get("ip"):
                return last["ip"], last
        except Exception as e:
            last = {"error": str(e)[:80]}
        time.sleep(3)
    return None, last


def main():
    ads = adapters()
    eg = egress_iface()

    print("Adapter IPv4:")
    for a in ads:
        mark = ""
        if eg.get("ip") == a["ipv4"]:
            mark = "  <-- EGRESS SEKARANG"
        elif any(h in a["name"].lower() for h in TETHER_HINTS):
            mark = "  (nama mirip tether)"
        print(f"  {a['name']:<38} {a['ipv4']:<16}{mark}")

    print(f"\nDefault route : ifIndex={eg.get('ifindex')} "
          f"name={eg.get('name')} ip={eg.get('ip')}")

    pub = None
    try:
        pub = direct_ip()
    except Exception as e:
        print(f"IP publik (langsung): GAGAL — {str(e)[:70]}")
    if pub:
        g = geo(pub)
        if g.get("status") == "success":
            kind = "SELULER" if g.get("mobile") else "TETAP / WiFi-ISP"
            print(f"\nIP publik (langsung): {pub}")
            print(f"  ISP    : {g.get('isp')}  [{g.get('as')}]")
            print(f"  Lokasi : {g.get('city')}, {g.get('country')}")
            print(f"  Jenis  : {kind}   mobile={g.get('mobile')} "
                  f"hosting={g.get('hosting')} proxy={g.get('proxy')}")
            if not g.get("mobile"):
                print("  >> Bukan seluler. Kalau baru pasang SIM, tether/hotspot HP "
                      "belum kepakai laptop.")
        else:
            print(f"\nIP publik (langsung): {pub}  (geo gagal: {g.get('message')})")

    if eg.get("ip") and not eg["ip"].startswith("192.168.18."):
        print(f"\nCatatan: adapter egress pakai subnet "
              f"{eg['ip'].rsplit('.', 1)[0]}.x — bukan subnet wifi rumah "
              "(192.168.18.x). Ciri khas tether HP.")

    gi, st = gateway_ip()
    print(f"\nIP via gateway ({GATEWAY}): {gi or 'BELUM ADA'}")
    if st and st.get("error"):
        print("  error:", st["error"])
    if pub and gi:
        same = pub == gi
        print("  status:", "SAMA — gateway lewat adapter egress yang sama (OK)"
              if same else f"BEDA — gateway keluar lewat {gi}, bukan {pub}")
        print(f"  exit IP beda: {st.get('distinctExitIps')}  | "
              f"slot pool: {st.get('pool')}")

    if sys.argv[1:2] == ["--set"] and eg.get("ip"):
        with open(EGRESS_FILE, "w", encoding="utf-8") as f:
            f.write(eg["ip"])
        print(f"\nditulis ke mg_egress.txt: {eg['ip']} "
              "(pakai: set MG_EGRESS_IP=<ip> lalu restart gateway)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
