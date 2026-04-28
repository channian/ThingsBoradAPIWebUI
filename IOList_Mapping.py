import pandas as pd
import csv
import os

def ifix_mapping_tool_final(a_file, b_file, output_file):
    print("🚀 啟動 iFIX 自動盤點對接程式...")

    # --- Step 1: 處理 A 檔案 (需求表) ---
    try:
        # 讀取需求表，通常為 UTF-8
        df_a = pd.read_csv(a_file, encoding='utf-8-sig')
    except:
        # 若失敗則嘗試 Excel 常用的 CP950
        df_a = pd.read_csv(a_file, encoding='cp950')

    # 建立大寫匹配 Key
    df_a['JOIN_KEY'] = df_a['Tag Name'].astype(str).str.strip().str.upper()

    # --- Step 2: 處理 B 檔案 (iFIX 導出表) ---
    b_data = []
    current_header = []
    
    with open(b_file, 'r', encoding='cp950', errors='replace') as f:
        # 自動偵測分隔符 (Tab 或 逗號)
        sample = f.readline()
        delimiter = '\t' if '\t' in sample else ','
        f.seek(0)
        
        reader = csv.reader(f, delimiter=delimiter)
        for row in reader:
            if not row or row[0].startswith('['): continue
            
            # 定位欄位名稱行
            if row[0].startswith('!'):
                current_header = [h.strip('! ') for h in row]
                continue
            
            # 處理資料行
            if current_header:
                # 清洗數據：移除 iFIX 特有的驚嘆號與空格
                row_dict = {current_header[idx]: val.strip('! ') for idx, val in enumerate(row) if idx < len(current_header)}
                b_data.append(row_dict)

    df_b = pd.DataFrame(b_data)
    
    # 建立 B 檔案大寫匹配 Key
    if 'A_TAG' in df_b.columns:
        df_b['JOIN_KEY'] = df_b['A_TAG'].astype(str).str.strip().str.upper()
    else:
        print("❌ 錯誤：在 iFIX 資料庫檔案中找不到 'A_TAG' 欄位。")
        return

    # --- Step 3: 執行 Mapping (Left Join) ---
    # 以 A 檔案為準，將 B 檔案資訊併入
    result = pd.merge(df_a, df_b, on='JOIN_KEY', how='left')

    # --- Step 4: 欄位映射賦值 ---
    # 對應關係：(B 檔案代碼 -> A 檔案目標欄位)
    mapping_logic = {
        'A_IODV': 'I/O DEVICE',
        'A_IOAD': 'I/O ADDRESS',
        'A_SCALE_ENABLED': 'SCALE Enabled',
        'A_SCALE_RAWLOW': 'Raw Low',
        'A_SCALE_RAWHIGH': 'Raw High',
        'A_ELO': 'Scaled Low',
        'A_EHI': 'Scaled High',
        'A_DESC': 'Description'
    }

    for source_code, target_field in mapping_logic.items():
        if source_code in result.columns:
            # 將抓到的資料填入目標欄位
            result[target_field] = result[source_code]

    # --- Step 5: 整理最終輸出格式 ---
    # 這是你要求的 12 個欄位標準格式
    final_columns = [
        'Site', 'System', 'SCADA Node Name', 'Tag Name', 
        'I/O DEVICE', 'I/O ADDRESS', 'SCALE Enabled', 
        'Raw Low', 'Raw High', 'Scaled Low', 'Scaled High', 'Description'
    ]
    
    # 檢查並補齊缺失欄位 (例如 Site, System 等預填欄位)
    for col in final_columns:
        if col not in result.columns:
            result[col] = ""

    # 僅提取指定欄位，並過濾掉中間產生的臨時欄位 (如 JOIN_KEY)
    df_output = result[final_columns]

    # --- Step 6: 輸出結果 ---
    df_output.to_csv(output_file, index=False, encoding='utf-8-sig')
    
    # 計算成功率
    success_count = result['A_TAG'].notna().sum()
    fail_count = result['A_TAG'].isna().sum()
    
    print("-" * 30)
    print(f"✅ 自動 Mapping 任務完成！")
    print(f"📊 盤點結果摘要：")
    print(f"   - 需求總數：{len(df_a)} 筆")
    print(f"   - 成功匹配：{success_count} 筆")
    print(f"   - 未找到點位：{fail_count} 筆")
    print(f"💾 檔案已儲存至：{output_file}")
    print("-" * 30)

# --- 使用方式範例 ---
ifix_mapping_tool_final('IGS_mapping_test.csv', 'K18_HVAC.csv', 'Final_Result_TEST01.csv')

# 使用範例
#ifix_mapping_ignore_case('IGS_mapping_test.csv', 'K18_HVAC.csv', 'Final_Result_TEST01.csv')
