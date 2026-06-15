import os
import time
import json
import threading
import pandas as pd
import sys
# Add parent directory (weekly/) to sys.path so core/ imports work
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from datetime import datetime, timedelta
from concurrent.futures import ThreadPoolExecutor, as_completed
from dotenv import load_dotenv
import requests

from core.browser import get_session, return_to_selector, refresh_tokens, auto_switch_merchant
from core.client import ShopeeClient
from core.logger import get_logger
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.common.by import By

# Load environment variables
load_dotenv()
log = get_logger("omzet_pipeline")

# --- Toggle Konfigurasi Global ---
ENABLE_GSHEETS_PUSH = False   # Set ke True untuk mengizinkan unggah ke Google Sheets
ENABLE_POSTGRES_PUSH = False  # Set ke True untuk mengizinkan unggah ke PostgreSQL (Tabel Gajah)

def subtract_months(dt, months):
    """Helper to subtract calendar months."""
    for _ in range(months):
        dt = (dt - timedelta(days=1)).replace(day=1)
    return dt

def get_live_merchants(app_name="ShopeeFood", max_age_hours=0.01, merchant_filter=None):
    """
    Fetches live merchants from Google Sheets and caches them locally.
    Uses cached data if it's less than max_age_hours old.
    """
    import os
    import time
    from datetime import datetime
    
    url = "https://docs.google.com/spreadsheets/d/e/2PACX-1vQ3tLKBNXDqRgBw0mNhKZFxgvKx-JoiTDzm_s5Ix1cm7O6HCv4IvExOLR2HSRVaXSsx82V348mcr9X4/pub?gid=0&single=true&output=csv"
    cache_path = "data/master_merchants_cache.csv"
    os.makedirs("data", exist_ok=True)
    
    # Cek cache
    if os.path.exists(cache_path):
        mtime = os.path.getmtime(cache_path)
        age_hours = (time.time() - mtime) / 3600
        if age_hours < max_age_hours:
            log.info(f"🔄 [DATA] Using cached merchant list (Age: {age_hours:.1f}h)")
            df = pd.read_csv(cache_path)
            sf_df = df[(df['Aplikasi'] == app_name) & (df['Status'] == 'Live')]
            
            if merchant_filter:
                if "|" in merchant_filter:
                    filter_vals = [m.strip().lower().rstrip('_') for m in merchant_filter.split("|")]
                    sf_df = sf_df[sf_df['Merchant Name'].str.strip().str.lower().str.rstrip('_').isin(filter_vals)]
                else:
                    filter_val = merchant_filter.strip().lower().rstrip('_')
                    sf_df = sf_df[sf_df['Merchant Name'].str.strip().str.lower().str.rstrip('_') == filter_val]
                
            sf_df = sf_df[(sf_df['Merchant Name'] != '-') & (sf_df['Merchant Name'].notna())]
            sf_df = sf_df.drop_duplicates(subset=['Merchant Name'])
            return sf_df['Merchant Name'].tolist()
            
    # Jika tidak ada cache atau sudah usang, download ulang
    log.info("🌐 [DATA] Downloading fresh merchant list from Google Sheets...")
    try:
        cache_buster = f"&t={int(time.time())}" if "?" in url else f"?t={int(time.time())}"
        df = pd.read_csv(url + cache_buster)
        df.to_csv(cache_path, index=False)
        
        sf_df = df[(df['Aplikasi'] == app_name) & (df['Status'] == 'Live')]
        
        if merchant_filter:
            if "|" in merchant_filter:
                filter_vals = [m.strip().lower().rstrip('_') for m in merchant_filter.split("|")]
                sf_df = sf_df[sf_df['Merchant Name'].str.strip().str.lower().str.rstrip('_').isin(filter_vals)]
            else:
                filter_val = merchant_filter.strip().lower().rstrip('_')
                sf_df = sf_df[sf_df['Merchant Name'].str.strip().str.lower().str.rstrip('_') == filter_val]
            
        sf_df = sf_df[(sf_df['Merchant Name'] != '-') & (sf_df['Merchant Name'].notna())]
        sf_df = sf_df.drop_duplicates(subset=['Merchant Name'])
        
        return sf_df['Merchant Name'].tolist()
    except Exception as e:
        log.error(f"⚠️ Failed to fetch/parse merchants: {e}")
        return []

