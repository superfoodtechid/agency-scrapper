from __future__ import annotations

import argparse
import os
from pathlib import Path

import pandas as pd
import requests
import sys
from dotenv import load_dotenv

# Load environment variables
load_dotenv()


BASE_DIR = Path(__file__).resolve().parent
DEFAULT_INPUT = BASE_DIR / "downloads" / "grab_transactions_3months_(01-02-26_to_30-04-26).csv"
DEFAULT_OUTPUT = BASE_DIR / "monthly_summary_wide.xlsx"
DATE_START = pd.Timestamp("2026-02-01")
DATE_END = pd.Timestamp("2026-04-30 23:59:59")


def resolve_input_path(raw_path: str | None) -> Path:
	if raw_path:
		return Path(raw_path).expanduser().resolve()

	# Cari file terbaru di folder downloads
	downloads_dir = BASE_DIR / "downloads"
	if downloads_dir.exists():
		files = list(downloads_dir.glob("grab_transactions_*.csv"))
		if files:
			# Ambil file yang paling baru dimodifikasi
			latest_file = max(files, key=lambda p: p.stat().st_mtime)
			print(f"Menggunakan file terbaru: {latest_file.name}")
			return latest_file

	return DEFAULT_INPUT


def load_transactions(csv_path: Path) -> pd.DataFrame:
	if not csv_path.exists():
		raise FileNotFoundError(f"CSV file not found: {csv_path}")

	df = pd.read_csv(csv_path)

	required_columns = {"Created On", "Long Order ID", "Net Sales", "Category"}
	missing_columns = required_columns.difference(df.columns)
	if missing_columns:
		raise ValueError(f"Missing required columns: {', '.join(sorted(missing_columns))}")

	return df


def summarize_monthly(df: pd.DataFrame, username: str = None) -> pd.DataFrame:
	working = df.copy()

	working["Updated On"] = pd.to_datetime(working["Updated On"], errors="coerce", format="%d %b %Y %I:%M %p")
	working["Long Order ID"] = working["Long Order ID"].fillna("").astype(str).str.strip()
	working["Category"] = working["Category"].fillna("").astype(str).str.strip().str.casefold()
	working["Net Sales"] = pd.to_numeric(working["Net Sales"], errors="coerce").fillna(0)
	working["Status"] = working["Status"].fillna("").astype(str).str.strip().str.casefold()

	valid_long_order_id = working["Long Order ID"].str.match(r"^[A-Za-z0-9-]+$", na=False)
	
	# An order is valid if it has a proper ID and is a Payment OR an Adjustment
	# We exclude Cancelled orders
	is_order_category = working["Category"].isin(["payment", "adjustment"])
	is_not_cancelled = working["Status"].ne("cancelled")
	
	valid_orders = working.loc[valid_long_order_id & is_order_category & is_not_cancelled].copy()
	valid_orders = valid_orders.loc[valid_orders["Updated On"].notna()].copy()
	valid_orders = valid_orders.loc[
		(valid_orders["Updated On"] >= DATE_START) & (valid_orders["Updated On"] <= DATE_END)
	].copy()
	valid_orders["Month"] = valid_orders["Updated On"].dt.to_period("M").dt.to_timestamp()

	summary = (
		valid_orders.groupby("Month", as_index=False)
		.agg(
			Order_Count=("Long Order ID", "count"), # Count rows to match stakeholder requirement
			Omzet_Net_Sales=("Net Sales", "sum"),
		)
		.sort_values("Month")
		.reset_index(drop=True)
	)

	summary.insert(0, "Username", username or os.getenv("GRAB_USERNAME", "unknown"))

	return summary


def format_rupiah(value: float | int | None) -> str:
	if pd.isna(value):
		return "Rp0"

	number = round(float(value))
	return f"Rp{number:,.0f}".replace(",", ".")


def summarize_wide(df: pd.DataFrame, username: str = None) -> pd.DataFrame:
	monthly = summarize_monthly(df, username)
	if monthly.empty:
		return pd.DataFrame()

	months = monthly["Month"].sort_values().tolist()
	rows = {"Username": username or os.getenv("GRAB_USERNAME", "unknown")}

	for idx, month in enumerate(months, start=1):
		month_data = monthly.loc[monthly["Month"].eq(month)].iloc[0]
		rows[f"Omzet Bulan ke-{idx}"] = float(month_data["Omzet_Net_Sales"])

	for idx, month in enumerate(months, start=1):
		month_data = monthly.loc[monthly["Month"].eq(month)].iloc[0]
		rows[f"Order Bulan ke-{idx}"] = int(month_data["Order_Count"])

	return pd.DataFrame([rows])


def print_summary(summary: pd.DataFrame) -> None:
	if summary.empty:
		print("Tidak ada data valid yang cocok dengan aturan filter.")
		return

	display = summary.copy()
	display["Month"] = display["Month"].dt.strftime("%Y-%m")
	display["Omzet_Net_Sales"] = display["Omzet_Net_Sales"].apply(format_rupiah)

	print(display.to_string(index=False))


