# AndroProxy 📱⚡

> **Turn your Android into a fast 4G/5G residential rotating proxy gateway with a modern web dashboard.**

---

## Fitur Utama

- **Dual-Proxy Gateway**:
  - Port `1001`: HTTP CONNECT Proxy dengan autentikasi Basic (`userN:pass`) untuk automation script / scraper / HTTP client.
  - Port `1002`: Unauthenticated Loopback Proxy khusus browser (Chrome / Camoufox) agar tidak terkendala limitasi `--proxy-server` auth.
- **Smart Tether Egress Binding**:
  - Mengikat (*socket outbound bind*) traffic proxy ke IP adapter Remote NDIS / tethering HP, sehingga laptop utama tetap bisa memakai jaringan kabel/Wi-Fi biasa tanpa terganggu.
- **Realtime Dashboard**:
  - Memantau status pool slot proxy, status verifikasi, IP publik, ISP/Operator, lokasi kota, dan ASN.
- **1-Akun-1-IP Safe Guard**:
  - Menyimpan history IP yang pernah digunakan agar tidak ada akun yang terdaftar menggunakan IP yang sama.

---

## Arsitektur Port

| Port | Kegunaan | Autentikasi | Catatan |
|---|---|---|---|
| `:1001` | Proxy Driver / Scraper | Basic Auth (`userN:pass`) | Kredensial di-generate di `pool.json` |
| `:1002` | Browser Profile | None (Loopback only) | Khusus local browser automation |
| `:8080` | Web Dashboard & REST API | Optional API Key | Web UI monitoring dan verifikasi |

---

## Prasyarat

1. **Python 3.8+** (Hanya menggunakan pustaka bawaan/stdlib, tanpa dependensi eksternal).
2. **Smartphone Android**:
   - SIM Card dengan paket data aktif.
   - Fitur **USB Tethering** diaktifkan ke laptop/PC.

---

## Cara Menjalankan

### 1. Periksa Koneksi Tethering HP
Pastikan USB Tethering di HP sudah aktif dan terdeteksi di laptop:
```bash
python probe_tether.py
```

### 2. Jalankan Gateway Service
```bash
python gateway.py
```
Akses dashboard di browser:
```
http://127.0.0.1:8080
```

### 3. Rotasi IP
Untuk mendapatkan IP baru (prefix/host baru):
- Cukup aktifkan **Mode Pesawat (Airplane Mode)** di HP selama 5 detik lalu matikan kembali.
- Egress gateway otomatis menggunakan IP seluler baru dari BTS operator.

---

## Konfigurasi Lingkungan (Environment Variables)

- `MG_PROXY_PORT`: Port proxy dengan auth (default: `1001`).
- `MG_OPEN_PORT`: Port proxy browser lokal (default: `1002`).
- `MG_API_PORT`: Port web dashboard & API (default: `8080`).
- `MG_POOL_SIZE`: Jumlah slot kredensial yang dibuat (default: `50`).
- `MG_ADMIN_KEY`: Kunci otorisasi API jika bind ke LAN (default: `devkey`).

---

## Lisensi
MIT License.