def download_file(url, filename, cookies=None, max_retries=3):
    """Downloads a file from a URL with optional cookies and retries."""
    import requests
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/147.0.0.0 Safari/537.36"
    }
    
    for attempt in range(max_retries):
        try:
            response = requests.get(url, stream=True, cookies=cookies, headers=headers, timeout=30)
            response.raise_for_status()
            with open(filename, 'wb') as f:
                for chunk in response.iter_content(chunk_size=8192):
                    f.write(chunk)
            return True
        except Exception as e:
            if attempt < max_retries - 1:
                log.warning(f"⚠️ Download attempt {attempt+1} failed for {filename}: {e}. Retrying in 5s...")
                time.sleep(5)
            else:
                log.error(f"❌ Failed to download {filename} after {max_retries} attempts: {e}")
    return False


# ── Parallel Polling Helper ───────────────────────────────────────────────────

_download_lock = threading.Lock()  # Ensure thread-safe file path deduplication

def _poll_and_download_merchant(m_name, ctx, report_dir, global_ranges, poll_timeout=1800):
    """
    Polls and downloads exported reports for a single merchant.
    Designed to be run concurrently inside a ThreadPoolExecutor.

    Returns:
        (m_name: str, downloaded: list[tuple[path, label]], error: bool)
    """
    from core.client import ShopeeClient
    from core.logger import get_logger
    log = get_logger("omzet_pipeline")

    client = ShopeeClient(
        tob_token=ctx["tob_token"],
        entity_id=ctx["entity_id"],
        extra_cookies=ctx["cookies"]
    )
    downloaded = []
    start_poll = time.time()
    consecutive_errors = 0
    poll_round = 0

    while len(downloaded) < len(global_ranges) and (time.time() - start_poll) < poll_timeout:
        poll_round += 1
        reports = client.get_report_list()

        if reports is None:  # Network error
            consecutive_errors += 1
            wait = min(10 * (2 ** (consecutive_errors - 1)), 60)
            log.warning(f"  🌐 [THREAD] {m_name}: network error, retrying in {wait}s...")
            time.sleep(wait)
            continue

        consecutive_errors = 0
        found_new = False

        for rep in reports:
            if rep.get("status") not in [2, 3] or not rep.get("download_url"):
                continue
            if not rep.get("create_time", 0) or rep["create_time"] < ctx["start_trigger_time"]:
                continue

            report_name = rep.get("name", f"report_{rep.get('id')}.xlsx")
            base_path = os.path.join(report_dir, f"{m_name.replace(' ', '_')}_{report_name}")

            # Thread-safe path deduplication
            with _download_lock:
                target_path = base_path
                version = 1
                while os.path.exists(target_path):
                    version += 1
                    name_part, ext_part = os.path.splitext(base_path)
                    target_path = f"{name_part}-{version:02d}{ext_part}"
                # Create a placeholder so other threads don't pick the same path
                open(target_path, 'wb').close()

            already = [d[0] for d in downloaded]
            if target_path in already:
                try: os.unlink(target_path)
                except: pass
                continue

            if download_file(rep.get("download_url"), target_path):
                log.info(f"  ✅ [DOWNLOAD] {m_name} → {report_name}")
                downloaded.append((target_path, report_name))
                found_new = True
            else:
                # Remove placeholder on failed download
                try: os.unlink(target_path)
                except: pass

        if not found_new and poll_round % 6 == 0:  # log every ~30s
            elapsed = int(time.time() - start_poll)
            log.info(f"  ⏳ [THREAD] {m_name}: waiting... ({len(downloaded)}/{len(global_ranges)} ready, {elapsed}s elapsed)")

        if len(downloaded) < len(global_ranges):
            time.sleep(5)  # Per-merchant poll interval: 5s (vs old 10s shared across all)

    if len(downloaded) < len(global_ranges):
        log.warning(f"  ⏰ [THREAD] {m_name}: timeout — {len(downloaded)}/{len(global_ranges)} files downloaded.")

    return m_name, downloaded


