"""Kepware API Gateway Client

提供登入認證與 Scale 配置操作。
"""
import logging
import requests

log = logging.getLogger("kw_gw_client")


class KepwareGatewayClient:
    """Kepware API Gateway 連線封裝"""

    def __init__(self, base_url: str):
        self.base_url = base_url.rstrip("/")
        self.token = None

    def login(self, username: str, password: str) -> str:
        """登入取得 Bearer Token"""
        resp = requests.post(
            f"{self.base_url}/api/auth/login",
            json={"username": username, "password": password},
            timeout=15,
        )
        resp.raise_for_status()
        data = resp.json()
        self.token = data.get("access_token") or data.get("token")
        if not self.token:
            raise ValueError("登入回應中無 access_token")
        log.info(f"[KepwareGW] 登入成功: {self.base_url}")
        return self.token

    @property
    def _headers(self):
        if not self.token:
            raise RuntimeError("尚未登入，請先呼叫 login()")
        return {
            "Authorization": f"Bearer {self.token}",
            "Content-Type": "application/json",
        }

    def set_scale(self, config: dict) -> dict:
        """設定單筆 Tag 的 Scale 配置

        config 範例:
        {
            "tag_name": "Channel1.Device1.Temperature",
            "scale_type": "linear",
            "input_min": 0,
            "input_max": 65535,
            "output_min": 0.0,
            "output_max": 100.0,
            "clamp_low": true,
            "clamp_high": true,
            "unit": "℃"
        }
        """
        resp = requests.post(
            f"{self.base_url}/api/scale/set",
            headers=self._headers,
            json=config,
            timeout=15,
        )
        resp.raise_for_status()
        return resp.json()

    def get_scale(self, tag_name: str) -> dict:
        """查詢單筆 Tag 的 Scale 配置"""
        resp = requests.get(
            f"{self.base_url}/api/scale/get/{tag_name}",
            headers=self._headers,
            timeout=15,
        )
        resp.raise_for_status()
        return resp.json()

    def list_scales(self) -> dict:
        """列出所有 Scale 配置"""
        resp = requests.get(
            f"{self.base_url}/api/scale/list",
            headers=self._headers,
            timeout=15,
        )
        resp.raise_for_status()
        return resp.json()
