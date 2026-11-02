# -*- coding: utf-8 -*-
"""
Gateway 作業佇列：同一台 Gateway 的作業排隊（先進先出），不同台的作業彼此並行。

背景：Kepware 的限速是「每台 Gateway 各自計算」，所以同一台一次只能跑一個作業
（否則速率疊加會撞限速），不同台則互不影響。舊作法是同一台忙碌時直接回 409；
現在改為排隊，並允許取消排隊中（或執行中）的作業。

設計
- 純標準函式庫：不 import FastAPI / 資料庫 / app.main，可獨立測試。
- 每個 key（見 gateway_key）一條 FIFO 佇列，**佇列頭＝正在執行（持有該 Gateway）**，
  其後都是排隊中；一個 threading.Condition 保護全部狀態。
- 呼叫端流程（由背景任務層 app/gateway_jobs.py 代勞）::

      ahead = queue.enqueue_many(keys, task_id, max_depth)   # 在請求處理函式內同步完成
      ...                                                    # 之後在背景執行緒：
      try:
          if queue.wait_turn(key, task_id, cancelled=lambda: task.cancelled):
              ...執行...
      finally:
          queue.release(key, task_id)                        # 冪等，一定要放 finally

  enqueue 必須在請求處理期間（回應 task_id 之前）同步完成，排隊順序才會等於請求到達順序，
  且「佇列深度檢查＋加入」是單一原子操作（不會兩個請求同時通過檢查而超過上限）。
- 取消：呼叫端先把 task.cancelled 設為 True，再呼叫 wake()，排隊中的等待者會立刻
  重新檢查 cancelled() 並自佇列移除自己，不必等下一次輪詢。

執行緒安全：所有公開方法皆可由任意執行緒呼叫；沒有任何持鎖睡眠
（等待一律用 Condition.wait，等待期間會釋放鎖）。
"""

from __future__ import annotations

import re
import threading
from collections import deque
from typing import Callable, Dict, List, Optional


# ── 例外 ───────────────────────────────────────────────

class QueueFullError(Exception):
    """某個 Gateway 的佇列已達上限，整批 enqueue_many 都不會加入。

    屬性：
        key: 已滿的佇列 key。
        depth: 當下該佇列長度（排隊中＋執行中）。
        limit: 上限（max_depth）。
    """

    def __init__(self, key: str, depth: int, limit: int) -> None:
        super().__init__(
            f"Gateway 佇列已滿：{key} 目前有 {depth} 個作業（排隊中＋執行中），上限 {limit} 個"
        )
        self.key = key
        self.depth = depth
        self.limit = limit


# ── Gateway key ────────────────────────────────────────

# scheme://authority + 其餘（path / query / fragment）
_URL_WITH_SCHEME = re.compile(r"^([A-Za-z][A-Za-z0-9+.\-]*)://([^/?#]*)(.*)$", re.DOTALL)


def _lower_host(authority: str) -> str:
    """authority 可能是 [userinfo@]host[:port]；只把 host[:port] 轉小寫，帳密部分保持原樣。"""
    userinfo, at, hostport = authority.rpartition("@")
    return f"{userinfo}{at}{hostport.lower()}"


def normalize_gateway_url(url: Optional[str]) -> str:
    """把 Gateway URL 正規化成可比較的字串（用於沒有 gateway_id 的手動憑證）。

    規則：去前後空白 → 去尾端所有 "/" → scheme 與 host 轉小寫（path / query 保持原樣，
    有 userinfo 時帳密也保持原樣）。None 或空字串回 ""。
    沒有 scheme 的輸入（如 "Host:8080/Api"）則把第一個 "/" 之前當成 host 轉小寫。
    這讓 "http://GW:8080/" 與 "http://gw:8080" 共用同一個佇列 key，
    不會因為多打一個斜線就變成另一把鎖。
    """
    if url is None:
        return ""
    text = str(url).strip().rstrip("/")
    if not text:
        return ""
    m = _URL_WITH_SCHEME.match(text)
    if m:
        scheme, authority, rest = m.groups()
        return f"{scheme.lower()}://{_lower_host(authority)}{rest}"
    head, slash, rest = text.partition("/")
    return f"{_lower_host(head)}{slash}{rest}"


def gateway_key(gateway_id, gw_url) -> str:
    """佇列 key：有 gateway_id（truthy）用 ``gw:<id>``，否則用正規化後的 URL ``url:<url>``。"""
    if gateway_id:
        return f"gw:{gateway_id}"
    return f"url:{normalize_gateway_url(gw_url)}"


# ── 佇列 ───────────────────────────────────────────────

def _never_cancelled() -> bool:
    return False


