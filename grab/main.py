import argparse
import asyncio
import io
import os
import shutil
from pathlib import Path

import pandas as pd
import requests
from dotenv import load_dotenv
import sys
import os

# --- Toggle Konfigurasi Global ---
ENABLE_GSHEETS_PUSH = False  # Set ke True untuk mengizinkan unggah ke Google Sheets

# Add current directory to sys.path to allow importing grab_api_scraper
sys.path.append(os.path.dirname(os.path.abspath(__file__)))

import logging
from logging.handlers import RotatingFileHandler
from datetime import datetime

from grab_api_scraper import run_api_download_for_portal, validate_credentials

# --- Logging Setup ---
def setup_logger():
    os.makedirs("logs", exist_ok=True)
    # Generate timestamped filename
    timestamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    log_file = f"logs/grab_run_{timestamp}.log"
    
    # Only clean up non-log files (like old screenshots)
    for f in Path("logs").glob("*"):
        if f.is_file() and not f.name.endswith(".log"):
            try: f.unlink()
            except: pass

    logger = logging.getLogger("GrabAuto")
    logger.setLevel(logging.INFO)
    # Clear existing handlers if any (for notebook/interactive environments)
    if logger.hasHandlers():
        logger.handlers.clear()

    formatter = logging.Formatter('[%(asctime)s] %(message)s', datefmt='%Y-%m-%d %H:%M:%S')

    # Console
    ch = logging.StreamHandler(sys.stdout)
    ch.setFormatter(formatter)
    logger.addHandler(ch)

    # File
    fh = logging.FileHandler(log_file, mode='w', encoding='utf-8')
    fh.setFormatter(formatter)
    logger.addHandler(fh)
    
    return logger

log = setup_logger()

CSV_URL = "https://docs.google.com/spreadsheets/d/e/2PACX-1vQ3tLKBNXDqRgBw0mNhKZFxgvKx-JoiTDzm_s5Ix1cm7O6HCv4IvExOLR2HSRVaXSsx82V348mcr9X4/pub?gid=0&single=true&output=csv"

