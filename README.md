<div align="center">

<img src="pictures/Color_Logo_Png.png" alt="Flowincheck" width="120">

# Flowincheck

**Lampu lalu lintas adaptif berbasis YOLOv8 — kamera ESP32-S3 _atau_ CCTV publik online**

Hitung kendaraan dari kamera, tentukan lalu lintas **LANCAR / PADAT / MACET**, lalu atur durasi lampu merah-kuning-hijau secara otomatis. Dilengkapi dashboard web publik dan panel admin.

</div>

---

## Daftar Isi

1. [Fitur](#fitur)
2. [Arsitektur](#arsitektur)
3. [Struktur Proyek](#struktur-proyek)
4. [Instalasi di Ubuntu](#instalasi-di-ubuntu)
5. [Menggunakan CCTV Publik (Mode B)](#menggunakan-cctv-publik-mode-b)
6. [Menggunakan Kamera ESP32-S3 (Mode A)](#menggunakan-kamera-esp32-s3-mode-a)
7. [Lampu Lalu Lintas ESP32-C6](#lampu-lalu-lintas-esp32-c6)
8. [Menjalankan sebagai Service (systemd)](#menjalankan-sebagai-service-systemd)
9. [HTTPS dengan Nginx](#https-dengan-nginx)
10. [MQTT (opsional)](#mqtt-opsional)
11. [Referensi API](#referensi-api)
12. [Pemecahan Masalah](#pemecahan-masalah)
13. [Keamanan & Etika](#keamanan--etika)
14. [Screenshot](#screenshot)

---

## Fitur

- **Dua sumber kamera dalam satu pipeline**
  - **Mode A – `esp32`**: kamera ESP32-S3 mengirim frame JPEG ke server.
  - **Mode B – `cctv`**: server menarik gambar sendiri dari **API/stream CCTV publik** (snapshot JPEG, HLS `.m3u8`, atau MJPEG). Tidak perlu perangkat keras tambahan.
- **Deteksi YOLOv8n** untuk mobil, motor, bus, dan truk.
- **ROI (filter jalur)**: hitung hanya kendaraan di jalur yang relevan, digambar langsung di tab Admin.
- **Hasil deteksi ROI**: kotak tiap kendaraan + `kelas: confidence` + "Vehicles detected: N" di tab Pantauan, tabel deteksi, dan unduhan JPG (render server), PNG, JSON, CSV. Di editor ROI, "Uji deteksi terakhir" menghitung ulang jumlah kendaraan untuk poligon yang sedang digambar sebelum disimpan.
- **Klasifikasi kepadatan** (LANCAR / PADAT / MACET) dan **program lampu otomatis** (durasi merah/kuning/hijau).
- **Dashboard satu file HTML**: ringkasan, peta pantauan, riwayat + ekspor CSV, grafik rata-rata per jam, mode gelap/terang, responsif di HP.
- **Panel Admin** (token): koordinat lokasi, ROI, ambang, program lampu, parameter firmware, perintah jarak jauh.
- **MQTT opsional** untuk update live; tanpa MQTT, semua tetap jalan lewat HTTP.
- Riwayat disimpan di **Supabase** (PostgreSQL).

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
                 │ merah/kuning/hijau│                   │ + panel Admin    │
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
| `dashboard.html` | Dashboard web satu file (tambah `?demo=1` untuk mode contoh) |
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
sudo apt install -y git curl python3 python3-venv python3-pip \
                    ffmpeg libgl1 libglib2.0-0
```

- `ffmpeg` **wajib** untuk CCTV bertipe `hls` dan `mjpeg`.
- `libgl1` dan `libglib2.0-0` dibutuhkan OpenCV (dependensi `ultralytics`) di server tanpa GUI.

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

Dashboard: `http://IP_SERVER:8000/` — panel admin ada di tab **Admin** (masukkan `admin_token`).

> Ingin melihat tampilan tanpa server? Buka `dashboard.html?demo=1` di browser.

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
    lat: -7.9797                 # titik sebenarnya (atau klik peta di tab Admin)
    lng: 112.6304
    calibration_max_count: 25
    density_low: 6               # di bawah ini  -> LANCAR
    density_high: 18             # sama/di atas  -> MACET
    roi_label: "jalur kanan"
    roi:                         # opsional, bisa digambar di tab Admin > ROI
      - [0.90, 0.09]
      - [0.53, 1.00]
      - [0.94, 0.86]
```

Beberapa kamera sekaligus = tambahkan beberapa entri di bawah `locations:`. Seluruh lokasi `mode: cctv` ditarik bergantian setiap `cctv_poll_interval_seconds`.

Nama kunci seperti `cctv_snapshot_url` dipakai juga untuk stream HLS/MJPEG; `cctv_stream_type` yang menentukan cara pengambilannya.

### 4. Kalibrasi jalur (ROI)

Satu frame CCTV sering mencakup beberapa jalur/arah, padahal lampu hanya perlu menghitung jalur yang masuk ke simpang itu.

- Buka `http://IP_SERVER:8000/api/debug/roi-preview?location=Malang-CCTV` untuk melihat frame live dengan poligon ROI berwarna merah, **atau**
- Gambar langsung di dashboard: **Admin → pilih lokasi → ROI**.

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
4. Kamera mengirim JPEG ke `POST /api/ingest/esp32/{device_id}?location=...`. Interval, resolusi, dan kualitas JPEG bisa diubah dari tab **Admin** tanpa flash ulang.

## Lampu Lalu Lintas ESP32-C6

`esp32c6_trafficlight.ino` menjalankan siklus **merah → hijau → kuning** memakai durasi dari server (`GET /api/light/latest`, dan MQTT bila aktif). Program baru baru dipakai di awal siklus berikutnya, sehingga lampu tidak melompat di tengah fase.

- Wiring: **RED → GPIO 4**, **YELLOW → GPIO 5**, **GREEN → GPIO 6**, GND bersama. Modul LED 5V jangan diberi daya dari pin 3V3.
- Isi `WIFI_*`, `SERVER_HOST`, `LOCATION_NAME`, `LOCATION_SLUG` (harus sama dengan `config.yaml`).
- Jika server tidak terjangkau, lampu memakai mode offline: `fallback`, `blink_yellow`, atau `all_red` (diatur di tab Admin).
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

> `WorkingDirectory` wajib benar karena `config.yaml`, `dashboard.html`, dan `runtime_state.json` dibaca relatif terhadap folder itu. Jika mengikat ke `127.0.0.1`, akses dari luar lewat Nginx (bagian berikut). Untuk akses langsung ke port 8000, ganti ke `0.0.0.0`.

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
| `ImportError: libGL.so.1` | `sudo apt install -y libgl1 libglib2.0-0`. |
| Instalasi pip lama / disk penuh | Pasang `torch` versi CPU dulu (langkah 3). |
| `Killed` saat memuat model | RAM kurang; tambah swap: `sudo fallocate -l 2G /swapfile && sudo chmod 600 /swapfile && sudo mkswap /swapfile && sudo swapon /swapfile`. |
| Dashboard kosong, data tidak muncul | Cek tabel `greenflow_logs` sudah dibuat dan `service_key` benar; lihat `journalctl -u flowincheck -f` (`failed to write to Supabase`). |
| Tab Admin: 503 | `admin_token` di `config.yaml` masih kosong. |
| Tab Admin: 401 | Token salah. |
| `/api/light/latest` berisi `note: fallback` | Belum ada frame yang diproses untuk lokasi itu; tunggu satu siklus polling atau cek log CCTV. |
| Nama lokasi tidak cocok | `location` di `config.yaml`, firmware, dan `?location=` harus **sama persis** (huruf besar/kecil dihitung). |

## Keamanan & Etika

- Simpan `config.yaml` di luar repo publik: tambahkan ke `.gitignore` bersama `runtime_state.json` dan `.venv/`.

  ```bash
  printf "config.yaml\nruntime_state.json\n.venv/\n__pycache__/\n*.pt\n" >> .gitignore
  ```
- Selalu pakai HTTPS untuk panel admin, dan token admin yang panjang dan acak.
- Hanya pakai CCTV publik yang memang boleh diakses, patuhi syarat layanan penyedia, dan jangan menyebarkan ulang rekaman.
- Sistem ini hanya **menghitung kendaraan**, tidak mengenali plat nomor atau wajah, dan tidak menyimpan gambar ke database (hanya angka hitungan).

## Screenshot

| Dashboard | Pantauan peta |
|---|---|
| ![Dashboard terang](screenshots/01-dashboard-terang.png) | ![Pantauan peta](screenshots/02-pantauan-peta-terang.png) |

| Editor ROI (Admin) | Mode gelap |
|---|---|
| ![Editor ROI](screenshots/05-admin-editor-roi.png) | ![Dashboard gelap](screenshots/10-dashboard-gelap.png) |

| Riwayat | Mobile |
|---|---|
| ![Riwayat](screenshots/03-riwayat-terang.png) | ![Mobile](screenshots/13-mobile-pantauan.png) |

| Hasil deteksi ROI (Pantauan) | Uji deteksi di editor ROI |
|---|---|
| ![Hasil ROI](screenshots/14-pantauan-hasil-roi.png) | ![Uji deteksi](screenshots/15-admin-roi-uji-deteksi.png) |

| Unduhan JPG dari server | Unduhan PNG dari browser |
|---|---|
| ![JPG server](screenshots/17-contoh-jpg-server-roi.jpg) | ![PNG browser](screenshots/18-contoh-png-browser-roi.png) |

---

<div align="center">

Dibuat oleh **Gathan Ega Fyandhika** · [GitHub](https://github.com/gathanega) · [LinkedIn](https://www.linkedin.com/in/gathanegafyandhika)

</div>