class GatewayQueue:
    """每個 key 一條 FIFO 的作業佇列；佇列頭＝執行中，其後＝排隊中。"""

    def __init__(self) -> None:
        self._cond = threading.Condition()
        self._queues: Dict[str, deque] = {}     # key -> deque[task_id]（頭＝執行中）

    # ── 寫入 ──

    def enqueue_many(self, keys: List[str], task_id: str,
                     max_depth: Optional[int] = None) -> Dict[str, int]:
        """原子地把 task_id 加入多個 key 的佇列尾端。

        - keys 內重複的 key 只加入一次。
        - 先檢查全部：任一 key 的目前長度 >= max_depth（max_depth 為 None 則不檢查）就拋
          QueueFullError（帶該 key、目前長度、上限），且**一個都不加入**。
        - 同一 (key, task_id) 已在佇列中 → ValueError（同樣不加入任何一個）。
        - 回傳 {key: ahead}，ahead＝加入當下排在它前面的作業數（0＝輪到就立刻執行）。
        """
        unique_keys = list(dict.fromkeys(keys))
        with self._cond:
            for key in unique_keys:
                dq = self._queues.get(key)
                if dq is not None and task_id in dq:
                    raise ValueError(f"作業 {task_id} 已在 {key} 的佇列中，不可重複加入")
            if max_depth is not None:
                for key in unique_keys:
                    depth = len(self._queues.get(key, ()))
                    if depth >= max_depth:
                        raise QueueFullError(key, depth, max_depth)
            ahead: Dict[str, int] = {}
            for key in unique_keys:
                dq = self._queues.setdefault(key, deque())
                ahead[key] = len(dq)
                dq.append(task_id)
            return ahead

    def release(self, key: str, task_id: str) -> None:
        """自 key 的佇列移除 task_id（不論執行中或排隊中），並喚醒所有等待者。

        冪等：不在佇列時什麼都不做。只有 task_id 吻合才會動到項目（防止誤放他人）。
        佇列空了就從內部字典移除，避免 key 洩漏。
        """
        with self._cond:
            if self._remove_locked(key, task_id):
                self._cond.notify_all()

    def wake(self) -> None:
        """喚醒所有等待者重新檢查 cancelled()（取消端點在設定 task.cancelled 之後呼叫）。"""
        with self._cond:
            self._cond.notify_all()

    # ── 等待 ──

    def wait_turn(self, key: str, task_id: str,
                  cancelled: Optional[Callable[[], bool]] = _never_cancelled,
                  poll: float = 0.25) -> bool:
        """阻塞到 task_id 成為 key 佇列的頭，回 True（呼叫端此時持有該 Gateway，用完要 release）。

        回 False 的情況（此時 task_id 已不在佇列，不需再 release，但 release 是冪等的，多呼叫無害）：
        - cancelled() 為真：把自己的項目移除（並喚醒其他等待者）；
        - (key, task_id) 根本不在佇列（例如已被 release）。
        即使已是佇列頭，也會先檢查 cancelled()：已取消就移除並回 False。

        cancelled 是在持有內部鎖的狀態下呼叫的（務必輕量、不可阻塞）。這是刻意的：
        「檢查 cancelled → 進入等待」對 wake() 而言是原子的，取消端「先設旗標、再 wake()」
        就不會因為搶在等待者進入 wait 之前而漏掉喚醒。
        等待期間每 poll 秒（以及被 release / wake 喚醒時）重新檢查；poll 應 > 0。
        """
        with self._cond:
            while True:
                dq = self._queues.get(key)
                if dq is None or task_id not in dq:
                    return False
                if cancelled is not None and cancelled():
                    self._remove_locked(key, task_id)
                    self._cond.notify_all()
                    return False
                if dq[0] == task_id:
                    return True
                self._cond.wait(timeout=poll)

    # ── 查詢 ──

    def depth(self, key: str) -> int:
        """佇列長度（排隊中＋執行中）。"""
        with self._cond:
            return len(self._queues.get(key, ()))

    def position(self, key: str, task_id: str) -> Optional[int]:
        """0＝頭（執行中）；n＝前面還有 n 個；不在佇列回 None。"""
        with self._cond:
            dq = self._queues.get(key)
            if dq is None:
                return None
            for idx, tid in enumerate(dq):
                if tid == task_id:
                    return idx
            return None

    def holder(self, key: str) -> Optional[str]:
        """目前持有（正在執行）該 Gateway 的 task_id；佇列空回 None。"""
        with self._cond:
            dq = self._queues.get(key)
            return dq[0] if dq else None

    def snapshot(self) -> Dict[str, List[str]]:
        """{key: [task_id, ...]} 的複本（頭在最前）；修改回傳值不會影響佇列。"""
        with self._cond:
            return {key: list(dq) for key, dq in self._queues.items()}

    # ── 內部 ──

    def _remove_locked(self, key: str, task_id: str) -> bool:
        """（須持有 self._cond）移除 (key, task_id)；佇列空了就刪 key。回傳是否真的移除了。"""
        dq = self._queues.get(key)
        if dq is None:
            return False
        try:
            dq.remove(task_id)
        except ValueError:
            return False
        if not dq:
            del self._queues[key]
        return True
