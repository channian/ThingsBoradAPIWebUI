# -*- coding: utf-8 -*-
"""
ThingsBoard REST API Client
從原始腳本抽取共用的 API 呼叫邏輯
"""

import os
import requests
import logging

log = logging.getLogger("tb_client")


def _resolve_verify():
    """從環境變數決定 SSL 驗證方式

    優先順序:
      1. TB_CA_BUNDLE=/path/to/ca.pem  → 使用自訂 CA 憑證
      2. TB_VERIFY_SSL=false/0/no       → 停用驗證（自簽憑證用）
      3. 預設                            → 驗證（True）
    """
    ca_bundle = os.getenv("TB_CA_BUNDLE", "").strip()
    if ca_bundle:
        return ca_bundle
    verify_env = os.getenv("TB_VERIFY_SSL", "true").strip().lower()
    if verify_env in ("false", "0", "no", "off"):
        # 停用驗證時關閉 urllib3 警告，避免 log 洗版
        try:
            import urllib3
            urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
        except Exception:
            pass
        return False
    return True


def _resolve_proxies():
    """從環境變數決定是否透過 HTTP Proxy 呼叫 TB

    TB_USE_PROXY=true/1/yes  → 回傳 None（由 requests 讀取 HTTP_PROXY 等環境變數）
    其他（含預設）           → 回傳 {"http": None, "https": None}（繞過 proxy）

    內網 TB 通常不需要走 proxy，預設就繞過避免企業 proxy 連不到內網 IP 的問題。
    """
    use_proxy = os.getenv("TB_USE_PROXY", "false").strip().lower()
    if use_proxy in ("true", "1", "yes", "on"):
        return None
    return {"http": None, "https": None}


class ThingsBoardClient:
    """ThingsBoard REST API 封裝"""

    def __init__(self, base_url: str, verify=None, proxies=None):
        self.base_url = base_url.rstrip("/")
        self.token = None
        self.verify = _resolve_verify() if verify is None else verify
        self.proxies = _resolve_proxies() if proxies is None else proxies

    @property
    def _headers(self):
        h = {"X-Authorization": f"Bearer {self.token}"}
        if self.token:
            h["Content-Type"] = "application/json"
        return h

    # ── 認證 ──────────────────────────────────────────

    def login(self, username: str, password: str) -> str:
        """登入取得 JWT Token"""
        resp = requests.post(
            f"{self.base_url}/api/auth/login",
            json={"username": username, "password": password},
            timeout=10,
            verify=self.verify,
            proxies=self.proxies,
        )
        resp.raise_for_status()
        self.token = resp.json()["token"]
        return self.token

    def set_token(self, token: str):
        self.token = token

    # ── 裝置查詢 ──────────────────────────────────────

    def get_device_by_name(self, device_name: str):
        """依名稱查詢單一裝置，回傳裝置 JSON 或 None"""
        safe_name = requests.utils.quote(device_name)
        resp = requests.get(
            f"{self.base_url}/api/tenant/devices?deviceName={safe_name}",
            headers=self._headers,
            timeout=10,
            verify=self.verify,
            proxies=self.proxies,
        )
        if resp.status_code == 200:
            return resp.json()
        return None

    def get_devices_page(self, page: int = 0, page_size: int = 20,
                         text_search: str = None):
        """分頁查詢裝置列表"""
        params = {
            "pageSize": page_size,
            "page": page,
            "sortProperty": "name",
            "sortOrder": "ASC",
        }
        if text_search:
            params["textSearch"] = text_search
        resp = requests.get(
            f"{self.base_url}/api/tenant/devices",
            headers=self._headers,
            params=params,
            timeout=15,
            verify=self.verify,
            proxies=self.proxies,
        )
        resp.raise_for_status()
        return resp.json()

    # ── 裝置新增 ──────────────────────────────────────

    def create_device(self, payload: dict):
        """新增裝置，回傳 requests.Response"""
        return requests.post(
            f"{self.base_url}/api/device",
            headers=self._headers,
            json=payload,
            timeout=10,
            verify=self.verify,
            proxies=self.proxies,
        )

    # ── 裝置刪除 ──────────────────────────────────────

    def delete_device(self, device_id: str):
        """依 ID 刪除裝置，回傳 requests.Response"""
        return requests.delete(
            f"{self.base_url}/api/device/{device_id}",
            headers=self._headers,
            timeout=10,
            verify=self.verify,
            proxies=self.proxies,
        )

    # ── DeviceProfile ─────────────────────────────────

    def get_device_profiles(self, page: int = 0, page_size: int = 100,
                            text_search: str = None) -> dict:
        """分頁取得 DeviceProfile 列表"""
        params = {
            "pageSize": page_size,
            "page": page,
            "sortProperty": "name",
            "sortOrder": "ASC",
        }
        if text_search:
            params["textSearch"] = text_search
        resp = requests.get(
            f"{self.base_url}/api/deviceProfiles",
            headers=self._headers,
            params=params,
            timeout=15,
            verify=self.verify,
            proxies=self.proxies,
        )
        resp.raise_for_status()
        return resp.json()

    def get_all_device_profile_names(self) -> list:
        """取得所有 DeviceProfile 名稱列表"""
        names = []
        page = 0
        while True:
            result = self.get_device_profiles(page=page, page_size=100)
            for dp in result.get("data", []):
                names.append(dp.get("name", ""))
            if not result.get("hasNext", False):
                break
            page += 1
            if page > 50:  # 安全上限
                break
        return names
