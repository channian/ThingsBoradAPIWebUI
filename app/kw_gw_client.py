"""Kepware API Gateway Client

提供登入認證與 Kepware Tag Scaling 設定操作。
使用 Kepware 內建的 PUT /api/config/tags 修改 Tag 縮放設定。
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

    def set_tag_scaling(self, config: dict) -> dict:
        """設定 Kepware Tag 的內建 Scaling

        config 範例:
        {
            "channel_name": "Channel1",
            "device_name": "Device1",
            "tag_name": "Temperature",
            "scaling_type": 1,              # 0=None, 1=Linear, 2=Square Root
            "scaling_raw_low": 0,
            "scaling_raw_high": 65535,
            "scaling_scaled_low": 0.0,
            "scaling_scaled_high": 100.0,
            "scaling_clamp_low": True,
            "scaling_clamp_high": True,
            "scaling_units": "℃",
        }
        """
        resp = requests.put(
            f"{self.base_url}/api/config/tags",
            headers=self._headers,
            json=config,
            timeout=15,
        )
        resp.raise_for_status()
        return resp.json()