async def run_all(date_start: str = None, date_end: str = None, output_dir: str = None, user_filter: str = None, outlet_filter: str = None, branch_filter: str = None):
    # Reload env just in case
    load_dotenv(override=True)
    
    log.info(f"Fetching merchant list from spreadsheet...")
    try:
        resp = requests.get(CSV_URL, timeout=30)
        resp.raise_for_status()
        df = pd.read_csv(io.StringIO(resp.text))
        
        # Filter for GrabFood and Status Live
        grab_df = df[df["Aplikasi"].str.contains("Grab", na=False, case=False)]
        grab_df = grab_df[grab_df["Status"].str.contains("Live", na=False, case=False)]
        
        portals = []
        for idx, row in grab_df.iterrows():
            user_sf = row.get("Nama Pengguna.1")
            user_mt = row.get("Nama Pengguna")
            pwd_sf = row.get("Kata Sandi.1")
            pwd_mt = row.get("Kata Sandi")
            
            user = user_sf if pd.notna(user_sf) and str(user_sf).strip() != "-" else user_mt
            pwd = pwd_sf if pd.notna(pwd_sf) and str(pwd_sf).strip() != "-" else pwd_mt
            
            if pd.notna(user) and pd.notna(pwd) and str(user).strip() != "-" and str(pwd).strip() != "-":
                u_str = str(user).strip()
                p_str = str(pwd).strip()
                outlet = str(row.get("Nama Outlet", "Unknown")).strip()
                
                # Di Master DB, kolom Cabang tidak ada, gunakan Brand
                branch_val = row.get("Cabang", row.get("Brand", ""))
                branch = str(branch_val).strip() if pd.notna(branch_val) else ""
                
                # Apply custom outlet and branch filters internally
                if outlet_filter:
                    if "|" in outlet_filter:
                        valid_outlets = [o.strip().lower() for o in outlet_filter.split("|")]
                        if str(outlet).strip().lower() not in valid_outlets: continue
                    elif str(outlet).strip().lower() != str(outlet_filter).strip().lower():
                        continue
                if branch_filter:
                    if "|" in branch_filter:
                        valid_branches = [b.strip().lower() for b in branch_filter.split("|")]
                        if str(branch).strip().lower() not in valid_branches: continue
                    elif str(branch).strip().lower() != str(branch_filter).strip().lower():
                        continue
                
                # Smart credential validation
                is_valid, err_msg = validate_credentials(u_str, p_str)
                if not is_valid:
                    log.warning(f"⚠️  [VALIDATION WARNING] Row #{idx+1} for '{outlet} ({branch})' has invalid credentials: {err_msg}")
                    
                portals.append({
                    "id": len(portals) + 1,
                    "outlet": outlet,
                    "branch": branch,
                    "user": u_str,
                    "pwd": p_str
                })

        
    except Exception as e:
        log.error(f"Failed to fetch or parse spreadsheet: {e}")
        return

    # Determine output directory
    if output_dir:
        laporan_dir = Path(output_dir)
    else:
        start_str = date_start or "all"
        end_str = date_end or "all"
        laporan_dir = Path("laporan") / f"{start_str}_{end_str}"
    
    # Auto-cleanup old CSV files is disabled as per user request to keep existing files

    log.info("="*60)
    log.info(f"  GRAB MULTI-PORTAL AUTOMATION ({len(portals)} portals)")
    
    unique_users = {}
    for p_info in portals:
        u = p_info["user"]
        if user_filter and user_filter.lower() not in u.lower():
            continue

        if u not in unique_users:
            unique_users[u] = {"pwd": p_info["pwd"], "portals": []}
        unique_users[u]["portals"].append(p_info)
    
    log.info(f"  Unique Accounts: {len(unique_users)}")
    log.info("="*60)
    
    from playwright.async_api import async_playwright
    
    async with async_playwright() as p:
        # Load headless setting and concurrency from config.json walk-up
        headless_env = True
        concurrency_limit = 3
        try:
            import json
            for parent in Path(__file__).resolve().parents:
                config_file = parent / "config.json"
                if config_file.exists():
                    with open(config_file, "r") as f:
                        config_data = json.load(f)
                        headless_env = config_data.get("headless_grab", True)
                        concurrency_limit = config_data.get("max_concurrency", 3)
                    break
        except Exception:
            pass
        browser = await p.chromium.launch(headless=headless_env)
        semaphore = asyncio.Semaphore(concurrency_limit)
        failures = []

        async def process_user(username, info):
            password = info["pwd"]
            related_portals = info["portals"]
            first_outlet = related_portals[0]["outlet"]
            
            async with semaphore:
                log.info(f"[ACCOUNT] Starting for: {username} ({first_outlet})")
                try:
                    downloaded_file, err = await run_api_download_for_portal(
                        username, password, 
                        start_date=date_start, 
                        end_date=date_end,
                        browser=browser
                    )

                    if not downloaded_file:
                        log.error(f"  ✗ [ACCOUNT] {username} Failed: {err}")
                        failures.append({"user": username, "error": err, "outlets": [p["outlet"] for p in related_portals]})
                        return

                    for portal in related_portals:
                        portal_id = portal["id"]
                        outlet_name = f"{portal['outlet']} ({portal['branch']})" if portal['branch'] else portal['outlet']
                        laporan_dir.mkdir(parents=True, exist_ok=True)
                        
                        portal_safe_name = f"{portal['outlet']}_{portal['branch']}" if portal['branch'] else f"{portal['outlet']}"
                        portal_safe_name = portal_safe_name.replace("/", "_").replace("\\", "_")
                        
                        version = 1
                        dest_xlsx = laporan_dir / f"{portal_safe_name}.xlsx"
                        while dest_xlsx.exists():
                            version += 1
                            dest_xlsx = laporan_dir / f"{portal_safe_name}-{version:02d}.xlsx"
                        
                        # Convert CSV to XLSX
                        tmp_df = pd.read_csv(downloaded_file, dtype=str)
                        tmp_df.to_excel(dest_xlsx, index=False)
                        log.info(f"  ✓ [PORTAL {portal_id}] {outlet_name} — Saved to: {dest_xlsx.name}")

                except Exception as e:
                    log.error(f"  ✗ [ACCOUNT] {username} CRITICAL ERROR: {str(e)}")

        tasks = [process_user(u, info) for u, info in unique_users.items()]
        await asyncio.gather(*tasks)
        
        # --- Sequential Retry for Failed Accounts ---
        if failures:
            log.info("\n" + "="*60)
            log.info(f"  [RETRY] Attempting to re-run {len(failures)} failed accounts sequentially to resolve network/concurrency issues...")
            log.info("="*60)
            
            retry_failures = list(failures)
            failures.clear() # Clear so it only contains true failures after retry
            
            for f in retry_failures:
                username = f["user"]
                info = unique_users[username]
                log.info(f"\n  [RETRY ACCOUNT] Re-running sequentially for: {username}")
                await process_user(username, info)
                
        await browser.close()

    log.info("="*60)
    log.info("  ALL PORTALS FINISHED PROCESSING")
    if failures:
        log.info("-" * 60)
        log.info(f"  FAILED ACCOUNTS ({len(failures)}):")
        for f in failures:
            log.info(f"  - {f['user']}: {f['error']}")
    else:
        log.info("  ✓ ALL ACCOUNTS PROCESSED SUCCESSFULLY")
    log.info("="*60)

    # --- Gabungkan semua CSV menjadi file master ---
    if output_dir:
        laporan_dir = Path(output_dir)
    else:
        start_str = date_start or "all"
        end_str = date_end or "all"
        laporan_dir = Path("laporan") / f"{start_str}_{end_str}"

    xlsx_files = sorted(laporan_dir.glob("*.xlsx")) if laporan_dir.exists() else []
    # Exclude master file jika sudah ada dari run sebelumnya
    xlsx_files = [f for f in xlsx_files if f.stem != "MASTER" and f.stem != "0Master" and not f.stem.startswith("0Master-") and not f.stem.startswith("MASTER-") and not f.stem.startswith("CUSTOM_") and not f.stem.startswith("BASELINE_CUSTOM_")]
    if outlet_filter or branch_filter:
        valid_stems = []
        for p_info in portals:
            portal_safe_name = f"{p_info['outlet']}_{p_info['branch']}" if p_info['branch'] else f"{p_info['outlet']}"
            portal_safe_name = portal_safe_name.replace("/", "_").replace("\\", "_")
            valid_stems.append(portal_safe_name)
        xlsx_files = [f for f in xlsx_files if f.stem in valid_stems]

    if not xlsx_files:
        print("\n[SKIP] Tidak ada file XLSX untuk digabung.")
        return

    print(f"\nScanning and validating {len(xlsx_files)} raw XLSX files for master merging...")
    frames = []
    for xlsx_path in xlsx_files:
        try:
            df = pd.read_excel(xlsx_path, dtype=str)
            if df.empty or len(df) == 0:
                print(f"  ⚠️ [CHECK] Raw file '{xlsx_path.name}' is EMPTY (no transaction rows). Skipping merger.")
                continue
                
            print(f"  🔍 [CHECK] Raw file '{xlsx_path.name}' has {len(df)} rows. Including in MASTER...")
            df.insert(0, "Merchant", xlsx_path.stem)
            frames.append(df)
        except Exception as e:
            print(f"  ❌ [CHECK] Gagal membaca atau memproses '{xlsx_path.name}': {e}")

    if not frames:
        log.info("⏭️ [SKIP] Tidak ada file CSV yang memiliki data untuk digabung.")
        return

    master_df = pd.concat(frames, ignore_index=True)

    # Deduplicate based on Transaction ID if it exists
    if "Transaction ID" in master_df.columns:
        before_count = len(master_df)
        master_df = master_df.drop_duplicates(subset=["Transaction ID"], keep="first")
        after_count = len(master_df)
        if before_count > after_count:
            log.info(f"  [INFO] Menghapus {before_count - after_count} baris duplikat.")

    # Normalisasi kolom tanggal
    date_cols = ["Updated On", "Created On", "Transfer Date"]
    for col in date_cols:
        if col in master_df.columns:
            parsed = pd.to_datetime(master_df[col], format="%d %b %Y %I:%M %p", errors="coerce")
            mask_failed = parsed.isna() & master_df[col].notna()
            if mask_failed.any():
                parsed[mask_failed] = pd.to_datetime(master_df.loc[mask_failed, col], errors="coerce")
            master_df[col] = parsed.dt.strftime("%Y-%m-%d at %H:%M").where(parsed.notna(), other=master_df[col])

    # Simpan sebagai Excel Lokal (Pemisahan Penamaan Pelaporan)
    filename_prefix = "0Master"

    master_xlsx = laporan_dir / f"{filename_prefix}.xlsx"
    version = 1
    while master_xlsx.exists():
        version += 1
        master_xlsx = laporan_dir / f"{filename_prefix}-{version:02d}.xlsx"
    master_df.to_excel(master_xlsx, index=False, sheet_name="All Merchants")

    log.info(f"✓ Laporan Excel: {master_xlsx}")
    log.info(f"  Total baris  : {len(master_df):,} | Merchant: {master_df['Merchant'].nunique()}")

    # --- Distribusi ke Google Sheets via Apps Script ---
    apps_script_url = "https://script.google.com/macros/s/AKfycbxuqQ72VfP-5f-h-ud1XZDgG47KDwyP8gDg2AFzIjq6JrnZnWGenRs50G06RxsPiSxj/exec"
    if not ENABLE_GSHEETS_PUSH:
        log.info("\n⏭️ [SKIP] Distribusi ke Google Sheets dinonaktifkan secara global.")
    elif outlet_filter or branch_filter:
        log.info("\n⏭️ [SKIP] Custom/Single Outlet run dideteksi. Distribusi ke Google Sheets dilewati untuk mencegah kerusakan data master.")
    elif apps_script_url:
        log.info("\n📤 [PROGRESS] Mengirim data ke Google Sheets...")
        
        dist_df = master_df.copy()
        
        # Tambah Flag dan Month
        dist_df["Flag"] = "Final OP"
        
        def get_month_from_grab(date_str):
            try:
                # Format: "YYYY-MM-DD at HH:MM"
                return date_str.split(" ")[0][:7]
            except:
                return ""
        
        if "Created On" in dist_df.columns:
            dist_df["Month"] = dist_df["Created On"].apply(get_month_from_grab)
        else:
            dist_df["Month"] = ""
            
        dist_df["Move to OE/OP"] = ""
        
        # Headers target Grab (sesuai urutan di sheet)
        target_headers = [
            "Flag", "Month", "Merchant Name", "Merchant ID", "Store Name", "Store ID", 
            "Updated On", "Created On", "Type", "Category", "Subcategory", "Status", 
            "Transaction ID", "Linked Transaction ID", "Partner transaction ID 1", 
            "Partner transaction ID 2", "Long Order ID", "Short Order ID", "Booking ID", 
            "Order Channel", "Order Type", "Payment Method", "Receiving account / Source of fund", 
            "Terminal ID", "Channel", "Offer Type", "Grab Fee (%)", "Points Multiplier", 
            "Points Issued", "Settlement ID", "Transfer Date", "Amount", "Tax on Order Value", 
            "Restaurant Packaging Charge", "Non-Member Fee", "Restaurant Service Charge", 
            "Offer", "Discount (Merchant-Funded)", "Delivery Fee Discount (Merchant-Funded)", 
            "Delivery Charge (Grab Online Store)", "Delivery Charge (Merchant Delivery)", 
            "GrabExpress Delivery Service Fee", "Net Sales", "Net MDR", "Tax on MDR", 
            "Grab Fee", "Marketing success fee", "Delivery Commission", "Channel Commission", 
            "Order commission", "GrabFood / GrabMart Other Commission", "GrabKitchen Commission", 
            "GrabKitchen Other Commission", "Withholding Tax", "Total", "Tax on MDR (%)", 
            "Delivery Commission (%)", "Channel Commission (%)", "Order Commission (%)",
            "Tax on GrabFood / GrabMart Commission, Adjustments, Ads",
            "Tax on Total GrabKitchen Commission", "Cancellation Reason", "Cancelled by", 
            "Reason for Refund", "Description", "Incident group", "Incident alias", 
            "Customer refund Item", "Appeal link", "Appeal status", "Package/Voucher Used", 
            "Attributed Service Fee", "Attributed Promo", "Move to OE/OP"
        ]

        # Rename columns from MASTER to match target headers
        rename_map = {
            "Step-up commission": "GrabFood / GrabMart Other Commission",
            "Tax on GrabFood/GrabMart commission, adjustments, ads": "Tax on GrabFood / GrabMart Commission, Adjustments, Ads"
        }
        dist_df = dist_df.rename(columns=rename_map)
        
        # Pastikan semua kolom ada (isi kosong jika tidak ada)
        for col in target_headers:
            if col not in dist_df.columns:
                dist_df[col] = ""
        
        # Pilih kolom sesuai urutan target
        final_df = dist_df[target_headers]
        
        # Payload JSON (Handle NaN values which are not JSON compliant)
        payload = final_df.fillna("").to_dict(orient="records")
        
        try:
            response = requests.post(
                f"{apps_script_url}?sheet=Grab&clear=true",
                json=payload,
                timeout=60
            )
            if response.status_code == 200:
                res_json = response.json()
                if res_json.get("status") == "success":
                    log.info(f"✅ [SUCCESS] Berhasil mengirim {res_json.get('rows_added')} baris ke sheet Grab.")
                else:
                    log.error(f"❌ [ERROR] Apps Script error: {res_json.get('message')}")
            else:
                log.error(f"❌ [ERROR] Gagal mengirim data: HTTP {response.status_code}")
        except Exception as e:
            log.error(f"❌ [ERROR] Gagal terhubung ke Apps Script: {e}")
    else:
        log.info("\n⚠️ [SKIP] APPS_SCRIPT_URL tidak ditemukan di .env. Melewati distribusi ke G-Sheets.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Jalankan scraper Grab multi-portal dan hitung omzet."
    )
    parser.add_argument(
        "--start-date",
        default=None,
        help="Filter awal (inklusif), format YYYY-MM-DD. Contoh: 2026-02-01",
    )
    parser.add_argument(
        "--end-date",
        default=None,
        help="Filter akhir (inklusif), format YYYY-MM-DD. Contoh: 2026-04-30",
    )
    parser.add_argument(
        "--output-dir",
        default=None,
        help="Override output directory for reports.",
    )
    parser.add_argument(
        "--user",
        default=None,
        help="Filter specific username to run.",
    )
    parser.add_argument(
        "--outlet",
        default=None,
        help="Filter specific outlet name to run.",
    )
    parser.add_argument(
        "--branch",
        default=None,
        help="Filter specific branch name to run.",
    )
    args = parser.parse_args()
    asyncio.run(run_all(
        date_start=args.start_date, 
        date_end=args.end_date, 
        output_dir=args.output_dir, 
        user_filter=args.user,
        outlet_filter=args.outlet,
        branch_filter=args.branch
    ))
