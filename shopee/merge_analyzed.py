import os
import pandas as pd
from datetime import datetime
import glob

def merge_analyzed_reports():
    report_dir = "data/reports/weekly"
    pattern = os.path.join(report_dir, "*.xlsx")
    all_files = glob.glob(pattern)
    files = [f for f in all_files if not os.path.basename(f).startswith("Master_") and not os.path.basename(f).startswith("0Master") and not os.path.basename(f).startswith("CUSTOM_") and not os.path.basename(f).startswith("Merged_")]
    
    if not files:
        print("❌ No analyzed files found in data/reports/weekly")
        return

    print(f"📂 Found {len(files)} analyzed files. Merging...")
    
    all_data = []
    for f in files:
        try:
            print(f"  📖 Reading {os.path.basename(f)}...")
            df = pd.read_excel(f)
            all_data.append(df)
        except Exception as e:
            print(f"  ❌ Error reading {f}: {e}")

    if all_data:
        master_df = pd.concat(all_data, ignore_index=True)
        
        # Sort by Merchant Name and Order Time if they exist
        if "Merchant Name" in master_df.columns:
            sort_cols = ["Merchant Name"]
            if "Waktu Pesanan Dibuat" in master_df.columns:
                sort_cols.append("Waktu Pesanan Dibuat")
            master_df = master_df.sort_values(by=sort_cols)

        # Generate filename with timestamp to avoid overwrite confusion
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        output_path = os.path.join(report_dir, f"Merged_Analyzed_Weekly_{timestamp}.xlsx")
        
        master_df.to_excel(output_path, index=False)
        print(f"\n🎉 SUCCESS! Merged report created at: {output_path}")
        print(f"   Total rows: {len(master_df)}")

if __name__ == "__main__":
    merge_analyzed_reports()
