# -*- coding: utf-8 -*-
"""唯讀探測：登入 Kepware API Gateway，讀取「單一 Tag」的定義並原樣印出。

用途：確認 Gateway 回傳的 Tag 內容長什麼樣子（尤其資料型態欄位的名稱與數值），
作為「舊點 data_type 校正」功能的依據。**只會發出登入與一次 GET，不會修改任何東西。**

用法（在能連到 Gateway 的電腦上，專案根目錄執行）::

    python scripts/kw_probe_tag.py --url http://GATEWAY:PORT --user 帳號 \\
        --channel K8CHS --device CHS --tag A_OK_01 --tag-group 2F.CHS

- 密碼不用寫在指令裡：省略 --password 時會用隱藏輸入詢問。
- 沒有 tag group（點直接在 device 底下）就省略 --tag-group。
- Gateway 使用自簽憑證時加 --insecure（只影響這次探測）。
- 會沿用 .env 的 KW_GW_USE_PROXY / KW_GW_CA_BUNDLE 等設定，和系統本身的連線方式一致。
"""
import argparse
import getpass
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

try:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".env"))
except Exception:
    pass

import requests  # noqa: E402
from app.kw_gw_client import KepwareGatewayClient  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description="唯讀探測：讀取 Kepware Gateway 上單一 Tag 的定義")
    ap.add_argument("--url", required=True, help="Gateway 位址，例如 http://10.0.0.5:8080")
    ap.add_argument("--user", required=True, help="Gateway 帳號")
    ap.add_argument("--password", default=None, help="Gateway 密碼（省略則隱藏輸入）")
    ap.add_argument("--channel", required=True)
    ap.add_argument("--device", required=True)
    ap.add_argument("--tag", required=True, help="Tag 名稱（不含群組）")
    ap.add_argument("--tag-group", default=None, help="點分隔的群組路徑，如 2F.CHS；沒有就省略")
    ap.add_argument("--insecure", action="store_true", help="略過 SSL 憑證驗證（僅此次探測）")
    args = ap.parse_args()

    password = args.password if args.password is not None else getpass.getpass("Gateway 密碼：")
    client = KepwareGatewayClient(args.url, verify=False if args.insecure else None)
    try:
        client.login(args.user, password)
    except Exception as e:
        print(f"登入失敗：{e}", file=sys.stderr)
        return 2

    try:
        data = client.get_tag(args.channel, args.device, args.tag, args.tag_group)
    except requests.HTTPError as e:
        r = e.response
        print(f"讀取失敗：HTTP {r.status_code if r is not None else '?'}", file=sys.stderr)
        if r is not None:
            print((r.text or "")[:1000], file=sys.stderr)
        return 3
    except Exception as e:
        print(f"讀取失敗：{e}", file=sys.stderr)
        return 3

    print("===== 原始回應（請完整複製貼給我）=====")
    print(json.dumps(data, ensure_ascii=False, indent=2))

    # 輔助：列出名稱含 type 的欄位，方便一眼看出資料型態叫什麼
    hints = []

    def walk(node, path=""):
        if isinstance(node, dict):
            for k, v in node.items():
                p = f"{path}.{k}" if path else str(k)
                if "type" in str(k).lower():
                    hints.append((p, v))
                walk(v, p)
        elif isinstance(node, list):
            for i, v in enumerate(node):
                walk(v, f"{path}[{i}]")

    walk(data)
    print("\n===== 名稱含 'type' 的欄位（資料型態通常在這裡）=====")
    for p, v in hints:
        print(f"  {p} = {v!r}")
    if not hints:
        print("  （沒有找到；請以上方原始回應為準）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
