# 🚀 Agency Report — Unified Weekly Transaction Pipeline

Pipeline otomatis untuk mengunduh dan mengolah laporan transaksi mingguan dari platform **GrabFood** dan **ShopeeFood**, dijalankan melalui satu CLI terpadu.

---

## 📁 Struktur Proyek

```
agency/
├── cli.py                  # Entry point utama (unified CLI)
├── config.json             # Konfigurasi global (headless, concurrency)
├── pyproject.toml          # Definisi dependensi (dikelola uv)
├── start.sh                # Script setup & run (Linux/macOS)
├── start.bat               # Script setup & run (Windows)
├── grab/
│   ├── grab_api_scraper.py # Logika scraping API Grab (Playwright)
│   ├── main.py             # Pipeline multi-portal Grab
│   └── result.py           # Kalkulasi & format hasil Grab
├── shopee/
│   ├── run_weekly.py       # Pipeline multi-merchant Shopee
│   └── merge_analyzed.py   # Utilitas merge laporan Shopee
└── core/
    ├── browser.py          # Manajemen sesi browser (Selenium/undetected-chromedriver)
    ├── client.py           # ShopeeClient HTTP (API calls)
    ├── logger.py           # Logger terpusat
    └── otp.py              # Penanganan OTP Shopee
```

---

## ⚙️ Prasyarat

