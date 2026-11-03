# -*- coding: utf-8 -*-
"""
Kepware Tag「資料型態（data_type）」的純邏輯工具（只用標準函式庫，不依賴 FastAPI / DB / requests）。

用於「data_type 校正」功能：讀取 Kepware 上已建立的 Tag，取出它目前的資料型態，
與系統推導的預期值比對；校正（PUT）之後再讀回，確認「只有 data_type 變了」。

★ 重要：Gateway 的「讀取單一 Tag」回應 schema，文件只寫 any，**欄位名稱至今未經真實回應確認**。
  這裡的 extract_tag_data_type 是「依文件與常見命名推測」的寬鬆解析，設計原則是**寧可判不出來也絕不亂猜**：
  找不到、有歧義、值怪異一律回 None，由呼叫端當成失敗處理（不寫入）。
  日後拿到真實回應（scripts/kw_probe_tag.py）若欄位名稱不同，只需調整本檔的
  DATA_TYPE_KEYS / WRAPPER_KEYS 這兩個常數，其他地方不用動。

data_type 數值（Kepware 文件）：
    1=Boolean 2=Char 3=Byte 4=Short 5=Word 6=Long 7=DWord 8=Float 9=Double
    10=String 11=BCD 12=LBCD 13=Date 14=LLong 15=QWord；0 視為「未指定」（舊版程式曾把點位建成 0）。
"""

from __future__ import annotations

import re
from typing import Any, Iterable, Optional

# ── 資料型態代碼與名稱 ─────────────────────────────────────

DATA_TYPE_NAMES: dict[int, str] = {
    0: "未指定(0)",
    1: "Boolean",
    2: "Char",
    3: "Byte",
    4: "Short",
    5: "Word",
    6: "Long",
    7: "DWord",
    8: "Float",
    9: "Double",
    10: "String",
    11: "BCD",
    12: "LBCD",
    13: "Date",
    14: "LLong",
    15: "QWord",
}

# 可以「寫入」Kepware 的型態範圍：0（未指定）是要被校正掉的狀態，不是校正目標
WRITABLE_MIN = 1
WRITABLE_MAX = 15


def is_writable_data_type(value: Any) -> bool:
    """value 是否為可寫入 Kepware 的資料型態代碼（int、非 bool、1–15）。"""
    return (isinstance(value, int) and not isinstance(value, bool)
            and WRITABLE_MIN <= value <= WRITABLE_MAX)


def data_type_label(code: Any) -> str:
    """代碼 → 顯示名稱。None → "—"；未知（含 bool、非整數、超出 0–15）→ "未知(<code>)"。"""
    if code is None:
        return "—"
    if isinstance(code, int) and not isinstance(code, bool) and code in DATA_TYPE_NAMES:
        return DATA_TYPE_NAMES[code]
    return f"未知({code})"


# ── 從「讀取 Tag」回應取出資料型態（依文件推測、待真實回應確認）──────────

# 候選鍵（比對不分大小寫）。名稱全為推測：Gateway 文件未列出回應欄位。
DATA_TYPE_KEYS: tuple[str, ...] = (
    "data_type", "dataType", "datatype", "tag_data_type", "servermain.TAG_DATA_TYPE",
)
# 常見的包裝層：回應的第一層，加上這些鍵（值為 dict）底下的一層，才會去找候選鍵；不會更深。
WRAPPER_KEYS: tuple[str, ...] = ("tag", "data", "result", "definition", "properties")

_DATA_TYPE_KEYS_LOWER = frozenset(k.lower() for k in DATA_TYPE_KEYS)
_WRAPPER_KEYS_LOWER = frozenset(k.lower() for k in WRAPPER_KEYS)

# 型態名稱（不分大小寫）→ 代碼；"Default" 是 Kepware 對「未指定」的稱呼
_NAME_TO_CODE: dict[str, int] = {name.casefold(): code for code, name in DATA_TYPE_NAMES.items()}
_NAME_TO_CODE["default"] = 0

_DIGITS = re.compile(r"[0-9]+")        # 只收 ASCII 數字（排除全形數字、上標等 str.isdigit 會放行的字元）


def _parse_data_type_value(value: Any) -> Optional[int]:
    """單一值 → 代碼 0–15，無法確定回 None。bool 不是數字（True 不可變成 1）；float、None、容器一律無法辨識。"""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        code = value
    elif isinstance(value, str):
        text = value.strip()
        if _DIGITS.fullmatch(text):
            code = int(text)
        else:
            code = _NAME_TO_CODE.get(text.casefold())
            if code is None:
                return None
    else:
        return None
    return code if 0 <= code <= 15 else None


