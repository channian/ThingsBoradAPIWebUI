# -*- coding: utf-8 -*-
"""
設定管理器
管理 config.json 的讀寫：下拉選單選項、預設值、映射規則
"""

import json
import os
import logging
from typing import Any

log = logging.getLogger("config_manager")

# 預設設定模板
DEFAULT_CONFIG = {
    "dropdown_options": {
        "driver_type": ["OPC", "Modbus", "BACnet"],
        "node_name": [],
        "zone": ["Zone1", "Zone2"],
        "bu": ["East", "West"],
        "site": [],
        "floor": [],
        "owner": [],
        "department": [],
        "data_type": ["Boolean", "Word", "DWord", "Float"],
        "system": [],
        "tabname": [],
        "memory_type": [],
    },
    "defaults": {
        "driver_type": "OPC",
        "site": "",
        "data_type": "",
    },
    "mapping_rules": {
        "zone_prefix": {
            "K0": "Zone1",
            "K1": "Zone1",
            "K2": "Zone2",
        },
        "bu_prefix": {
            "K1": "East",
            "K2": "East",
            "K3": "East",
            "K4": "East",
            "K11": "East",
            "K5": "West",
            "K7": "West",
            "K8": "West",
            "K12": "West",
            "K13B": "West",
        },
        "label_prefix": {
            "OPC": "ns=2;s=",
        },
    },
}


class ConfigManager:
    """config.json CRUD 管理器"""

    def __init__(self, config_path: str):
        self.config_path = config_path
        self._ensure_config()

    def _ensure_config(self):
        """確保 config.json 存在，不存在則建立預設"""
        if not os.path.exists(self.config_path):
            self._save(DEFAULT_CONFIG)
            log.info(f"已建立預設設定檔: {self.config_path}")

    def _load(self) -> dict:
        try:
            with open(self.config_path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            log.error(f"讀取設定檔失敗: {e}")
            return json.loads(json.dumps(DEFAULT_CONFIG))

    def _save(self, config: dict):
        os.makedirs(os.path.dirname(self.config_path), exist_ok=True)
        with open(self.config_path, "w", encoding="utf-8") as f:
            json.dump(config, f, ensure_ascii=False, indent=2)

    # ── 完整設定 ──────────────────────────────────────

    def get_all(self) -> dict:
        return self._load()

    # ── 下拉選項 CRUD ─────────────────────────────────

    def get_dropdown_options(self) -> dict:
        return self._load().get("dropdown_options", {})

    def get_dropdown_field(self, field: str) -> list:
        return self._load().get("dropdown_options", {}).get(field, [])

    def set_dropdown_field(self, field: str, values: list):
        config = self._load()
        config.setdefault("dropdown_options", {})[field] = values
        self._save(config)

    def add_dropdown_value(self, field: str, value: str) -> bool:
        config = self._load()
        options = config.setdefault("dropdown_options", {}).setdefault(field, [])
        if value in options:
            return False
        options.append(value)
        self._save(config)
        return True

    def remove_dropdown_value(self, field: str, value: str) -> bool:
        config = self._load()
        options = config.get("dropdown_options", {}).get(field, [])
        if value not in options:
            return False
        options.remove(value)
        self._save(config)
        return True

    def delete_dropdown_field(self, field: str) -> bool:
        config = self._load()
        if field in config.get("dropdown_options", {}):
            del config["dropdown_options"][field]
            self._save(config)
            return True
        return False

    # ── 預設值 ────────────────────────────────────────

    def get_defaults(self) -> dict:
        return self._load().get("defaults", {})

    def set_defaults(self, defaults: dict):
        config = self._load()
        config["defaults"] = defaults
        self._save(config)

    def set_default_value(self, field: str, value: Any):
        config = self._load()
        config.setdefault("defaults", {})[field] = value
        self._save(config)

    # ── 映射規則 ──────────────────────────────────────

    def get_mapping_rules(self) -> dict:
        return self._load().get("mapping_rules", {})

    def get_mapping_rule(self, rule_name: str) -> dict:
        return self._load().get("mapping_rules", {}).get(rule_name, {})

    def set_mapping_rule(self, rule_name: str, mapping: dict):
        config = self._load()
        config.setdefault("mapping_rules", {})[rule_name] = mapping
        self._save(config)

    def delete_mapping_rule(self, rule_name: str) -> bool:
        config = self._load()
        if rule_name in config.get("mapping_rules", {}):
            del config["mapping_rules"][rule_name]
            self._save(config)
            return True
        return False
