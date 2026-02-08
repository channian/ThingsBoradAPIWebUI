# -*- coding: utf-8 -*-
"""
ThingsBoard REST API Client
從原始腳本抽取共用的 API 呼叫邏輯
"""

import requests
import logging

log = logging.getLogger("tb_client")


class ThingsBoardClient:
    """ThingsBoard REST API 封裝"""

    def __init__(self, base_url: str):
        self.base_url = base_url.rstrip("/")
        self.token = None

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
        )

    # ── 裝置刪除 ──────────────────────────────────────

    def delete_device(self, device_id: str):
        """依 ID 刪除裝置，回傳 requests.Response"""
        return requests.delete(
            f"{self.base_url}/api/device/{device_id}",
            headers=self._headers,
            timeout=10,
        )