def extract_tag_data_type(payload: Any) -> Optional[int]:
    """從「讀取單一 Tag」的回應取出資料型態代碼（0–15）；無法確定一律回 None。

    ★ 依文件與常見命名推測，欄位名稱待真實 Gateway 回應確認（見模組說明）。

    找法：在回應第一層，以及 tag／data／result／definition／properties 這幾個鍵（值為 dict）
    底下的一層，找候選鍵 data_type／dataType／datatype／tag_data_type／servermain.TAG_DATA_TYPE
    （鍵名不分大小寫）。值可為整數、數字字串，或型態名稱字串（"Float" 等，不分大小寫；"Default" 視為 0）。

    回 None 的情況（呼叫端必須當成「無法判讀」處理，不可猜、不可寫入）：
      - payload 不是 dict、找不到任何候選鍵；
      - 任何一個找到的候選值無法辨識（含 bool、float、None、超出 0–15 的數字、不認識的名稱）；
      - 找到多個候選，但數值不一致（有歧義）。
    """
    if not isinstance(payload, dict):
        return None
    layers = [payload]
    for key, value in payload.items():
        if isinstance(key, str) and key.lower() in _WRAPPER_KEYS_LOWER and isinstance(value, dict):
            layers.append(value)
    found = [value for layer in layers for key, value in layer.items()
             if isinstance(key, str) and key.lower() in _DATA_TYPE_KEYS_LOWER]
    if not found:
        return None
    parsed = [_parse_data_type_value(v) for v in found]
    if any(code is None for code in parsed):
        return None
    if len(set(parsed)) != 1:
        return None
    return parsed[0]


def describe_payload_keys(payload: Any, max_len: int = 200) -> str:
    """列出回應前兩層的鍵名（只有鍵名、不含值），給「無法判讀」的錯誤訊息用；長度截斷到 max_len 字。

    例：``name, data_type, tag{name,address}``（第二層以大括號附在第一層鍵名後面）。"""
    if not isinstance(payload, dict):
        text = f"(非物件：{type(payload).__name__})"
    elif not payload:
        text = "(空物件)"
    else:
        parts = []
        for key, value in payload.items():
            if isinstance(value, dict) and value:
                parts.append(f"{key}{{{','.join(str(k) for k in value)}}}")
            else:
                parts.append(str(key))
        text = ", ".join(parts)
    if len(text) > max_len:
        text = text[:max_len - 1] + "…"
    return text


# ── 寫入後比對：PUT 有沒有動到 data_type 以外的欄位 ─────────────────

def _same(a: Any, b: Any) -> bool:
    """嚴格相等：型別不同就不相等（避免 1 == True、1 == 1.0 把變動吃掉）；dict / list 遞迴比較。"""
    if type(a) is not type(b):
        return False
    if isinstance(a, dict):
        return a.keys() == b.keys() and all(_same(a[k], b[k]) for k in a)
    if isinstance(a, list):
        return len(a) == len(b) and all(_same(x, y) for x, y in zip(a, b))
    return a == b


def _diff(before: Any, after: Any, path: str, allowed: frozenset, out: list) -> None:
    if isinstance(before, dict) and isinstance(after, dict):
        for key in sorted(set(before) | set(after), key=str):
            if str(key).lower() in allowed:
                continue
            child = f"{path}.{key}" if path else str(key)
            if key not in before or key not in after:
                out.append(child)                       # 新增或消失的鍵
            else:
                _diff(before[key], after[key], child, allowed, out)
        return
    if not _same(before, after):
        out.append(path or "<root>")


def diff_tag_payloads(before: Any, after: Any,
                      allowed: Iterable[str] = DATA_TYPE_KEYS) -> list[str]:
    """遞迴比較兩份「讀取 Tag」回應，回傳除了 allowed 鍵之外，值有變動／新增／消失的鍵路徑（排序後）。

    路徑以點串接，如 ``scaling_type``、``nested.address``。allowed 以「鍵名」比對（不分大小寫、任何層級），
    命中的鍵整棵子樹都不比較。list 視為單一值（有差異就回報該 list 所在路徑，不逐項展開）。
    用於寫入後檢查「PUT 有沒有影響到 data_type 以外的欄位」：回傳空 list 才算乾淨。"""
    allowed_lower = frozenset(str(a).lower() for a in allowed)
    out: list[str] = []
    _diff(before, after, "", allowed_lower, out)
    return sorted(out)
