"""Rotate tether IP: disable/enable adapter, wait for new IP, verify GitHub."""
import json
import socket
import ssl
import subprocess
import sys
import time
import urllib.request

BIND = "127.0.0.1"
PROXY_PORT = 1001
OPEN_PORT = 1002
POOL_FILE = "pool.json"


def run_ps(script):
    r = subprocess.run(
        ["powershell", "-NoProfile", "-Command", script],
        capture_output=True, text=True, errors="replace"
    )
    return r.stdout.strip()


def get_egress_ip():
    script = (
        "$r = Get-NetRoute -DestinationPrefix '0.0.0.0/0' | "
        "Sort-Object RouteMetric | Select-Object -First 1; "
        "if ($r) { $a = Get-NetAdapter -InterfaceIndex $r.InterfaceIndex "
        "-ErrorAction SilentlyContinue; "
        "$i = (Get-NetIPAddress -InterfaceIndex $r.InterfaceIndex "
        "-AddressFamily IPv4 -ErrorAction SilentlyContinue | "
        "Select-Object -First 1).IPAddress; "
        "\"$($r.InterfaceIndex)|$($a.Name)|$i\" }"
    )
    out = run_ps(script)
    parts = out.split("|")
    if len(parts) >= 3:
        return parts[0], parts[1], parts[2]
    return None, None, None


def get_public_ip():
    try:
        return urllib.request.urlopen(
            urllib.request.Request("https://api.ipify.org",
                                   headers={"User-Agent": "mg/1.0"}),
            timeout=15
        ).read().decode().strip()
    except Exception:
        return None


def test_github_via_proxy():
    """Test GitHub /signup via proxy. Returns (ok, status_code)."""
    pool = None
    try:
        with open(POOL_FILE, encoding="utf-8") as f:
            pool = json.load(f)
    except Exception:
        pass
    if not pool or not pool.get("slots"):
        return False, "no pool"
    s = pool["slots"][0]
    import base64
    auth = base64.b64encode(f"{s['username']}:{s['password']}".encode()).decode()
    try:
        c = socket.create_connection((BIND, PROXY_PORT), timeout=10)
        c.sendall(f"CONNECT github.com:443 HTTP/1.1\r\nHost: github.com\r\n"
                   f"Proxy-Authorization: Basic {auth}\r\n\r\n".encode())
        hdr = b""
        while b"\r\n\r\n" not in hdr:
            hdr += c.recv(4096)
        if b" 200" not in hdr.split(b"\r\n", 1)[0]:
            return False, f"proxy fail: {hdr[:80]!r}"
        ctx = ssl.create_default_context()
        ss = ctx.wrap_socket(c, server_hostname="github.com")
        ss.sendall(
            b"GET /signup HTTP/1.1\r\n"
            b"Host: github.com\r\n"
            b"User-Agent: Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            b"AppleWebKit/537.36 (KHTML, like Gecko) "
            b"Chrome/140.0.0.0 Safari/537.36\r\n"
            b"Accept: text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8\r\n"
            b"Accept-Language: en-US,en;q=0.9\r\n"
            b"Connection: close\r\n\r\n"
        )
        resp = b""
        while True:
            d = ss.recv(4096)
            if not d:
                break
            resp += d
        status_line = resp.split(b"\r\n", 1)[0].decode()
        ss.close()
        code = status_line.split(" ")[1] if " " in status_line else "?"
        if code in ("200", "301", "302"):
            return True, code
        return False, code
    except Exception as e:
        return False, str(e)[:80]


def disable_adapter(ifindex):
    run_ps(f'Disable-NetAdapter -InterfaceIndex {ifindex} -Confirm:$false')
    time.sleep(3)


def enable_adapter(ifindex):
    run_ps(f'Enable-NetAdapter -InterfaceIndex {ifindex} -Confirm:$false')
    time.sleep(5)


def wait_for_ip(timeout=60):
    deadline = time.time() + timeout
    while time.time() < deadline:
        idx, name, ip = get_egress_ip()
        if ip and not ip.startswith("169.254"):
            return idx, name, ip
        time.sleep(2)
    return None, None, None


def main():
    print("=" * 50)
    print("  TETHER IP ROTATOR")
    print("=" * 50)

    # Get current egress
    idx, name, old_ip = get_egress_ip()
    print(f"\nCurrent egress: ifIndex={idx} name={name} ip={old_ip}")

    old_pub = get_public_ip()
    print(f"Current public IP: {old_pub}")

    # Test current IP
    print("\n[1/3] Testing current IP against GitHub...")
    ok, code = test_github_via_proxy()
    if ok:
        print(f"  OK: GitHub returns {code} — no rotation needed!")
        return 0
    print(f"  BLOCKED: GitHub returns {code}")

    # Rotate
    print(f"\n[2/3] Rotating IP (disable/enable adapter {idx})...")
    disable_adapter(idx)
    print("  Adapter disabled. Waiting 10s...")
    time.sleep(10)
    enable_adapter(idx)
    print("  Adapter enabled. Waiting for IP...")

    new_idx, new_name, new_ip = wait_for_ip(timeout=60)
    if not new_ip:
        print("  FAIL: no IP after 60s")
        return 1
    print(f"  New egress IP: {new_ip}")

    new_pub = get_public_ip()
    print(f"  New public IP: {new_pub}")

    if new_pub == old_pub:
        print("  WARN: IP unchanged — ISP may assign same IP. Retrying...")
        # Try once more
        disable_adapter(idx)
        time.sleep(15)
        enable_adapter(idx)
        new_idx, new_name, new_ip = wait_for_ip(timeout=60)
        new_pub = get_public_ip()
        print(f"  After 2nd try: {new_pub}")

    # Test new IP
    print(f"\n[3/3] Testing new IP against GitHub...")
    ok, code = test_github_via_proxy()
    if ok:
        print(f"  OK: GitHub returns {code} — IP rotated successfully!")
        print(f"\n  Old IP: {old_pub}")
        print(f"  New IP: {new_pub}")
        return 0
    else:
        print(f"  STILL BLOCKED: GitHub returns {code}")
        print(f"\n  IP {new_pub} also blocked. Try:")
        print("  1. Airplane mode on phone 30s")
        print("  2. Different SIM")
        print("  3. Wait 10 min then retry")
        return 1


if __name__ == "__main__":
    sys.exit(main())
