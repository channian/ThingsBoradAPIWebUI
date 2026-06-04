"""Kepware API Gateway Client

提供登入認證、Tag Group/Tag CRUD 與 Tag Scaling 設定操作。
透過 Kepware API Gateway（KepwareAPIWeb）REST 介面操作。
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

    def _request(self, method: str, path: str, **kwargs) -> requests.Response:
        """共用 HTTP 請求，自動帶 headers / verify / proxies"""
        resp = requests.request(
            method, f"{self.base_url}{path}",
            headers=self._headers, timeout=15,
            verify=self.verify, proxies=self.proxies,
            **kwargs,
        )
        return resp

    # ── Tag Group CRUD ──

    def create_tag_group(self, channel_name: str, device_name: str,
                         name: str, description: str = None,
                         parent_group: str = None) -> dict:
        """建立 Tag 群組（支援巢狀：指定 parent_group）"""
        payload = {
            "channel_name": channel_name,
            "device_name": device_name,
            "name": name,
        }
        if description:
            payload["description"] = description
        if parent_group:
            payload["parent_group"] = parent_group
        resp = requests.post(
            f"{self.base_url}/api/config/tag_groups",
            headers=self._headers, json=payload,
            timeout=15, verify=self.verify, proxies=self.proxies,
        )
        resp.raise_for_status()
        return resp.json()

    def ensure_tag_groups(self, channel_name: str, device_name: str,
                          group_path: str) -> None:
        """確保多層 tag group 路徑存在，不存在則逐層建立

        group_path 格式: "2F.CHS" → 先建 2F，再建 CHS (parent=2F)
        """
        if not group_path:
            return
        parts = group_path.split(".")
        for i, part in enumerate(parts):
            parent = ".".join(parts[:i]) if i > 0 else None
            try:
                self.create_tag_group(channel_name, device_name, part,
                                      parent_group=parent)
                log.info(f"[KepwareGW] 建立 tag group: {channel_name}/{device_name}/{'.'.join(parts[:i+1])}")
            except requests.HTTPError as e:
                if e.response is not None and e.response.status_code == 409:
                    pass  # 已存在，略過
                else:
                    raise

    # ── Tag CRUD ──

    def create_tag(self, channel_name: str, device_name: str,
                   tag_name: str, address: str = None,
                   data_type: int = 0, description: str = None,
                   tag_group: str = None) -> dict:
        """建立 Tag"""
        tag_obj = {"name": tag_name, "data_type": data_type}
        if address is not None:
            tag_obj["address"] = address
        if description is not None:
            tag_obj["description"] = description
        payload = {
            "channel_name": channel_name,
            "device_name": device_name,
            "tag": tag_obj,
        }
        if tag_group:
            payload["tag_group"] = tag_group
        resp = requests.post(
            f"{self.base_url}/api/config/tags",
            headers=self._headers, json=payload,
            timeout=15, verify=self.verify, proxies=self.proxies,
        )
        resp.raise_for_status()
        return resp.json()

    def delete_tag(self, channel_name: str, device_name: str,
                   tag_name: str, tag_group: str = None) -> dict:
        """刪除 Tag"""
        payload = {
            "channel_name": channel_name,
            "device_name": device_name,
            "tag_name": tag_name,
        }
        if tag_group:
            payload["tag_group"] = tag_group
        resp = requests.delete(
            f"{self.base_url}/api/config/tags",
            headers=self._headers, json=payload,
            timeout=15, verify=self.verify, proxies=self.proxies,
        )
        resp.raise_for_status()
        return resp.json()

    # ── Tag Scaling ──

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