def push_to_gsheet(username: str, wide_summary: pd.DataFrame, outlet: str = "", branch: str = "") -> None:
	url = "https://script.google.com/macros/s/AKfycbz8zCLNqDnVaz6Iau7uD-hZiynpaHigjtElk6Wlb5onr_Y9pRgfjtEkYm9unr1cNxkq/exec"
	if wide_summary.empty:
		return

	row = wide_summary.iloc[0]

	# Mapping berdasarkan kunci di wide_summary
	payload = {
		"username": str(username),
		"outlet": str(outlet),
		"branch": str(branch),
		"omzet1": float(row.get("Omzet Bulan ke-1", 0)),
		"omzet2": float(row.get("Omzet Bulan ke-2", 0)),
		"omzet3": float(row.get("Omzet Bulan ke-3", 0)),
		"order1": int(row.get("Order Bulan ke-1", 0)),
		"order2": int(row.get("Order Bulan ke-2", 0)),
		"order3": int(row.get("Order Bulan ke-3", 0)),
	}

	print(f"\nPushing data to Google Sheets for {username}...")
	try:
		# Apps Script requires follow redirects (default in requests)
		response = requests.post(url, json=payload, timeout=30)
		if response.status_code == 200:
			try:
				result = response.json()
				if result.get("status") == "success":
					print("✓ Berhasil dikirim ke Google Sheets!")
				else:
					print(f"✗ Gagal: {result.get('message')}")
			except:
				# Sometimes Apps Script returns HTML error page even with 200
				print("✓ Push terkirim (cek GSheet jika status tidak muncul)")
		else:
			print(f"✗ Gagal mengirim (HTTP {response.status_code})")
	except Exception as e:
		print(f"✗ Error saat push ke Google Sheets: {str(e)}")


def main(username: str = None, outlet: str = "", branch: str = "") -> None:
	parser = argparse.ArgumentParser(
		description="Hitung omzet per bulan dan total order per bulan dari file Grab transactions.",
	)
	parser.add_argument("csv_path", nargs="?", help="Path file CSV transaksi")
	parser.add_argument(
		"--output",
		default=str(DEFAULT_OUTPUT),
		help="Path output CSV ringkasan bulanan",
	)
	args, _ = parser.parse_known_args()

	input_path = resolve_input_path(args.csv_path)
	output_path = Path(args.output).expanduser().resolve()

	df = load_transactions(input_path)
	summary = summarize_monthly(df, username)
	wide_summary = summarize_wide(df, username)
	
	if outlet:
		wide_summary.insert(0, "Outlet", outlet)
	if branch:
		wide_summary.insert(1, "Branch", branch)

	output_path.parent.mkdir(parents=True, exist_ok=True)
	# if output_path.suffix.lower() == ".xlsx":
	# 	with pd.ExcelWriter(output_path, engine="openpyxl") as writer:
	# 		wide_summary.to_excel(writer, index=False, sheet_name="Summary")

	# 	from openpyxl import load_workbook
	# 	from openpyxl.styles import Alignment, Font

	# 	workbook = load_workbook(output_path)
	# 	worksheet = workbook["Summary"]

	# 	for cell in worksheet[1]:
	# 		cell.font = Font(bold=True)
	# 		cell.alignment = Alignment(horizontal="center", vertical="center")

	# 	for row in worksheet.iter_rows(min_row=2):
	# 		for cell in row:
	# 			cell.alignment = Alignment(horizontal="center", vertical="center")

	# 	for column in worksheet.columns:
	# 		max_length = 0
	# 		column_letter = column[0].column_letter
	# 		for cell in column:
	# 			cell_value = "" if cell.value is None else str(cell.value)
	# 			max_length = max(max_length, len(cell_value))
	# 		worksheet.column_dimensions[column_letter].width = max(max_length + 2, 18)

	# 	workbook.save(output_path)
	# else:
	# 	wide_summary.to_csv(output_path, index=False)

	print_summary(summary)
	if not wide_summary.empty:
		print("\nFormat spreadsheet (Data Mentah):")
		with pd.option_context("display.max_columns", None, "display.width", 200):
			print(wide_summary.to_string(index=False))

	# Push ke Google Sheets
	# user_to_push = username or os.getenv("GRAB_USERNAME", "unknown")
	# push_to_gsheet(user_to_push, wide_summary, outlet, branch)

	# 🐘 SYNC KE POSTGRESQL (NEW)
	try:
		print("\n🐘 Syncing raw transactions to PostgreSQL...")
		from database.db_manager import DatabaseManager
		db = DatabaseManager()
		db.ingest_grab(df)
		db.refresh_master()
		print("✅ [DB] Successfully pushed to Master Table.")
	except Exception as e:
		print(f"⏭️ [SKIP] PostgreSQL sync skipped (DB is temporarily inactive or offline).")

	# print(f"\nRingkasan disimpan ke: {output_path}")
	print("\n⏭️ [SKIP] Local Excel saving disabled by user request.")


if __name__ == "__main__":
	main()
