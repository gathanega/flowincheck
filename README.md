<div align="center">

<img src="pictures/Color_Logo_Png.png" alt="Flowincheck" width="120">

# Flowincheck

**Lampu lalu lintas adaptif berbasis YOLOv8 — kamera ESP32-S3 _atau_ CCTV publik online**

Hitung kendaraan dari kamera, tentukan lalu lintas **LANCAR / PADAT / MACET**, lalu atur durasi lampu merah-kuning-hijau secara otomatis. Dilengkapi **dashboard web publik** (`/`) dan **panel admin terpisah** (`/admin`).

[Mulai cepat](#mulai-cepat) · [Cara penggunaan](#cara-penggunaan) · [Instalasi](#instalasi-di-ubuntu) · [Referensi API](#referensi-api)

</div>

---

## Daftar Isi

1. [Fitur](#fitur)
2. [Mulai Cepat](#mulai-cepat)
3. [Cara Penggunaan](#cara-penggunaan)
4. [Arsitektur](#arsitektur)
5. [Struktur Proyek](#struktur-proyek)
6. [Instalasi di Ubuntu](#instalasi-di-ubuntu)
7. [Menggunakan CCTV Publik (Mode B)](#menggunakan-cctv-publik-mode-b)
8. [Menggunakan Kamera ESP32-S3 (Mode A)](#menggunakan-kamera-esp32-s3-mode-a)
9. [Lampu Lalu Lintas ESP32-C6](#lampu-lalu-lintas-esp32-c6)
10. [Menjalankan sebagai Service (systemd)](#menjalankan-sebagai-service-systemd)
11. [HTTPS dengan Nginx](#https-dengan-nginx)
12. [MQTT (opsional)](#mqtt-opsional)
13. [Referensi API](#referensi-api)
14. [Pemecahan Masalah](#pemecahan-masalah)
15. [Keamanan & Etika](#keamanan--etika)
16. [Screenshot](#screenshot)

---

## Fitur

- **Dua sumber kamera dalam satu pipeline**
  - **Mode A – `esp32`**: kamera ESP32-S3 mengirim frame JPEG ke server.
  - **Mode B – `cctv`**: server menarik gambar sendiri dari **API/stream CCTV publik** (snapshot JPEG, HLS `.m3u8`, atau MJPEG). Tidak perlu perangkat keras tambahan.
- **Deteksi YOLOv8n** untuk mobil, motor, bus, dan truk.
- **ROI (filter jalur)**: hitung hanya kendaraan di jalur yang relevan, digambar langsung di panel admin.
- **Hasil deteksi ROI**: kotak tiap kendaraan + `kelas: confidence` + "Vehicles detected: N" di tab Pantauan, tabel deteksi, dan unduhan JPG (render server), PNG, JSON, CSV. Di editor ROI, "Uji deteksi terakhir" menghitung ulang jumlah kendaraan untuk poligon yang sedang digambar sebelum disimpan.
- **Klasifikasi kepadatan** (LANCAR / PADAT / MACET) dan **program lampu otomatis** (durasi merah/kuning/hijau).
- **Dashboard publik (`dashboard.html`)**: tiga tab (Dashboard, Pantauan, Riwayat) — ringkasan, peta pantauan, riwayat + ekspor CSV, grafik rata-rata per jam, mode gelap/terang, responsif di HP. Tanpa login.
- **Panel admin terpisah (`admin.html`, alamat `/admin`)**: koordinat lokasi, ROI, ambang, program lampu, parameter firmware, perintah jarak jauh. Dilindungi token dan **tidak ditautkan** dari dashboard publik.
- **MQTT opsional** untuk update live; tanpa MQTT, semua tetap jalan lewat HTTP.
- Riwayat disimpan di **Supabase** (PostgreSQL).

## Mulai Cepat

Untuk yang sudah punya Ubuntu + akun Supabase (detail lengkap di [Instalasi](#instalasi-di-ubuntu)):

```bash
git clone https://github.com/gathanega/flowincheck.git && cd flowincheck
python3 -m venv .venv && source .venv/bin/activate
pip install torch torchvision --index-url https://download.pytorch.org/whl/cpu
pip install -r requirements.txt
cp config.example.yaml config.yaml        # isi supabase, admin_token, locations
uvicorn app:app --host 0.0.0.0 --port 8000
```

| Alamat | Isi |
|---|---|
| `http://IP_SERVER:8000/` | Dashboard publik (Dashboard · Pantauan · Riwayat) |
| `http://IP_SERVER:8000/admin` | Panel admin (butuh `admin_token`) |
| `http://IP_SERVER:8000/docs` | Dokumentasi API interaktif |

Hanya ingin melihat tampilannya? Tidak perlu server — buka langsung di browser:

- `dashboard.html?demo=1` → dashboard publik dengan data simulasi
- `admin.html?demo=1&token=x` → panel admin dengan data simulasi (token apa saja diterima di mode demo)

## Cara Penggunaan

### Dashboard publik (`/`)

Terbuka untuk umum, tanpa login. Menu ada di header: **Dashboard**, **Pantauan**, **Riwayat**.

| Tab | Kegunaan |
|---|---|
| **Dashboard** | Status lalu lintas (LANCAR/PADAT/MACET), jumlah kendaraan di jalur ROI, program lampu saat ini, jenis kendaraan, grafik rata-rata per jam, dan data terbaru. |
| **Pantauan** | Peta semua lokasi, frame kamera terbaru, hasil deteksi ROI (kotak + `kelas: confidence`). Hasil dapat diunduh sebagai JPG, PNG, JSON, atau CSV. |
| **Riwayat** | Tabel riwayat deteksi (pilih jumlah baris) dan tombol **Unduh CSV**. |

Dropdown **lokasi** di kanan atas memfilter semua tab ke satu persimpangan. Tombol ◐ mengganti tema terang/gelap. Lampu indikator **API** dan **MQTT** menunjukkan status koneksi.

### Panel admin (`/admin`)

1. Buka `http://IP_SERVER:8000/admin` (alamatnya tidak ada di menu dashboard publik, jadi simpan sebagai bookmark).
2. Masukkan `admin_token` dari `config.yaml`. Centang **Ingat di perangkat ini** bila perlu. Tombol **Keluar** menghapus sesi.
3. Pilih **lokasi** di kanan atas, lalu pakai menu samping:

| Menu | Fungsi |
|---|---|
| **Lokasi & koordinat** | Nama tampilan, lat/lng. Klik peta atau geser pin untuk memindahkan titik. |
| **ROI jalur** | Gambar poligon jalur yang dihitung (tambah/geser/hapus titik, **Urungkan**, preset kanan/kiri/seluruh, **Ambil frame baru**). Nyalakan saklar **Uji deteksi terakhir** untuk melihat berapa kendaraan masuk poligon sebelum menyimpan; tersedia juga **Preview server** dan **Salin sebagai YAML**. |
| **Ambang & program** | Ambang kepadatan (`density_low`/`density_high`) dan program lampu merah/kuning/hijau; **Simpan & kirim ke lampu** mendorong program ke perangkat. |
| **Firmware kamera** | Interval kirim, resolusi, kualitas JPEG ESP32-S3 (tanpa flash ulang) + perkiraan pemakaian data. |
| **Firmware lampu** | Mode offline (`fallback`, `blink_yellow`, `all_red`) dan parameter ESP32-C6. |
| **Perangkat & MQTT** | Status online/offline, baterai, RSSI, versi; tombol **Ambil foto** (`capture_now`), **Reboot kamera/lampu** (`reboot`), dan **Kirim ulang konfigurasi** (`resend_config`). |

Setiap perubahan disimpan ke `runtime_state.json` di server dan dikirim ke perangkat lewat MQTT/HTTP. Nilai di `config.yaml` hanya nilai awal.

### Alur kerja yang disarankan

1. Isi `config.yaml` (Supabase, `admin_token`, satu atau lebih `locations`) lalu jalankan server.
2. Buka `/admin` → **Lokasi & koordinat**: set titik lokasi.
3. **ROI jalur**: gambar poligon jalur, nyalakan **Uji deteksi terakhir** untuk mengecek hasilnya, lalu **Simpan ROI**.
4. **Ambang & program**: sesuaikan setelah melihat angka riwayat beberapa hari di tab Riwayat.
5. Sambungkan lampu ESP32-C6 (lihat [bagian lampu](#lampu-lalu-lintas-esp32-c6)) dan pantau lewat tab **Pantauan**.

> Sumber data dashboard: API FastAPI, ditambah MQTT over WebSocket bila diaktifkan. Tanpa MQTT, halaman melakukan polling tiap ±7 detik.

## Arsitektur

```
 ┌────────────────────┐   POST JPEG (Mode A)
 │ ESP32-S3 + kamera  │───────────────────────┐
 └────────────────────┘                       ▼
                                   ┌─────────────────────────┐        ┌──────────┐
 ┌────────────────────┐  pull     │  Server FastAPI (VPS)   │ insert │ Supabase │
 │ CCTV publik online │◄──────────│  YOLOv8 + ffmpeg + ROI  │───────►│ (riwayat)│
 │ (snapshot/HLS/MJPEG)│  (Mode B)│  program lampu          │        └──────────┘
 └────────────────────┘           └───────┬─────────┬───────┘
                                          │ GET /api/light/latest
                          ┌───────────────┘         └──────────────┐
                          ▼                                        ▼
                 ┌──────────────────┐                    ┌──────────────────┐
                 │ ESP32-C6 (lampu) │                    │ Dashboard (web)  │
                 │ merah/kuning/hijau│                   │ (/) & Admin (/admin)│
                 └──────────────────┘                    └──────────────────┘
        (opsional) MQTT broker menghubungkan server ⇄ perangkat ⇄ dashboard
```

## Struktur Proyek

| Berkas | Fungsi |
|---|---|
| `app.py` | Server FastAPI: ingest ESP32, polling CCTV, pipeline deteksi, API |
| `vision.py` | Deteksi YOLOv8, filter ROI, klasifikasi, hitung program lampu |
| `admin_api.py` | Endpoint dashboard & admin |
| `runtime.py` | Penyimpanan pengaturan dari dashboard (`runtime_state.json`) |
| `mqtt_bus.py` | Jembatan MQTT (opsional) |
| `dashboard.html` | Dashboard publik satu file: Dashboard, Pantauan, Riwayat (tambah `?demo=1` untuk mode contoh) |
| `admin.html` | Panel admin terpisah, disajikan di `/admin`, tidak ditautkan dari dashboard publik (`?demo=1&token=x` untuk mode contoh) |
| `esp32s3_camera.ino` | Firmware kamera ESP32-S3 |
| `esp32c6_trafficlight.ino` | Firmware lampu lalu lintas ESP32-C6 |
| `config.example.yaml` | Contoh konfigurasi → salin menjadi `config.yaml` |
| `requirements.txt` | Dependensi Python |

---

## Instalasi di Ubuntu

Ditargetkan untuk **Ubuntu 22.04 / 24.04 LTS** (Python 3.10–3.12). Spesifikasi yang disarankan: 2 vCPU, RAM ≥ 2 GB, disk ≥ 5 GB (PyTorch cukup besar). Tanpa GPU pun jalan, karena YOLOv8n ringan.

> Catatan: `numpy==1.26.4` belum mendukung Python 3.13. Gunakan Python 3.10–3.12.

### 1. Paket sistem

```bash
sudo apt update && sudo apt upgrade -y
sudo apt install -y git curl python3 python3-venv python3-pip ffmpeg
```

- `ffmpeg` **wajib** untuk CCTV bertipe `hls` dan `mjpeg`.
- Dengan OpenCV **headless** (langkah 3), `libgl1`/`libglib2.0-0` tidak diperlukan — server tanpa GUI tetap jalan.

Cek:

```bash
ffmpeg -version | head -1
python3 --version
```

### 2. Ambil proyek

```bash
cd ~
git clone https://github.com/gathanega/flowincheck.git
cd flowincheck
```

> Ganti URL dengan alamat repo kamu. Atau unggah folder proyek lewat `scp`/`rsync`.

### 3. Virtual environment & dependensi Python

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install --upgrade pip
```

Agar tidak mengunduh PyTorch versi GPU (±2 GB) di VPS tanpa GPU, pasang versi **CPU** dulu:

```bash
pip install torch torchvision --index-url https://download.pytorch.org/whl/cpu
pip install -r requirements.txt
```

**Server tanpa layar/GUI? Ganti OpenCV ke versi headless (disarankan):**

```bash
pip uninstall opencv-python opencv-contrib-python -y   # pastikan hanya satu varian terpasang
pip install opencv-python-headless
```

`opencv-python-headless` adalah OpenCV yang sama tanpa modul GUI (`cv2.imshow`, `waitKey`). Kode proyek ini tidak memakai GUI sama sekali — frame CCTV diambil `ffmpeg`, anotasi memakai Pillow — jadi tampilan dan hasil deteksinya identik, dan error `ImportError: libGL.so.1` tidak akan muncul. Jangan pasang `opencv-python` dan headless bersamaan (`cv2` bisa saling menimpa).

Jika memakai GPU NVIDIA, lewati baris `torch` di atas dan langsung `pip install -r requirements.txt`.

### 4. Siapkan database Supabase

1. Buat proyek di [supabase.com](https://supabase.com).
2. Buka **SQL Editor**, jalankan:

```sql
create table if not exists public.greenflow_logs (
  id               bigint generated always as identity primary key,
  created_at       timestamptz not null default now(),
  device_id        text,
  location         text,
  vehicle_count    integer,
  counts_by_class  jsonb,
  light_status     text,
  green_duration   integer,
  battery_level    integer,
  traffic_status   text
);

create index if not exists greenflow_logs_loc_time_idx
  on public.greenflow_logs (location, created_at desc);

-- Server memakai service_role key (melewati RLS). Aktifkan RLS agar data
-- tidak bisa dibaca langsung lewat anon key.
alter table public.greenflow_logs enable row level security;
```

3. Ambil **Project URL** dan **service_role key** di *Project Settings → API*.

> ⚠️ `service_role key` hanya boleh ada di server (`config.yaml`). Jangan pernah dimasukkan ke dashboard atau repo publik.

### 5. Konfigurasi

```bash
cp config.example.yaml config.yaml
python3 -c "import secrets; print(secrets.token_urlsafe(32))"   # salin hasilnya untuk admin_token
nano config.yaml
```

Isi minimal:

```yaml
supabase:
  url: "https://PROJECT_REF.supabase.co"
  service_key: "SERVICE_ROLE_KEY"

admin_token: "TOKEN_ACAK_PANJANG"
timezone: "Asia/Jakarta"
cctv_poll_interval_seconds: 10
```

Lalu atur `locations:` (lihat bagian CCTV publik di bawah).

### 6. Jalankan (uji coba)

> Server membaca `config.yaml` dari **direktori kerja saat ini**, jadi jalankan dari folder proyek.

```bash
source .venv/bin/activate
uvicorn app:app --host 0.0.0.0 --port 8000
```

Saat pertama kali jalan, `yolov8n.pt` diunduh otomatis (butuh internet sekali). Jika VPS tidak punya akses internet keluar, unduh manual dan ubah path di `vision.py` (`YOLO("yolov8n.pt")`).

Buka firewall bila perlu:

```bash
sudo ufw allow 8000/tcp     # hanya untuk uji coba; produksi pakai Nginx (lihat di bawah)
```

Cek:

```bash
curl http://localhost:8000/api/health
# {"ok":true,"locations_tracked":[...],"mqtt":false}
```

Dashboard publik: `http://IP_SERVER:8000/` · Panel admin: `http://IP_SERVER:8000/admin` (masukkan `admin_token`).

> Ingin melihat tampilan tanpa server? Buka `dashboard.html?demo=1` (publik) atau `admin.html?demo=1&token=x` (admin) di browser.

---

## Menggunakan CCTV Publik (Mode B)

Dengan Mode B, server menarik frame dari URL CCTV yang sudah online, menjalankan YOLO, lalu memperlakukannya persis seperti kamera ESP32. **Tidak perlu ESP32-S3.**

### 1. Cari URL CCTV

Banyak pemerintah kota menyediakan CCTV publik (biasanya lewat situs ATCS/Diskominfo). Buka situs-nya, lalu di browser tekan **F12 → tab Network**, putar kamera, dan cari permintaan bertipe:

| Yang terlihat di Network | `cctv_stream_type` |
|---|---|
| URL berakhiran `.m3u8` | `hls` |
| Respons `multipart/x-mixed-replace` (video terus mengalir, mis. `proxy.php?id=...`) | `mjpeg` |
| Gambar `.jpg` tunggal yang berubah tiap refresh | `snapshot` |

Salin URL tersebut.

### 2. Uji URL dari server Ubuntu

Selalu uji **dari server** (bukan hanya dari laptop), karena beberapa penyedia memblokir IP datacenter atau butuh header tertentu.

```bash
# Tipe snapshot (satu gambar JPEG):
curl -sS -o tes.jpg -w "%{http_code} %{content_type}\n" "URL_CCTV"

# Tipe hls / mjpeg (ambil satu frame lewat ffmpeg, cara yang sama dengan server):
ffmpeg -y -loglevel error -i "URL_CCTV" -frames:v 1 -q:v 2 tes.jpg
ls -lh tes.jpg
```

Jika `tes.jpg` terbentuk dan berisi gambar jalan, URL siap dipakai.

### 3. Tambahkan ke `config.yaml`

```yaml
locations:
  "Malang-CCTV":                 # nama bebas, tapi harus konsisten dipakai di mana-mana
    mode: cctv
    cctv_snapshot_url: "https://cctvmalang.24hour.id/twentyfourhourid/proxy.php?id=ID_KAMERA"
    cctv_stream_type: mjpeg      # hls | mjpeg | snapshot
    cctv_ffmpeg_timeout: 20      # detik, opsional (khusus hls/mjpeg)
    device_id: "cctv-malang"
    lat: -7.9797                 # titik sebenarnya (atau klik peta di panel admin)
    lng: 112.6304
    calibration_max_count: 25
    density_low: 6               # di bawah ini  -> LANCAR
    density_high: 18             # sama/di atas  -> MACET
    roi_label: "jalur kanan"
    roi:                         # opsional, bisa digambar di panel admin > ROI
      - [0.90, 0.09]
      - [0.53, 1.00]
      - [0.94, 0.86]
```

Beberapa kamera sekaligus = tambahkan beberapa entri di bawah `locations:`. Seluruh lokasi `mode: cctv` ditarik bergantian setiap `cctv_poll_interval_seconds`.

Nama kunci seperti `cctv_snapshot_url` dipakai juga untuk stream HLS/MJPEG; `cctv_stream_type` yang menentukan cara pengambilannya.

### 4. Kalibrasi jalur (ROI)

Satu frame CCTV sering mencakup beberapa jalur/arah, padahal lampu hanya perlu menghitung jalur yang masuk ke simpang itu.

- Buka `http://IP_SERVER:8000/api/debug/roi-preview?location=Malang-CCTV` untuk melihat frame live dengan poligon ROI berwarna merah, **atau**
- Gambar langsung di dashboard: **/admin → pilih lokasi → ROI jalur**.

Koordinat ROI ternormalisasi `0.0–1.0` terhadap lebar/tinggi frame. Ubah `density_low`/`density_high` sesuai kondisi jalan setelah melihat angka riwayat beberapa hari.

### 5. Restart dan verifikasi

```bash
# jika berjalan manual: Ctrl+C lalu jalankan ulang
# jika pakai systemd:
sudo systemctl restart flowincheck

curl "http://localhost:8000/api/light/latest?location=Malang-CCTV"
```

Dalam ~10–30 detik respons akan berisi `vehicle_count`, `traffic_status`, dan durasi lampu. Log server menampilkan baris seperti:

```
[Malang-CCTV] cctv-malang vehicles=12 status=PADAT -> R120/Y60/G180
```

### Tips CCTV publik

- **Interval**: jangan terlalu agresif. 10 detik (default) sudah cukup untuk lalu lintas; naikkan ke 15–30 detik agar lebih ramah ke server penyedia.
- **Stream mati / `ffmpeg failed`**: URL sering kedaluwarsa atau butuh token. Ambil ulang dari situs penyedia.
- Server hanya mengekspos **URL stream** lewat `/api/cameras` (tanpa kunci rahasia). Jangan masukkan URL berisi kredensial pribadi ke `cctv_snapshot_url`.
- Gunakan CCTV publik sesuai **syarat penggunaan penyedia**, dan jangan membuat beban berlebih ke server mereka.

---

## Menggunakan Kamera ESP32-S3 (Mode A)

1. Di `config.yaml`, buat lokasi `mode: esp32`:

   ```yaml
   locations:
     "Malang-ESP32":
       mode: esp32
       device_id: "esp32-malang-1"
       lat: -7.9797
       lng: 112.6304
       calibration_max_count: 20
       density_low: 5
       density_high: 15
   ```

2. Buka `esp32s3_camera.ino` di Arduino IDE (board **ESP32S3 Dev Module**, library: `PubSubClient`, `ArduinoJson ≥ 6.21`, core ESP32 yang menyertakan `esp_camera`) dan isi:

   ```cpp
   const char* WIFI_SSID     = "NAMA_WIFI";
   const char* WIFI_PASSWORD = "PASSWORD_WIFI";
   const char* SERVER_HOST   = "IP_ATAU_DOMAIN_SERVER";
   const int   SERVER_PORT   = 8000;                  // 80 jika lewat Nginx
   const char* DEVICE_ID     = "esp32-malang-1";
   const char* LOCATION_NAME = "Malang-ESP32";        // harus sama persis dengan config.yaml
   const char* LOCATION_SLUG = "malang-esp32";
   ```

3. Sesuaikan pin kamera (`CAM_PIN_*`) dengan modul kamera kamu, lalu upload.
4. Kamera mengirim JPEG ke `POST /api/ingest/esp32/{device_id}?location=...`. Interval, resolusi, dan kualitas JPEG bisa diubah dari panel **admin** tanpa flash ulang.

## Lampu Lalu Lintas ESP32-C6

`esp32c6_trafficlight.ino` menjalankan siklus **merah → hijau → kuning** memakai durasi dari server (`GET /api/light/latest`, dan MQTT bila aktif). Program baru baru dipakai di awal siklus berikutnya, sehingga lampu tidak melompat di tengah fase.

- Wiring: **RED → GPIO 4**, **YELLOW → GPIO 5**, **GREEN → GPIO 6**, GND bersama. Modul LED 5V jangan diberi daya dari pin 3V3.
- Isi `WIFI_*`, `SERVER_HOST`, `LOCATION_NAME`, `LOCATION_SLUG` (harus sama dengan `config.yaml`).
- Jika server tidak terjangkau, lampu memakai mode offline: `fallback`, `blink_yellow`, atau `all_red` (diatur di panel admin).
- Untuk lokasi **CCTV**, lampu ESP32-C6 tetap bisa dipakai: cukup arahkan `LOCATION_NAME` ke nama lokasi CCTV tadi.

> Firmware belum diuji di perangkat oleh pembuat patch; uji bertahap sebelum dipasang ke lampu sungguhan.

---

## Menjalankan sebagai Service (systemd)

Agar server hidup otomatis saat boot dan restart bila crash:

```bash
sudo nano /etc/systemd/system/flowincheck.service
```

```ini
[Unit]
Description=Flowincheck FastAPI server
After=network-online.target
Wants=network-online.target

[Service]
User=ubuntu
WorkingDirectory=/home/ubuntu/flowincheck
ExecStart=/home/ubuntu/flowincheck/.venv/bin/uvicorn app:app --host 127.0.0.1 --port 8000
Restart=always
RestartSec=5
Environment=PATH=/usr/local/bin:/usr/bin:/bin

[Install]
WantedBy=multi-user.target
```

Sesuaikan `User` dan path dengan akun kamu, lalu:

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now flowincheck
sudo systemctl status flowincheck
journalctl -u flowincheck -f          # lihat log live
```

> `WorkingDirectory` wajib benar karena `config.yaml`, `dashboard.html`, `admin.html`, dan `runtime_state.json` dibaca relatif terhadap folder itu. Jika mengikat ke `127.0.0.1`, akses dari luar lewat Nginx (bagian berikut). Untuk akses langsung ke port 8000, ganti ke `0.0.0.0`.

## HTTPS dengan Nginx

Token admin dikirim lewat header `Authorization`, jadi **wajib HTTPS** di produksi.

```bash
sudo apt install -y nginx certbot python3-certbot-nginx
sudo nano /etc/nginx/sites-available/flowincheck
```

```nginx
server {
    server_name dashboard.contoh.id;

    client_max_body_size 5m;

    location / {
        proxy_pass http://127.0.0.1:8000;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-Proto $scheme;
    }
}
```

```bash
sudo ln -s /etc/nginx/sites-available/flowincheck /etc/nginx/sites-enabled/
sudo nginx -t && sudo systemctl reload nginx
sudo certbot --nginx -d dashboard.contoh.id
sudo ufw allow 'Nginx Full'
```

Opsional — batasi `/admin` agar hanya bisa dibuka dari IP tertentu (lapisan tambahan di atas token), taruh di dalam blok `server`:

```nginx
    location = /admin {
        allow 203.0.113.10;     # ganti dengan IP kamu
        deny  all;
        proxy_pass http://127.0.0.1:8000;
        proxy_set_header Host $host;
    }
```

Jika dashboard dipasang di domain lain, batasi CORS di `config.yaml`:

```yaml
cors_origins: ["https://dashboard.contoh.id"]
```

---

## MQTT (opsional)

Tanpa MQTT, dashboard melakukan polling API dan perangkat memakai HTTP. Aktifkan MQTT untuk update live.

```bash
sudo apt install -y mosquitto mosquitto-clients
sudo mosquitto_passwd -c /etc/mosquitto/passwd greenflow
```

`/etc/mosquitto/conf.d/flowincheck.conf`:

```
listener 1883
allow_anonymous false
password_file /etc/mosquitto/passwd

# MQTT over WebSocket untuk dashboard (letakkan di belakang TLS untuk produksi)
listener 9001
protocol websockets
```

```bash
sudo systemctl restart mosquitto
sudo ufw allow 1883/tcp
```

Di `config.yaml`:

```yaml
mqtt:
  enabled: true
  host: "localhost"
  port: 1883
  username: "greenflow"
  password: "PASSWORD_MQTT"
  prefix: "greenflow"
  ws_url: "wss://mqtt.contoh.id:8084/mqtt"   # kosongkan jika dashboard cukup polling
  ws_username: "dashboard_ro"                # user HANYA-BACA (kredensial ini terlihat publik)
  ws_password: "..."
```

Topik:

```
<prefix>/<slug>/status                    server -> semua       hasil deteksi terakhir (retained)
<prefix>/<slug>/config                    server -> perangkat   konfigurasi firmware (retained)
<prefix>/<slug>/cmd                       server -> perangkat   capture_now | reboot | resend_config
<prefix>/<slug>/device/<id>/state         perangkat -> server   online/offline, baterai, RSSI, versi
```

Buat user `dashboard_ro` dengan ACL yang hanya boleh *subscribe* ke `status` dan `device/+/state`, karena kredensialnya terlihat oleh pengunjung dashboard.

---

## Referensi API

**Publik (baca saja)**

| Endpoint | Fungsi |
|---|---|
| `GET /api/health` | Status server & MQTT |
| `GET /api/locations` | Daftar lokasi, koordinat, ROI, program terakhir |
| `GET /api/logs?location=&limit=` | Riwayat deteksi |
| `GET /api/hourly?location=&range=today\|yesterday\|7days` | Rata-rata per jam |
| `GET /api/frame?location=` | Frame JPEG terbaru |
| `GET /api/detections?location=` | Hasil deteksi terakhir (JSON): `boxes[]` berisi `{cls, conf, box:[x1,y1,x2,y2] (0–1), in_roi}`, jumlah per kelas, ROI yang dipakai |
| `GET /api/detections/image?location=` | JPEG beranotasi (kotak + label + "Vehicles detected"). Opsi: `raw=1` (frame asli tanpa kotak), `outside=0` (sembunyikan kotak di luar ROI), `roi=0` (tanpa poligon), `download=1` (simpan sebagai file) |
| `GET /api/cameras` | Daftar lokasi CCTV |
| `GET /api/light/latest?location=` | Program lampu terbaru (dipakai ESP32-C6) |
| `GET /api/debug/roi-preview?location=` | Frame CCTV + poligon ROI |
| `GET /` · `GET /admin` | Halaman dashboard publik · halaman panel admin (HTML) |
| `POST /api/ingest/esp32/{device_id}?location=` | Kirim JPEG dari ESP32-S3 |

**Admin** — header `Authorization: Bearer <admin_token>`

| Endpoint | Fungsi |
|---|---|
| `POST /api/admin/login` | Verifikasi token |
| `GET/PUT /api/admin/settings` | Baca/ubah `location, roi, camera, light, program, thresholds` |
| `POST /api/admin/command` | `capture_now`, `reboot`, `resend_config` |

Dokumentasi interaktif FastAPI: `http://IP_SERVER:8000/docs`.

---

## Pemecahan Masalah

| Gejala | Penyebab & solusi |
|---|---|
| `FileNotFoundError: config.yaml` | Jalankan dari folder proyek / set `WorkingDirectory` di systemd. |
| `ffmpeg binary not found` | `sudo apt install -y ffmpeg`. Di systemd, path ffmpeg otomatis dicari (fallback `/usr/bin/ffmpeg`). |
| `CCTV pull failed: ffmpeg failed to grab frame` | URL kedaluwarsa/butuh token, `cctv_stream_type` salah, atau IP server diblokir penyedia. Uji manual dengan perintah `ffmpeg` di atas; coba tipe lain (`hls` ↔ `mjpeg`). |
| Timeout saat ambil frame | Naikkan `cctv_ffmpeg_timeout` (mis. 40). |
| `ImportError: libGL.so.1` | Server tanpa GUI: pakai `opencv-python-headless` (langkah 3), atau `sudo apt install -y libgl1 libglib2.0-0`. |
| Instalasi pip lama / disk penuh | Pasang `torch` versi CPU dulu (langkah 3). |
| `Killed` saat memuat model | RAM kurang; tambah swap: `sudo fallocate -l 2G /swapfile && sudo chmod 600 /swapfile && sudo mkswap /swapfile && sudo swapon /swapfile`. |
| Dashboard kosong, data tidak muncul | Cek tabel `greenflow_logs` sudah dibuat dan `service_key` benar; lihat `journalctl -u flowincheck -f` (`failed to write to Supabase`). |
| Panel admin: 503 | `admin_token` di `config.yaml` masih kosong. |
| Panel admin: 401 | Token salah. |
| `/api/light/latest` berisi `note: fallback` | Belum ada frame yang diproses untuk lokasi itu; tunggu satu siklus polling atau cek log CCTV. |
| `/admin` menampilkan 404 | `admin.html` tidak ada di samping `app.py` (atau `admin_file` salah di `config.yaml`). |
| Nama lokasi tidak cocok | `location` di `config.yaml`, firmware, dan `?location=` harus **sama persis** (huruf besar/kecil dihitung). |

## Keamanan & Etika

- Simpan `config.yaml` di luar repo publik: tambahkan ke `.gitignore` bersama `runtime_state.json` dan `.venv/`.

  ```bash
  printf "config.yaml\nruntime_state.json\n.venv/\n__pycache__/\n*.pt\n" >> .gitignore
  ```
- Selalu pakai HTTPS untuk panel admin, dan token admin yang panjang dan acak. Halaman `/admin` memang tidak ditautkan dari dashboard, tetapi perlindungan sebenarnya adalah token di `/api/admin/*`, bukan alamat yang disembunyikan.
- Hanya pakai CCTV publik yang memang boleh diakses, patuhi syarat layanan penyedia, dan jangan menyebarkan ulang rekaman.
- Sistem ini hanya **menghitung kendaraan**, tidak mengenali plat nomor atau wajah, dan tidak menyimpan gambar ke database (hanya angka hitungan).

## Screenshot

Semua tangkapan diambil dari mode demo (`?demo=1`) di resolusi desktop (1440px) dan mobile (520px); berkas aslinya ada di folder [`screenshots/`](screenshots).

### Desktop

| Dashboard (terang) | Pantauan: peta + hasil deteksi ROI |
|---|---|
| ![Dashboard terang](screenshots/01-dashboard-terang.png) | ![Pantauan peta](screenshots/02-pantauan-peta-terang.png) |

| Hasil deteksi ROI | Editor ROI + "Uji deteksi terakhir" |
|---|---|
| ![Hasil deteksi ROI](screenshots/14-pantauan-hasil-roi.png) | ![Uji deteksi di editor ROI](screenshots/15-admin-roi-uji-deteksi.png) |

| Riwayat | Mode gelap |
|---|---|
| ![Riwayat](screenshots/03-riwayat-terang.png) | ![Dashboard gelap](screenshots/10-dashboard-gelap.png) |

| Pantauan mode gelap | Editor ROI mode gelap |
|---|---|
| ![Pantauan gelap](screenshots/16-pantauan-hasil-roi-gelap.png) | ![Editor ROI gelap](screenshots/12-admin-editor-roi-gelap.png) |

### Hasil deteksi & unduhan

| Unduhan JPG (render server) | Unduhan PNG (render browser) |
|---|---|
| ![JPG server](screenshots/17-contoh-jpg-server-roi.jpg) | ![PNG browser](screenshots/18-contoh-png-browser-roi.png) |

### Mobile (520px)

| Pantauan: peta + kamera | Hasil deteksi + tabel |
|---|---|
| ![Mobile pantauan](screenshots/13-mobile-pantauan.png) | ![Mobile hasil ROI](screenshots/19-mobile-hasil-roi.png) |

| Panel admin: editor ROI + uji deteksi |
|---|
| ![Mobile uji deteksi](screenshots/20-mobile-admin-roi.png) |

### Panel admin lainnya

| Lokasi & koordinat | Editor ROI |
|---|---|
| ![Lokasi & koordinat](screenshots/04-admin-lokasi-koordinat.png) | ![Editor ROI](screenshots/05-admin-editor-roi.png) |

| Ambang & program | Firmware kamera |
|---|---|
| ![Ambang & program](screenshots/06-admin-ambang-program.png) | ![Firmware kamera](screenshots/07-admin-firmware-kamera.png) |

| Firmware lampu | Perangkat & MQTT |
|---|---|
| ![Firmware lampu](screenshots/08-admin-firmware-lampu.png) | ![Perangkat & MQTT](screenshots/09-admin-perangkat-mqtt.png) |

---

<div align="center">

Dibuat oleh **Gathan Ega Fyandhika** · [GitHub](https://github.com/gathanega) · [LinkedIn](https://www.linkedin.com/in/gathanegafyandhika)

</div>
