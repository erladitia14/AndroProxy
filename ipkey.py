#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""ipkey.py — gate 1 akun = 1 IP.

IP yang udah kepake disimpan di used_ips.json. Driver wajib panggil `gate`
sebelum buka browser: kalau IP sekarang udah pernah kepake, gate keluar
exit 1 + bilang ROTATE. Abis akun jadi, panggil `mark`.

Pakai:
  python ipkey.py gate        # exit 0 = IP fresh, 1 = ROTATE dulu
  python ipkey.py mark        # tandai IP sekarang kepake (abis akun jadi)
  python ipkey.py rotate [s]  # coba ganti IP, tunggu maks s detik (default 300)
  python ipkey.py wait [s]    # tunggu IP ganti sendiri (rotasi operator)
  python ipkey.py list
  python ipkey.py reset
  python ipkey.py current     # IP yang dilihat target lewat proxy
"""
import ipaddress
import json
import os
import sys
import time
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
USED_FILE = os.path.join(HERE, "used_ips.json")
GATEWAY = os.environ.get("MG_API", "http://127.0.0.1:8080")


def _local():
    """Opener yang gak lewat system proxy."""
    return urllib.request.build_opener(urllib.request.ProxyHandler({}))


def canon(ip):
    """Normalisasi IPv4/IPv6 biar 2 bentuk IP yang sama gak dihitung 2x."""
    return str(ipaddress.ip_address(ip.strip()))


def load():
    if os.path.isfile(USED_FILE):
        with open(USED_FILE, encoding="utf-8") as f:
            return json.load(f)
    return {}


def save(d):
    tmp = USED_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(d, f, indent=2, sort_keys=True)
    os.replace(tmp, USED_FILE)


def is_used(ip):
    return canon(ip) in load()


def mark_used(ip):
    d = load()
    k = canon(ip)
    if k in d:
        return False
    d[k] = time.strftime("%Y-%m-%d %H:%M:%S")
    save(d)
    return True


def _status():
    with _local().open(GATEWAY + "/api/ip/status", timeout=30) as r:
        return json.loads(r.read())


def current(wait=60):
    """IP yang target lihat: sampel lewat proxy gateway, bukan IP rumah.

    Endpoint /api/ip/status refresh egress di background, jadi call pertama
    bisa balikin null — tunggu sampai keisi. Kalau IP di daftar 'kepake',
    langsung balik (nggak perlu nunggu refresh)."""
    deadline = time.time() + wait
    last = None
    while time.time() < deadline:
        d = _status()
        last = d
        if d.get("ip"):               # IP egress live (sample lewat proxy)
            return d["ip"]
        time.sleep(3)
    if last and last.get("usedIps"):  # refresh belum jadi: pakai catatan terakhir
        return last["usedIps"][-1]
    raise RuntimeError("gateway belum ngasih IP (cek: python probe_tether.py) "
                       + json.dumps(last)[:200])


def _adb():
    import shutil
    exe = shutil.which("adb")
    if exe:
        return exe
    for p in (r"C:\Users\erlan\AppData\Local\Android\Sdk\platform-tools\adb.exe",
              r"C:\platform-tools\adb.exe"):
        if os.path.isfile(p):
            return p
    return None


def _sh(cmd, timeout=25):
    import subprocess
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return r.returncode, (r.stdout + r.stderr).strip()
    except Exception as e:
        return 1, str(e)[:120]


def rotate():
    """Coba ganti IP seluler. Return (ok, cara, pesan).

    Operator seluler Indonesia (WIN/Smartfren) biasanya rotasi IP sendiri tiap
    beberapa menit. Jadi jalur paling andal = tunggu. Kalau HP tersambung ADB,
    mode pesawat bisa dipakai untuk maksa rotasi."""
    adb = _adb()
    if adb:
        rc, out = _sh([adb, "devices"])
        if rc == 0 and len([l for l in out.splitlines()[1:] if l.strip()]) > 0:
            # Airplane 30s (release PDP context) + data toggle — sticky NAT butuh waktu lebih lama
            _sh([adb, "shell", "cmd", "connectivity", "airplane-mode", "enable"])
            time.sleep(30)
            _sh([adb, "shell", "cmd", "connectivity", "airplane-mode", "disable"])
            time.sleep(8)
            _sh([adb, "shell", "svc", "data", "disable"])
            time.sleep(5)
            _sh([adb, "shell", "svc", "data", "enable"])
            return True, "adb", "mode pesawat 30s + data toggle via ADB, tunggu IP baru"
    return False, "manual", ("HP belum tersambung ADB. Rotasi manual: mode pesawat "
                            "ON 15 detik → OFF, atau Data OFF/ON.")


def wait_new(before, timeout=300, poll=10):
    """Tunggu sampai egress IP beda dari `before` (rotasi dari sisi operator)."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        time.sleep(poll)
        try:
            ip = current(wait=45)
        except Exception:
            continue
        if ip != before:
            return ip
    return None


def main():
    cmd = (sys.argv[1] if len(sys.argv) > 1 else "gate").lower()
    flags = sys.argv[2:]

    if cmd == "current":
        print(current())
        return 0

    if cmd == "list":
        d = load()
        print(f"{len(d)} IP kepake:")
        for k, v in sorted(d.items(), key=lambda x: x[1]):
            print(f"  {k}  {v}")
        return 0

    if cmd == "reset":
        save({})
        print("used_ips.json dikosongin")
        return 0

    if cmd == "rotate":
        before = current()
        print(f"IP sekarang: {before}")
        ok, cara, msg = rotate()
        print(f"[{cara}] {msg}")
        to = next((int(x) for x in flags if x.isdigit()), 300)
        print(f"nunggu IP baru (maks {to}s)...")
        ip = wait_new(before, timeout=to)
        if ip:
            print(f"IP BARU: {ip}")
            if is_used(ip):
                print("  (catatan: IP ini udah ada di used_ips.json)")
                return 1
            return 0
        print(f"IP belum ganti dalam {to}s. Coba lagi, atau rotasi manual "
              "(mode pesawat 15s di HP).")
        return 1

    if cmd == "wait":
        before = current()
        print(f"IP sekarang: {before} — nunggu ganti...")
        ip = wait_new(before, timeout=next((int(x) for x in flags if x.isdigit()), 300))
        print(f"IP baru: {ip}" if ip else "IP belum ganti.")
        return 0 if ip else 1

    ip = current()

    if cmd == "gate":
        if is_used(ip):
            print(f"ROTATE — IP {ip} udah kepake (lihat used_ips.json)")
            return 1
        print(f"OK — IP {ip} fresh, silakan lanjut")
        return 0

    if cmd == "mark":
        if mark_used(ip):
            print(f"marked {ip}")
            return 0
        print(f"{ip} udah ada di daftar")
        return 0

    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main())