def run_pipeline():
    import argparse
    parser = argparse.ArgumentParser(description="Shopee Omzet Weekly Pipeline")
    parser.add_argument("--start", type=str, help="Start date (YYYY-MM-DD)", default=None)
    parser.add_argument("--end", type=str, help="End date (YYYY-MM-DD)", default=None)
    parser.add_argument("--output-dir", type=str, help="Override output directory for reports", default=None)
    parser.add_argument("--skip-download", action="store_true", help="Skip browser automation and only process/merge raw files in output directory")
    parser.add_argument("--merchant", type=str, help="Filter specific merchant name to run", default=None)
    args = parser.parse_args()

    # Determine output directory
    report_dir = args.output_dir or "data/reports/weekly"

    # Pre-run cleanup of old Excel files in custom or download runs to ensure clean master aggregation
    import glob
    if not args.skip_download and os.path.exists(report_dir):
        if args.merchant:
            m_underscored = args.merchant.replace(' ', '_').replace('|', '_')
            if len(m_underscored) > 50:
                old_excels = glob.glob(os.path.join(report_dir, "0Master*.xlsx"))
            else:
                old_excels = glob.glob(os.path.join(report_dir, f"*{m_underscored}*.xlsx"))
        else:
            old_excels = glob.glob(os.path.join(report_dir, "*.xlsx"))
            
        old_excels = [f for f in old_excels if not os.path.basename(f).startswith("Master_Weekly_Report_ShopeeFood") and not os.path.basename(f).startswith("0Master")]
            
        if old_excels:
            log.info(f"🧹 Clearing {len(old_excels)} old Excel files in {report_dir} to prepare for fresh run...")
            for f in old_excels:
                try: os.unlink(f)
                except Exception as e: log.debug(f"Failed to delete {f}: {e}")

    # Determine date range
    now = datetime.now()
    if args.start and args.end:
        start_dt = datetime.strptime(args.start, "%Y-%m-%d")
        end_dt = datetime.strptime(args.end, "%Y-%m-%d").replace(hour=23, minute=59, second=59)
        label = f"{start_dt.strftime('%d %b %Y')} - {end_dt.strftime('%d %b %Y')}"
    else:
        # Default to last 7 days (including today)
        end_dt = now.replace(hour=23, minute=59, second=59)
        start_dt = (end_dt - timedelta(days=6)).replace(hour=0, minute=0, second=0)
        label = f"{start_dt.strftime('%d %b %Y')} - {end_dt.strftime('%d %b %Y')} (Last 7 Days)"
        
    global_ranges = [{"start": int(start_dt.timestamp()), "end": int(end_dt.timestamp()), "label": label}]
    
    print("\n" + "=" * 60)
    print(f"  Shopee Omzet - WEEKLY Report Pipeline")
    print(f"  Range: {label}")
    print("=" * 60)

    phone    = os.getenv("SHOPEE_PHONE", "").strip()
    username = os.getenv("SHOPEE_USERNAME", "").strip()
    password = os.getenv("SHOPEE_PASSWORD", "").strip()

    # Fallback: Load Shopee credentials from credentials.json in parent directories if not in env
    if not username or not password:
        try:
            from pathlib import Path
            import json
            for parent in Path(__file__).resolve().parents:
                cred_file = parent / "credentials.json"
                if cred_file.exists():
                    with open(cred_file, "r") as f:
                        creds = json.load(f)
                        if not username:
                            username = creds.get("shopee_username", "").strip()
                        if not password:
                            password = creds.get("shopee_password", "").strip()
                        if not phone:
                            phone = creds.get("shopee_phone", "").strip()
                    break
        except Exception:
            pass

    # Hardcoded/CSV fallback if still not found
    if not username or not password or not phone:
        try:
            log.info("🔍 [DATA] Fetching 'allvbadmin' credentials from Google Sheets...")
            url = "https://docs.google.com/spreadsheets/d/e/2PACX-1vQ3tLKBNXDqRgBw0mNhKZFxgvKx-JoiTDzm_s5Ix1cm7O6HCv4IvExOLR2HSRVaXSsx82V348mcr9X4/pub?gid=0&single=true&output=csv"
            cache_buster = f"&t={int(time.time())}" if "?" in url else f"?t={int(time.time())}"
            df = pd.read_csv(url + cache_buster)
            mask = df.isin(["allvbadmin"]).any(axis=1)
            if mask.any():
                row = df[mask].iloc[0]
                for col in df.columns:
                    if str(row[col]) == "allvbadmin":
                        idx = df.columns.get_loc(col)
                        username = "allvbadmin"
                        phone = str(row.iloc[idx+1]).split(".")[0] if pd.notna(row.iloc[idx+1]) else ""
                        password = str(row.iloc[idx+2]) if pd.notna(row.iloc[idx+2]) else ""
                        log.info("✅ [DATA] Successfully loaded credentials for 'allvbadmin' from Sheets.")
                        break
        except Exception as e:
            log.warning(f"⚠️ Failed to fetch 'allvbadmin' credentials from Sheets: {e}")
            
    # Ultimate fallback if it completely fails
    if not username: username = "allvbadmin"
    if not password: password = "Shopee@321"
    # Load headless setting from config.json walk-up
    headless = True
    try:
        from pathlib import Path
        import json
        for parent in Path(__file__).resolve().parents:
            config_file = parent / "config.json"
            if config_file.exists():
                with open(config_file, "r") as f:
                    headless = json.load(f).get("headless_shopee", True)
                break
    except Exception:
        pass

    if os.environ.get("HEADLESS") == "true":
        headless = True


    # ── 1. Determine Merchants to Process (Data-Driven via G-Sheets) ────
    target_merchants = get_live_merchants(app_name="ShopeeFood", max_age_hours=24, merchant_filter=args.merchant)
    log.info(f"📋 [PROGRESS] Found {len(target_merchants)} live merchants ready to process.")

    if not target_merchants:
        log.error("❌ No merchants to process. Aborting.")
        return

    driver = None
    if args.skip_download:
        log.info("⏭️ [SKIP] Bypassing browser download phase (Phases 1 & 2) as --skip-download is enabled.")
    else:
        try:
            # ── 2. Phase 1: Rapid Trigger (Trigger exports for all) ────────────
            log.info(f"🚀 [PROGRESS] PHASE 1: Triggering Exports for {len(target_merchants)} merchants...")
        
            # Initialize session
            session_data = get_session(username=username or None, password=password or None, phone=phone or None, 
                                       headless=headless, close_browser=False, target_name=target_merchants[0])
            if not session_data: return
            driver = session_data.get("driver")

            merchants_context = {} # Store tokens/ids for each merchant
            failed_merchants = []
            start_time_all = int(time.time())

            for i, merchant_name in enumerate(target_merchants):
                log.info(f"  [{i+1}/{len(target_merchants)}] Triggering: {merchant_name}")
            
                # Switch if not already there
                if i > 0:
                    switch_success = False
                    for retry in range(2):
                        if auto_switch_merchant(driver, merchant_name):
                            switch_success = True
                            break
                        else:
                            log.warning(f"  ⚠️ Retrying switch for {merchant_name} (Attempt {retry+2}/2)...")
                            time.sleep(3)
                
                    if not switch_success:
                        log.warning(f"  ❌ Skipping {merchant_name} after 2 failed switch attempts.")
                        if merchant_name not in failed_merchants: failed_merchants.append(merchant_name)
                        continue
                    time.sleep(3) # Wait for cookies to sync
            
                # Get tokens and VERIFY ID
                session = refresh_tokens(driver)
                if not session or "shopee_tob_token" not in session:
                    log.warning(f"  ❌ Failed to get auth data for {merchant_name}. Skipping.")
                    if merchant_name not in failed_merchants: failed_merchants.append(merchant_name)
                    continue
                active_id = str(session.get("shopee_tob_entity_id") or "")
            
                # Double check if the ID actually changed from previous
                if i > 0 and active_id == merchants_context.get(target_merchants[i-1], {}).get("entity_id"):
                     log.warning("  ⚠️ ID hasn't changed yet. Retrying token refresh...")
                     time.sleep(3)
                     session = refresh_tokens(driver)
                     active_id = str(session.get("shopee_tob_entity_id") or "")

                log.debug(f"  📍 Confirmed ID for {merchant_name}: {active_id}")
            
                # Store context for polling
                merchants_context[merchant_name] = {
                    "entity_id": active_id,
                    "tob_token": session["shopee_tob_token"],
                    "cookies": session.get("extra_cookies", {}),
                    "start_trigger_time": int(time.time())
                }

                # Initialize client and trigger
                client = ShopeeClient(tob_token=session["shopee_tob_token"], entity_id=active_id, extra_cookies=session.get("extra_cookies", {}))
            
                # Assign ranges based on CLI arguments
                ranges = global_ranges
            
                merchants_context[merchant_name]["ranges"] = ranges
                merchants_context[merchant_name]["downloaded"] = []

                # Trigger with retry on network error
                for r in ranges:
                    success = False
                    for trigger_retry in range(3):
                        res = client.export_transaction_report(merchant_ids=[active_id], start_time=r["start"], end_time=r["end"])
                        if res is True:
                            success = True
                            break
                        elif res is None: # Network Error
                            log.warning(f"  ⚠️ Network error during trigger for {merchant_name}. Retrying in 10s... ({trigger_retry+1}/3)")
                            time.sleep(10)
                        else: # API Error (res is False)
                            break
                
                    if not success:
                        log.error(f"  ❌ Failed to trigger export for {merchant_name} range {r.get('label')}")
                    time.sleep(1)

                # Batching delay: 10 merchants per batch, with 1 minute (60s) delay between batches
                if (i + 1) % 10 == 0 and (i + 1) < len(target_merchants):
                    log.info(f"⏳ [BATCH] Batch limit reached ({i + 1} merchants processed). Delaying for 60 seconds before processing the next batch...")
                    time.sleep(60)

            # ── 3. Phase 2: Parallel Polling & Download ─────────────────────────
            n_workers = min(8, len(merchants_context))  # Cap at 8 concurrent threads
            log.info(f"⏳ [PROGRESS] PHASE 2: Parallel polling {len(merchants_context)} merchants ({n_workers} threads)...")
            os.makedirs(report_dir, exist_ok=True)

            with ThreadPoolExecutor(max_workers=n_workers) as executor:
                futures = {
                    executor.submit(
                        _poll_and_download_merchant,
                        m_name, ctx, report_dir, global_ranges
                    ): m_name
                    for m_name, ctx in merchants_context.items()
                }
                for future in as_completed(futures):
                    m_name_done = futures[future]
                    try:
                        _, downloaded_files = future.result()
                        merchants_context[m_name_done]["downloaded"] = downloaded_files
                        log.info(f"  ✔ [BATCH] {m_name_done}: {len(downloaded_files)}/{len(global_ranges)} files")
                    except Exception as exc:
                        log.error(f"  ❌ [BATCH] {m_name_done} thread raised: {exc}")

            # ── Summary ──────────────────────────────────────────────────────────
            log.info("📋 [PROGRESS] Download Phase Complete. Summary:")
            for m_name, ctx in merchants_context.items():
                if len(ctx["downloaded"]) < len(global_ranges):
                    if m_name not in failed_merchants:
                        failed_merchants.append(m_name)
                log.info(f"  🏪 {m_name}: {len(ctx['downloaded'])}/{len(global_ranges)} files")
                for fpath, label in ctx["downloaded"]:
                    log.info(f"     📄 {fpath}")

            # ── Sequential Retry ────────────────────────────────────────────────
            if failed_merchants:
                log.info("\n" + "="*60)
                log.info(f"  [RETRY] Attempting to re-run {len(failed_merchants)} failed merchants sequentially...")
                log.info("="*60)
                
                for f_idx, m_name in enumerate(failed_merchants):
                    log.info(f"\n  [RETRY {f_idx+1}/{len(failed_merchants)}] Re-running sequentially for: {m_name}")
                    
                    # 1. Switch
                    switch_success = False
                    for retry in range(2):
                        if auto_switch_merchant(driver, m_name):
                            switch_success = True
                            break
                        else:
                            time.sleep(3)
                    
                    if not switch_success:
                        log.error(f"  ❌ [RETRY] Failed to switch to {m_name}.")
                        continue
                    time.sleep(3)
                    
                    # 2. Auth
                    session = refresh_tokens(driver)
                    if not session or "shopee_tob_token" not in session:
                        log.error(f"  ❌ [RETRY] Failed to get auth for {m_name}.")
                        continue
                    
                    active_id = str(session.get("shopee_tob_entity_id") or "")
                    client = ShopeeClient(tob_token=session["shopee_tob_token"], entity_id=active_id, extra_cookies=session.get("extra_cookies", {}))
                    
                    # 3. Trigger & Poll Sequentially
                    for r in global_ranges:
                        # Trigger
                        trigger_success = False
                        for trigger_retry in range(3):
                            res = client.export_transaction_report(merchant_ids=[active_id], start_time=r["start"], end_time=r["end"])
                            if res is True:
                                trigger_success = True
                                break
                            elif res is None:
                                time.sleep(10)
                            else:
                                break
                        
                        if not trigger_success:
                            log.error(f"  ❌ [RETRY] Failed to trigger export for {m_name}.")
                            continue
                            
                        # Poll just this merchant
                        start_trigger_time = int(time.time())
                        poll_timeout = 1800
                        start_poll = time.time()
                        downloaded = False
                        
                        while not downloaded and (time.time() - start_poll) < poll_timeout:
                            reports = client.get_report_list()
                            if reports is None:
                                time.sleep(10)
                                continue
                                
                            for rep in reports:
                                if rep.get("status") in [2, 3] and rep.get("download_url"):
                                    if rep.get("create_time", 0) and rep["create_time"] >= start_trigger_time:
                                        report_name = rep.get("name", f"report_{rep.get('id')}.xlsx")
                                        base_target_path = os.path.join(report_dir, f"{m_name.replace(' ', '_')}_{report_name}")
                                        target_path = base_target_path
                                        version = 1
                                        while os.path.exists(target_path):
                                            version += 1
                                            name_part, ext_part = os.path.splitext(base_target_path)
                                            target_path = f"{name_part}-{version:02d}{ext_part}"
                                            
                                        if download_file(rep.get("download_url"), target_path):
                                            log.info(f"  ✅ [RETRY DOWNLOAD] SUCCESS: {m_name} -> {report_name}")
                                            downloaded = True
                                            break
                            if not downloaded:
                                time.sleep(5)
                        
                        if not downloaded:
                            log.error(f"  ❌ [RETRY] Timeout waiting for download: {m_name}")

        finally:
            if driver is not None:
                try:
                    driver.quit()
                except Exception as e:
                    log.debug(f"Failed to quit driver: {e}")
    # ── 4. Phase 3: Scanning and Validating ALL Raw Files in report folder ──
    log.info("📊 [PROGRESS] PHASE 3: Scanning and Validating ALL Raw Files in report folder...")
    all_analyzed_data = []
    
    # Get all xlsx files in report_dir
    import glob
    xlsx_files = glob.glob(os.path.join(report_dir, "*.xlsx"))
    
    # Sort files to ensure deterministic merging order
    xlsx_files.sort()
    
    for fpath in xlsx_files:
        filename = os.path.basename(fpath)
        
        # Skip Master and other compiled/analyzed reports
        if filename.startswith("Master_") or filename.startswith("0Master") or filename.startswith("CUSTOM_") or filename.startswith("Merged_") or filename.endswith("_Analyzed.xlsx"):
            continue
            
        # Determine Merchant Name from filename
        matched_merchant = None
        for m in target_merchants:
            m_underscored = m.replace(' ', '_')
            # Check if filename starts with underscored merchant name followed by underscore
            if filename.startswith(m_underscored + "_"):
                matched_merchant = m
                break
                
        if not matched_merchant:
            # Skip files that do not match the current target merchants to prevent merging them!
            log.info(f"  ⏭️ [SKIP] Raw file '{filename}' does not belong to target merchants. Skipping.")
            continue
                
        try:
            # Pengecekan apakah file memiliki data (tidak kosong)
            df = pd.read_excel(fpath, dtype=str)
            
            if df.empty or len(df) == 0:
                log.warning(f"  ⚠️ [CHECK] Raw file '{filename}' is EMPTY (no transaction rows). Skipping merger.")
                continue
                
            if "Nilai Transaksi" in df.columns and "Harga Makanan" in df.columns:
                log.info(f"  🔍 [CHECK] Raw file '{filename}' has {len(df)} rows. Processing & including in MASTER...")
                # List of exact monetary columns in ShopeeFood reports
                monetary_cols = [
                    'Harga Makanan', 'Diskon', 'Diskon Flash Sale', 'Biaya Tambahan', 
                    'Subsidi Merchant untuk Voucher Deals', 'Subsidi Platform untuk Flash Sale', 
                    'Subsidi Voucher Makanan', 'Diskon Langsung', 'Nilai Transaksi', 
                    'Harga Checkout Murah'
                ]
                
                # Fix monetary columns: handle Shopee's inconsistent thousand separator/decimal format
                def clean_shopee_monetary(val):
                    if pd.isna(val) or str(val).lower() == 'nan': return 0
                    s = str(val).strip()
                    if not s or s == '-': return 0
                    
                    s_cleaned = s.replace('.', '')
                    if ',' in s_cleaned:
                        s_cleaned = s_cleaned.split(',')[0]
                        
                    try:
                        return int(s_cleaned)
                    except:
                        return 0

                for col in monetary_cols:
                    if col in df.columns:
                        df[col] = df[col].apply(clean_shopee_monetary).astype(int)
                
                # Calculate new metrics based on corrected raw values (keep decimals for Commission, Revenue, and OFD Fees)
                commission_real = (df['Nilai Transaksi'] * 0.25).fillna(0)
                revenue_real = (df['Nilai Transaksi'] - commission_real).fillna(0)
                ofd_fees_real = (df['Harga Makanan'] - revenue_real).fillna(0)
                
                # Insert new columns
                df['Commission'] = commission_real
                df['Revenue'] = revenue_real
                df['OFD Fees'] = ofd_fees_real
                
                # Add Merchant Name column at the beginning if not already present
                if "Merchant Name" not in df.columns:
                    df.insert(0, "Merchant Name", matched_merchant)
                
                # Fix scientific notation for Order IDs
                if "No. Pesanan" in df.columns:
                    df["No. Pesanan"] = df["No. Pesanan"].astype(str).str.replace(r'\.0$', '', regex=True)
                    
                # Reformat Waktu Penyelesaian from "07 Mei 2026 23:16" to "2026-05-07 at 23:16"
                if "Waktu Penyelesaian" in df.columns:
                    indo_months = {
                        'Januari': 'Jan', 'Februari': 'Feb', 'Maret': 'Mar', 
                        'April': 'Apr', 'Mei': 'May', 'Juni': 'Jun', 'Juli': 'Jul', 
                        'Agustus': 'Aug', 'September': 'Sep', 'Oktober': 'Oct', 
                        'November': 'Nov', 'Desember': 'Dec',
                        'Jan': 'Jan', 'Feb': 'Feb', 'Mar': 'Mar', 'Apr': 'Apr',
                        'Jun': 'Jun', 'Jul': 'Jul', 'Ags': 'Aug', 'Agu': 'Aug',
                        'Sep': 'Sep', 'Okt': 'Oct', 'Nov': 'Nov', 'Des': 'Dec'
                    }
                    temp_dates = df["Waktu Penyelesaian"].astype(str)
                    for indo, eng in sorted(indo_months.items(), key=lambda x: len(x[0]), reverse=True):
                        temp_dates = temp_dates.str.replace(indo, eng, case=False, regex=False)
                    
                    # Parse to datetime using robust explicit format
                    parsed_dates = pd.to_datetime(temp_dates, format='%d %b %Y %H:%M', errors='coerce')
                    
                    # Where parsing succeeded, apply the new format. Where it failed, keep original.
                    df["Waktu Penyelesaian"] = parsed_dates.dt.strftime('%Y-%m-%d at %H:%M').fillna(df["Waktu Penyelesaian"])
                    
                # Reorder columns to match Google Sheets format
                desired_order = [
                    'Merchant Name', 'Store ID', 'Nama Toko', 'Tipe Transaksi', 'No. Pesanan', 
                    'Waktu Penyelesaian', 'Status', 'Harga Makanan', 'Diskon', 'Diskon Flash Sale', 
                    'Biaya Tambahan', 'Subsidi Merchant untuk Voucher Deals', 
                    'Subsidi Platform untuk Flash Sale', 'Subsidi Voucher Makanan', 
                    'Diskon Langsung', 'Nilai Transaksi', 'Harga Checkout Murah', 'Notes', 
                    'Commission', 'OFD Fees', 'Revenue'
                ]
                final_cols = [c for c in desired_order if c in df.columns] + [c for c in df.columns if c not in desired_order]
                df = df[final_cols]
                
                # Save individual analyzed report by overwriting the raw file in place
                df.to_excel(fpath, index=False)
                log.info(f"     ✅ [DATA] Saved analyzed data (overwriting raw file): {os.path.basename(fpath)}")
                
                all_analyzed_data.append(df)
            else:
                log.warning(f"  ⚠️ [CHECK] Raw file '{filename}' is missing required columns. Skipping.")
        except Exception as e:
            log.error(f"  ❌ Error processing '{filename}': {e}")

    # ── 5. Phase 4: Master Aggregation ───────────────────────────────────
    if all_analyzed_data:
        log.info("📑 [PROGRESS] PHASE 4: Combining all analyzed reports...")
        master_df = pd.concat(all_analyzed_data, ignore_index=True)
        
        # Determine date range for filename from global_ranges
        min_start = min([r['start'] for r in global_ranges])
        max_end = max([r['end'] for r in global_ranges])
        
        # Convert unix timestamp to readable date (DDMMYYYY)
        min_start_str = datetime.fromtimestamp(min_start).strftime('%d%m%Y')
        max_end_str = datetime.fromtimestamp(max_end).strftime('%d%m%Y')
        
        if args.merchant:
            merchant_safe = str(args.merchant).strip().replace(" ", "_").replace("/", "_").replace("\\", "_").replace("|", "_")
            if len(merchant_safe) > 50:
                master_filename = "0Master"
            else:
                master_filename = f"CUSTOM_{merchant_safe}_{min_start_str}_{max_end_str}"
        else:
            master_filename = "0Master"
            
        master_filepath = os.path.join(report_dir, f"{master_filename}.xlsx")
        version = 1
        while os.path.exists(master_filepath):
            version += 1
            master_filepath = os.path.join(report_dir, f"{master_filename}-{version:02d}.xlsx")
        
        master_df.to_excel(master_filepath, index=False)
        log.info(f"🎉 [SUCCESS] Laporan created: {master_filepath}")
        log.info(f"   Total rows: {len(master_df)}")

        # ── 6. Phase 5: Distribution to Google Sheets ──────────────────────
        apps_script_url = "https://script.google.com/macros/s/AKfycbxuqQ72VfP-5f-h-ud1XZDgG47KDwyP8gDg2AFzIjq6JrnZnWGenRs50G06RxsPiSxj/exec"
        if not ENABLE_GSHEETS_PUSH:
            log.info("⏭️ [SKIP] PHASE 5: Distribusi ke Google Sheets dinonaktifkan secara global.")
        elif args.merchant:
            log.info("⏭️ [SKIP] PHASE 5: Custom Merchant run dideteksi. Distribusi ke Google Sheets dilewati untuk mencegah kerusakan data master.")
        elif apps_script_url:
            log.info("📤 [PROGRESS] PHASE 5: Sending data to Google Sheets...")
            
            # Mapping columns to match 'Shopee' sheet headers
            # Target Headers: Flag,Month,Store ID,Store name,Transaction type,Transaction ID (Order ID),Complete Time,Status,Food original price,Item discounts,Flash sale discount,Surcharge fee,Merchant Voucher Deals Subsidy,Platform Flash Sale Subsidy,Food Voucher Subsidy,Food Direct Discount,Transaction amount,Checkout Murah Price,Notes,Net Sales,Commission,Revenue,Move to OE/OP
            
            # Prepare data for mapping
            dist_df = master_df.copy()
            
            # Calculate Month and Flag
            def get_month_from_str(date_str):
                try:
                    # Date format is "YYYY-MM-DD at HH:MM"
                    return date_str.split(" ")[0][:7] # YYYY-MM
                except:
                    return ""

            dist_df["Flag"] = "Final OP"
            dist_df["Month"] = dist_df["Waktu Penyelesaian"].apply(get_month_from_str)
            dist_df["Net Sales"] = dist_df["Harga Makanan"] - dist_df["Diskon"]
            dist_df["Move to OE/OP"] = ""

            mapping = {
                "Flag": "Flag",
                "Month": "Month",
                "Store ID": "Store ID",
                "Nama Toko": "Store name",
                "Tipe Transaksi": "Transaction type",
                "No. Pesanan": "Transaction ID (Order ID)",
                "Waktu Penyelesaian": "Complete Time",
                "Status": "Status",
                "Harga Makanan": "Food original price",
                "Diskon": "Item discounts",
                "Diskon Flash Sale": "Flash sale discount",
                "Biaya Tambahan": "Surcharge fee",
                "Subsidi Merchant untuk Voucher Deals": "Merchant Voucher Deals Subsidy",
                "Subsidi Platform untuk Flash Sale": "Platform Flash Sale Subsidy",
                "Subsidi Voucher Makanan": "Food Voucher Subsidy",
                "Diskon Langsung": "Food Direct Discount",
                "Nilai Transaksi": "Transaction amount",
                "Harga Checkout Murah": "Checkout Murah Price",
                "Notes": "Notes",
                "Net Sales": "Net Sales",
                "Commission": "Commission",
                "Revenue": "Revenue",
                "Move to OE/OP": "Move to OE/OP"
            }

            # Select and rename columns
            final_df = dist_df[list(mapping.keys())].rename(columns=mapping)
            
            # Convert to list of dicts for JSON (Handle NaN values)
            payload = final_df.fillna("").to_dict(orient="records")
            
            # Send to Apps Script with retries
            success_send = False
            for send_attempt in range(3):
                try:
                    response = requests.post(
                        f"{apps_script_url}?sheet=Shopee&clear=true",
                        json=payload,
                        timeout=90 # Increased timeout
                    )
                    if response.status_code == 200:
                        res_json = response.json()
                        if res_json.get("status") == "success":
                            log.info(f"✅ [SUCCESS] Sent {len(payload)} rows to Shopee sheet.")
                            success_send = True
                            break
                        else:
                            log.error(f"❌ [ERROR] Apps Script error: {res_json.get('message')}")
                            break # If it's a logic error, don't retry
                    else:
                        log.warning(f"⚠️ Failed to send data (Attempt {send_attempt+1}/3): HTTP {response.status_code}")
                except Exception as e:
                    log.warning(f"⚠️ Connection error to Apps Script (Attempt {send_attempt+1}/3): {e}")
                
                if send_attempt < 2:
                    time.sleep(10)
            
            if not success_send:
                log.error("❌ Failed to send data to Google Sheets after multiple attempts.")
        else:
            log.warning("⚠️ [SKIP] APPS_SCRIPT_URL not found in .env. Skipping distribution.")

        # ── 7. Phase 6: Sync to PostgreSQL ──────────────────────────────────
        if not ENABLE_POSTGRES_PUSH:
            log.info("⏭️ [SKIP] PHASE 6: Sinkronisasi ke PostgreSQL dinonaktifkan secara global.")
        elif args.merchant:
            log.info("⏭️ [SKIP] PHASE 6: Custom Merchant run dideteksi. Sinkronisasi PostgreSQL dilewati untuk mencegah kerusakan data master.")
        else:
            try:
                log.info("🐘 [PROGRESS] PHASE 6: Syncing data to PostgreSQL...")
                from database.db_manager import DatabaseManager
                db = DatabaseManager()
                db.ingest_shopee(final_df)
                db.refresh_master()
                log.info("✅ [SUCCESS] Data successfully pushed to Master Table (Tabel Gajah).")
            except Exception as e:
                log.info(f"⏭️ [SKIP] PostgreSQL sync skipped (DB is temporarily inactive or offline).")

    # Driver cleanup handled in finally block of download phase
    pass


if __name__ == "__main__":
    run_pipeline()
