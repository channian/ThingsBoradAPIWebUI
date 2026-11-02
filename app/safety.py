# -*- coding: utf-8 -*-
"""
安全鎖模組：限制單次批次作業的點數，以及單一 Gateway 的任務佇列深度。

背景：Step 3 建點 / Step 5 Scale / 批次刪除都是對 Kepware 的大量寫入，
為避免誤操作一次送出過大的批次，在啟動背景任務「之前」先做上限檢查。

設計原則
- 純邏輯：只依賴標準函式庫，不 import FastAPI / 資料庫 / app.main，
  可單獨測試，也方便之後由 main.py 在路由層接入。
- 上限來自環境變數（見 OPERATIONS 與 load_limits），非法值一律退回預設並記錄警告，
  絕不因設定錯誤而讓服務啟動失敗。
- 表驅動：操作名稱、中文顯示名、環境變數名、對應的 SafetyLimits 欄位集中在
  OPERATIONS 一張表，新增操作只需在表中加一列（若需要獨立的新上限值，另需在
  SafetyLimits 與 _FIELD_ENV_VARS 各加一個欄位）。

用法（由呼叫端決定如何把 SafetyError 轉成 HTTP 回應）::

    from app.safety import check_batch_size, check_queue_depth, SafetyError

    try:
        check_batch_size("kw_execute", len(rows))
        check_queue_depth(busy_tasks_of_this_gateway)
    except SafetyError as e:
        ...  # str(e) 為可直接顯示給使用者的繁體中文訊息；e.code 可區分原因
"""

from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass, fields
from typing import Mapping

log = logging.getLogger("safety")

# 錯誤代碼
CODE_BATCH_TOO_LARGE = "batch_too_large"
CODE_QUEUE_FULL = "queue_full"

# 佇列檢查沒有「操作」概念，SafetyError.operation 固定填這個值
QUEUE_OPERATION = "queue"


# ── 例外 ───────────────────────────────────────────────

class SafetyError(Exception):
    """安全鎖攔截時拋出的例外。

    屬性：
        operation: 操作代號（OPERATIONS 的鍵，如 "kw_execute"；佇列檢查為 "queue"）。
        limit: 該項上限。
        actual: 實際值（批次點數，或 Gateway 目前排隊中＋執行中的任務數）。
        code: "batch_too_large" 或 "queue_full"。

    str(e) 為繁體中文、可直接顯示給使用者的完整訊息（含補救方式）。
    """

    def __init__(self, operation: str, limit: int, actual: int, code: str, message: str) -> None:
        super().__init__(message)
        self.operation = operation
        self.limit = limit
        self.actual = actual
        self.code = code
        self.message = message


# ── 上限設定 ────────────────────────────────────────────

@dataclass(frozen=True)
class SafetyLimits:
    """各項安全上限（不可變）。

    欄位：
        kw_execute_max: Step 3 Kepware 建點，單次最多點數。
        scale_max: Step 5 Kepware Scale 設定，單次最多點數。
        delete_max: Kepware 批次刪除，單次最多點數。
        queue_max_per_gateway: 單一 Gateway 同時允許的「排隊中＋執行中」任務數。
    """

    kw_execute_max: int = 10000
    scale_max: int = 10000
    delete_max: int = 2000
    queue_max_per_gateway: int = 5


# SafetyLimits 欄位 → 讀取的環境變數名稱（load_limits 依此表讀取）
_FIELD_ENV_VARS: dict[str, str] = {
    "kw_execute_max": "KIS_LIMIT_KW_EXECUTE",
    "scale_max": "KIS_LIMIT_SCALE",
    "delete_max": "KIS_LIMIT_DELETE",
    "queue_max_per_gateway": "KIS_LIMIT_QUEUE_PER_GATEWAY",
}

# 只接受 ASCII 的十進位整數（可帶正負號）；排除 "1_000"、"１２３"、"1e3"、"10.0" 這類非整數寫法
_INT_PATTERN = re.compile(r"[+-]?[0-9]+")


def _parse_limit(env_name: str, raw: str | None, default: int) -> int:
    """把環境變數字串解析成正整數上限；非法時退回預設並記錄警告（絕不拋例外）。

    - 未設定（raw 為 None）：直接用預設，不警告。
    - 前後空白會被容忍。
    - 非整數、空字串或 <= 0：退回預設，並以 warning 記錄一行。
    """
    if raw is None:
        return default
    text = raw.strip() if isinstance(raw, str) else str(raw).strip()
    if _INT_PATTERN.fullmatch(text):
        value = int(text)
        if value > 0:
            return value
    log.warning("環境變數 %s=%r 不是有效的正整數，退回預設值 %d", env_name, raw, default)
    return default


