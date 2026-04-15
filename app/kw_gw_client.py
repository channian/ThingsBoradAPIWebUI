"""Kepware API Gateway Client

提供登入認證與 Kepware Tag Scaling 設定操作。
使用 Kepware 內建的 PUT /api/config/tags 修改 Tag 縮放設定。
"""
import os
import logging
import requests

log = logging.getLogger("kw_gw_client")


def _resolve_verify():
    """從環境變數決定 SSL 驗證方式

    優先順序:
      1. KW_GW_CA_BUNDLE=/path/to/ca.pem  → 使用自訂 CA 憑證
      2. KW_GW_VERIFY_SSL=false/0/no       → 停用驗證（自簽憑證用）
      3. 預設                               → 驗證（True）
    """
    ca_bundle = os.getenv("KW_GW_CA_BUNDLE", "").strip()
    if ca_bundle:
        return ca_bundle
    verify_env = os.getenv("KW_GW_VERIFY_SSL", "true").strip().lower()
    if verify_env in ("false", "0", "no", "off"):
        try:
            import urllib3
            urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
        except Exception:
            pass
        return False
    return True


def _resolve_proxies():
    """從環境變數決定是否透過 HTTP Proxy 呼叫 Kepware GW

    KW_GW_USE_PROXY=true/1/yes  → 回傳 None（讀取環境變數 HTTP_PROXY 等）
    其他（含預設）              → 回傳 {"http": None, "https": None}（繞過 proxy）
    """
    use_proxy = os.getenv("KW_GW_USE_PROXY", "false").strip().lower()
    if use_proxy in ("true", "1", "yes", "on"):
        return None
    return {"http": None, "https": None}


class KepwareGatewayClient:
    """Kepware API Gateway 連線封裝"""

    def __init__(self, base_url: str, verify=None, proxies=None):
        self.base_url = base_url.rstrip("/")
        self.token = None
        self.verify = _resolve_verify() if verify is None else verify
        self.proxies = _resolve_proxies() if proxies is None else proxies

    def login(self, username: str, password: str) -> str:
        """登入取得 Bearer Token"""
        resp = requests.post(
            f"{self.base_url}/api/auth/login",
            json={"username": username, "password": password},
            timeout=15,
            verify=self.verify,
            proxies=self.proxies,
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
            verify=self.verify,
            proxies=self.proxies,
        )
        resp.raise_for_status()
        return resp.json()