| Kebutuhan | Versi Minimum |
|---|---|
| Python | ≥ 3.12 |
| [uv](https://docs.astral.sh/uv/) | terbaru |
| Google Chrome | terbaru |

---

## 🔧 Instalasi

### Linux / macOS

```bash
# Clone repositori
git clone https://github.com/superfoodtechid/agency-scrapper.git
cd agency-scrapper

# Setup otomatis (install uv, sync dependensi, install Playwright Chromium)
bash start.sh
```

### Windows

```bat
start.bat
```

### Manual (tanpa start.sh)

```bash
# Install uv jika belum ada
pip install uv

# Sync dependensi virtual environment
uv sync

# Install browser Chromium untuk Playwright (digunakan Grab)
uv run python -m playwright install chromium

# Jalankan CLI
uv run python cli.py
```

---

## 🗂️ Konfigurasi

### `config.json`

```json
{
  "headless_grab": false,
  "headless_shopee": false,
  "max_concurrency": 3
}
```

| Key | Tipe | Default | Keterangan |
|---|---|---|---|
| `headless_grab` | bool | `false` | Jalankan browser Grab tanpa GUI |
| `headless_shopee` | bool | `false` | Jalankan browser Shopee tanpa GUI |
| `max_concurrency` | int | `3` | Jumlah akun Grab yang diproses secara paralel |

### `.env` (opsional)

File `.env` di root proyek dapat digunakan untuk menyimpan kredensial Shopee:

```env
SHOPEE_USERNAME=username_anda
SHOPEE_PASSWORD=password_anda
SHOPEE_PHONE=08xxxxxxxxxx
```

> **Catatan:** Jika tidak ada `.env`, pipeline Shopee akan otomatis mengambil kredensial dari Google Sheets master.

---

## 🚀 Cara Penggunaan

### Mode Interaktif (Direkomendasikan)

Jalankan tanpa argumen untuk masuk ke wizard interaktif:

```bash
uv run python cli.py
```

Wizard akan memandu Anda memilih:
1. **Platform** — Grab, Shopee, atau keduanya
2. **Cakupan Outlet** — semua outlet atau filter spesifik
3. **Rentang Tanggal** — minggu lalu (otomatis) atau input manual

### Mode CLI (Non-Interaktif)

```bash
# Semua outlet, semua platform, minggu ini
uv run python cli.py all --start 2026-06-02 --end 2026-06-08

# Hanya Grab, outlet tertentu
uv run python cli.py grab --start 2026-06-02 --end 2026-06-08 --outlet "Nama Outlet" --branch "Nama Cabang"

# Hanya Shopee, merchant tertentu
uv run python cli.py shopee --start 2026-06-02 --end 2026-06-08

# Filter berdasarkan username akun Grab
uv run python cli.py grab --start 2026-06-02 --end 2026-06-08 --user "username@email.com"
```

### Argumen CLI

| Argumen | Keterangan |
|---|---|
| `platform` | `grab`, `shopee`, atau `all` |
| `--start` | Tanggal mulai (format: `YYYY-MM-DD` atau `DD-MM-YYYY`) |
| `--end` | Tanggal akhir (format: `YYYY-MM-DD` atau `DD-MM-YYYY`) |
| `--outlet` | Filter nama outlet (Grab) |
| `--branch` | Filter nama cabang (Grab) |
| `--user` | Filter username akun spesifik (Grab) |

---

## 📊 Output Laporan

Semua laporan disimpan di direktori `laporan/` (dikecualikan dari Git):

```
laporan/
├── grab/
│   └── 2026-06-02_to_2026-06-08/
│       ├── NamaOutlet_Cabang.xlsx    # Per portal
│       └── 0Master.xlsx              # Gabungan semua portal
└── shopee/
    └── 2026-06-02_to_2026-06-08/
        ├── MerchantName_Transactions_*.xlsx   # Per merchant (raw + analyzed)
        └── 0Master.xlsx                       # Gabungan semua merchant
```

### Format Kolom Master Grab

`Flag`, `Month`, `Merchant Name`, `Merchant ID`, `Store Name`, `Transaction ID`, `Amount`, `Net Sales`, `Grab Fee`, `Total`, dll.

### Format Kolom Master Shopee

`Merchant Name`, `Store ID`, `Nama Toko`, `No. Pesanan`, `Waktu Penyelesaian`, `Nilai Transaksi`, `Harga Makanan`, `Diskon`, `Commission`, `Revenue`, `OFD Fees`, dll.

---

## 🔄 Alur Pipeline

### Pipeline Grab

```
[Google Sheets]  →  Ambil daftar outlet & kredensial
      ↓
[Playwright]     →  Login & unduh laporan CSV via API Grab (paralel, max 3)
      ↓
[Retry Logic]    →  Ulangi akun gagal secara sekuensial
      ↓
[Merge]          →  Gabungkan semua CSV → 0Master.xlsx
      ↓
[Output]         →  laporan/grab/<range>/
```

### Pipeline Shopee

```
[Google Sheets]  →  Ambil daftar merchant ShopeeFood Live
      ↓
Phase 1: [Selenium]  →  Login → switch merchant → trigger export
      ↓
Phase 2: [API Poll]  →  Polling status laporan → unduh .xlsx
      ↓
Phase 3: [Analyze]   →  Hitung Commission, Revenue, OFD Fees
      ↓
Phase 4: [Merge]     →  Gabungkan semua → 0Master.xlsx
      ↓
[Output]             →  laporan/shopee/<range>/
```

---

## 🛠️ Pengembangan

### Toggle Konfigurasi Kode

Di `grab/main.py` dan `shopee/run_weekly.py`, terdapat toggle global:

```python
ENABLE_GSHEETS_PUSH = False   # Set True untuk push ke Google Sheets
ENABLE_POSTGRES_PUSH = False  # Set True untuk sinkronisasi ke PostgreSQL
```

### Menjalankan Modul Secara Terpisah

```bash
# Pipeline Grab saja
cd grab
uv run python main.py --start-date 2026-06-02 --end-date 2026-06-08

# Pipeline Shopee saja
cd shopee
uv run python run_weekly.py --start 2026-06-02 --end 2026-06-08 --merchant "Nama Merchant"

# Skip fase download, hanya merge file yang sudah ada
cd shopee
uv run python run_weekly.py --skip-download
```

---

## 📋 Sumber Data Master

Daftar merchant, outlet, cabang, dan kredensial diambil dari Google Sheets internal (akses terbatas). Pipeline secara otomatis mem-filter hanya entri dengan:
- `Aplikasi` = `GrabFood` / `ShopeeFood`  
- `Status` = `Live`

Cache lokal disimpan sementara di `shopee/data/master_merchants_cache.csv` untuk mengurangi beban request.

---

## 🔐 File yang Diabaikan Git

Berikut file/direktori yang **tidak** masuk ke repositori:

| Path | Keterangan |
|---|---|
| `.env` | Kredensial environment |
| `laporan/` | Output laporan |
| `logs/` | Log eksekusi |
| `*.xlsx`, `*.csv` | File laporan mentah dan olahan |
| `*credentials*.json` | File kredensial |
| `shopee/data/*_cache.csv` | Cache merchant |
| `.venv/` | Virtual environment |

---

## 🐛 Troubleshooting

**Browser tidak muncul / crash**
> Pastikan Google Chrome versi terbaru terinstal. Coba set `headless_grab: true` di `config.json`.

**Error "uv not found"**
> Jalankan `bash start.sh` — script akan otomatis menginstal `uv`.

**Merchant tidak terdeteksi (Shopee)**
> Pastikan nama merchant di Google Sheets persis sama (case-sensitive) dengan yang muncul di portal Shopee seller.

**Gagal download laporan Grab (timeout)**
> Kurangi `max_concurrency` di `config.json` menjadi `1` atau `2` untuk koneksi lambat.

---

## 📄 Lisensi

Internal use only — [superfoodtech.id](https://superfoodtech.id)