def load_limits(env: Mapping[str, str] | None = None) -> SafetyLimits:
    """從環境變數讀取安全上限。

    參數：
        env: 環境變數對照；預設為 os.environ（呼叫當下才取用），測試可注入 dict。

    讀取的環境變數：KIS_LIMIT_KW_EXECUTE、KIS_LIMIT_SCALE、KIS_LIMIT_DELETE、
    KIS_LIMIT_QUEUE_PER_GATEWAY。任一項非整數或 <= 0 時，該項退回預設值並記錄警告，
    其他項不受影響；此函式永不因設定值而拋例外。
    """
    source: Mapping[str, str] = os.environ if env is None else env
    # 預設值直接取自 SafetyLimits 的欄位預設（f.default），避免兩處各寫一份
    values = {
        f.name: _parse_limit(_FIELD_ENV_VARS[f.name], source.get(_FIELD_ENV_VARS[f.name]), f.default)
        for f in fields(SafetyLimits)
    }
    return SafetyLimits(**values)


# ── 操作表（表驅動：新增操作只需在此加一列）─────────────────

@dataclass(frozen=True)
class OperationSpec:
    """單一批次操作的安全鎖設定。

    欄位：
        label: 繁體中文顯示名（用於錯誤訊息）。
        env_var: 控制該操作上限的環境變數名稱（用於錯誤訊息的補救提示）。
        limit_field: 對應的 SafetyLimits 欄位名稱。
    """

    label: str
    env_var: str
    limit_field: str


OPERATIONS: dict[str, OperationSpec] = {
    "kw_execute": OperationSpec("Kepware 建點", _FIELD_ENV_VARS["kw_execute_max"], "kw_execute_max"),
    "scale": OperationSpec("Kepware Scale 設定", _FIELD_ENV_VARS["scale_max"], "scale_max"),
    "delete": OperationSpec("Kepware 批次刪除", _FIELD_ENV_VARS["delete_max"], "delete_max"),
}


# ── 快取 ───────────────────────────────────────────────

_cached_limits: SafetyLimits | None = None


def get_limits() -> SafetyLimits:
    """取得目前生效的安全上限（模組層級快取）。

    第一次呼叫時才執行 load_limits() 並快取，之後直接回傳快取。
    併發下最壞情況只是重複載入一次（結果相同），故不加鎖。
    若要重讀環境變數，請呼叫 reload_limits()。
    """
    global _cached_limits
    limits = _cached_limits
    if limits is None:
        limits = _cached_limits = load_limits()
    return limits


def reload_limits() -> SafetyLimits:
    """清除快取並重新讀取環境變數，回傳新的上限（測試與未來熱更新用）。"""
    global _cached_limits
    _cached_limits = None
    return get_limits()


# ── 檢查 ───────────────────────────────────────────────

def check_batch_size(operation: str, count: int, limits: SafetyLimits | None = None) -> None:
    """檢查單次批次的點數是否在該操作的上限內。

    參數：
        operation: 操作代號，必須是 OPERATIONS 的鍵（"kw_execute" / "scale" / "delete"）。
        count: 本次批次要處理的點數。
        limits: 上限設定；省略時使用 get_limits()。

    行為：count > 上限 拋 SafetyError(code="batch_too_large")；count == 上限 允許。

    例外：
        ValueError: operation 未知，或 count < 0（屬呼叫端程式錯誤，非使用者操作問題）。
        SafetyError: 超過上限，str(e) 為可直接顯示給使用者的繁體中文訊息。
    """
    spec = OPERATIONS.get(operation)
    if spec is None:
        raise ValueError(f"未知的安全鎖操作：{operation!r}（可用：{', '.join(OPERATIONS)}）")
    if count < 0:
        raise ValueError(f"點數不可為負數：{count}")
    if limits is None:
        limits = get_limits()
    limit = getattr(limits, spec.limit_field)
    if count > limit:
        raise SafetyError(
            operation=operation,
            limit=limit,
            actual=count,
            code=CODE_BATCH_TOO_LARGE,
            message=(
                f"{spec.label}單次最多處理 {limit} 點，本次要求 {count} 點，已超過上限。"
                f"請分批處理（以 ids 縮小範圍），或由管理者調整環境變數 {spec.env_var}。"
            ),
        )


def check_queue_depth(depth: int, limits: SafetyLimits | None = None) -> None:
    """檢查單一 Gateway 的任務佇列是否已滿。

    參數：
        depth: 該 Gateway 目前「排隊中＋執行中」的任務數。
        limits: 上限設定；省略時使用 get_limits()。

    行為：depth >= queue_max_per_gateway 拋 SafetyError(code="queue_full")
    （再接一個任務就會超過上限，故等於上限時即視為已滿）。

    例外：
        ValueError: depth < 0（屬呼叫端程式錯誤）。
        SafetyError: 佇列已滿，str(e) 為可直接顯示給使用者的繁體中文訊息。
    """
    if depth < 0:
        raise ValueError(f"佇列深度不可為負數：{depth}")
    if limits is None:
        limits = get_limits()
    limit = limits.queue_max_per_gateway
    if depth >= limit:
        env_var = _FIELD_ENV_VARS["queue_max_per_gateway"]
        raise SafetyError(
            operation=QUEUE_OPERATION,
            limit=limit,
            actual=depth,
            code=CODE_QUEUE_FULL,
            message=(
                f"該 Gateway 的任務佇列已滿（排隊中＋執行中共 {depth} 個，上限 {limit} 個）。"
                f"請稍後再試，或取消排隊中的任務；若需提高上限，請由管理者調整環境變數 {env_var}。"
            ),
        )
