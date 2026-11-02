# -*- coding: utf-8 -*-
"""
Gateway 自動路由：判斷一筆點位該送往哪一台 Kepware Gateway。

純邏輯模組——不依賴 DB、FastAPI 或任何 app.* 模組，輸入輸出皆為基本型別，
所有資料（結構快取索引、棟別/園區對應表）由呼叫端先查好再傳入。

判斷依據有二，兩者交叉檢查、不一致就擋下（絕不退回「預設 Gateway」，
那正是「靜默建錯地方」的來源）：
  1. channel 名稱：各 Gateway 的結構快取（kepware_structure）知道 channel 在哪台。
  2. 棟別/園區 → Gateway 對應表（gateway_route_map）：先查 site，再查 zone。

status 一覽：
  single           系統只有一台 Gateway，直接使用（旁路：不要求同步結構、不要求對應表）
  ok               channel 與對應表指向同一台
  channel_only     只有 channel 能判斷（對應表查無）
  map_only         只有對應表能判斷（channel 不在任何結構快取，例如尚未同步）
  conflict         channel 與對應表指向不同台 → 擋下
  conflict_channel channel 同時存在於多台 Gateway → 擋下
  unrouted         兩種依據都查不到 → 擋下
"""

import re
from dataclasses import dataclass
from typing import Optional


@dataclass(frozen=True)
class RouteResult:
    """路由判斷結果。gateway_id 為 None 代表無法路由（status 不在 ROUTABLE_STATUSES）。
    reason 為繁體中文、可直接顯示給使用者的說明。"""
    gateway_id: Optional[int]
    status: str
    reason: str


# 可以放行（執行時真的送出）的 status
ROUTABLE_STATUSES = frozenset({"single", "ok", "channel_only", "map_only"})


def _norm(value) -> str:
    """None → 空字串；其餘轉字串並 strip。空字串一律視為「沒有」。"""
    if value is None:
        return ""
    return str(value).strip()


def _lookup_map(site: str, zone: str, site_map: dict, zone_map: dict):
    """查對應表：site 層級比 zone 層級具體，先查 site，查不到才查 zone。
    回傳 (gateway_id 或 None, 依據說明)。"""
    if site:
        gw = (site_map or {}).get(site)
        if gw is not None:
            return gw, f"棟別 {site}"
    if zone:
        gw = (zone_map or {}).get(zone)
        if gw is not None:
            return gw, f"園區 {zone}"
    return None, ""


def resolve_gateway(channel, site, zone,
                    channel_index: dict, site_map: dict, zone_map: dict,
                    only_gateway_id: Optional[int] = None) -> RouteResult:
    """判斷一筆點位該送往哪台 Gateway。

    channel_index: {channel: [gateway_id, ...]}（來自各 Gateway 的結構快取）
    site_map / zone_map: {site: gateway_id} / {zone: gateway_id}（來自對應表）
    only_gateway_id: 系統只有一台 Gateway 時傳入該台 id → 一律 single 旁路
    比對皆為精確字串比對（先 strip；空字串/None 視為「沒有」）。
    """
    # 1. 單台旁路：行為與多 Gateway 改版前一致，不要求同步結構、不要求對應表
    if only_gateway_id is not None:
        return RouteResult(only_gateway_id, "single", "系統僅有 1 台 Gateway，直接使用")

    channel = _norm(channel)
    site = _norm(site)
    zone = _norm(zone)

    # dict.fromkeys 去重：同一台重複出現不算「多台」（build_channel_index 已去重，這裡防手工組的索引）
    ch_gws = list(dict.fromkeys((channel_index or {}).get(channel, []))) if channel else []
    map_gw, map_basis = _lookup_map(site, zone, site_map, zone_map)

    # 2. channel 同時存在於多台 Gateway
    if len(ch_gws) >= 2:
        ids = "、".join(f"Gateway {g}" for g in sorted(ch_gws))
        return RouteResult(
            None, "conflict_channel",
            f"channel {channel} 同時存在於 {len(ch_gws)} 台 Gateway（{ids}）",
        )

    # 3. channel 恰 1 台 且 對應表有值 → 交叉檢查
    if len(ch_gws) == 1 and map_gw is not None:
        ch_gw = ch_gws[0]
        if ch_gw == map_gw:
            return RouteResult(ch_gw, "ok", "結構快取與棟別/園區對應表一致")
        return RouteResult(
            None, "conflict",
            f"結構快取顯示在 Gateway {ch_gw}，但對應表指定 Gateway {map_gw}"
            f"（channel {channel}；對應依據：{map_basis}）",
        )

    # 4. 只有 channel 能判斷
    if len(ch_gws) == 1:
        return RouteResult(
            ch_gws[0], "channel_only",
            "依 channel 的結構快取判定；棟別/園區未設定對應，未做交叉檢查",
        )

    # 5. 只有對應表能判斷（channel 不在任何結構快取）
    if map_gw is not None:
        return RouteResult(
            map_gw, "map_only",
            f"channel 不在任何 Gateway 的結構快取，依對應表（{map_basis}）判定",
        )

    # 6. 皆無
    detail = f"（channel「{channel}」、棟別「{site}」、園區「{zone}」）"
    return RouteResult(None, "unrouted", "找不到 channel，且棟別/園區未設定對應" + detail)


def is_routable(result: RouteResult) -> bool:
    """該結果是否可放行（status 在 ROUTABLE_STATUSES 且 gateway_id 有值）。"""
    return result.status in ROUTABLE_STATUSES and result.gateway_id is not None


def group_by_gateway(items: list, results: list):
    """依路由結果把 items 分組。items 與 results 必須等長且一一對應。
    回傳 (groups, blocked)：
      groups  {gateway_id: [item, ...]}，可路由者依 gateway_id 分組，保持原順序
      blocked [(item, RouteResult), ...]，不可路由者，保持原順序
    """
    if len(items) != len(results):
        raise ValueError(
            f"items 與 results 長度不一致（{len(items)} vs {len(results)}）")
    groups: dict = {}
    blocked: list = []
    for item, res in zip(items, results):
        if is_routable(res):
            groups.setdefault(res.gateway_id, []).append(item)
        else:
            blocked.append((item, res))
    return groups, blocked


def build_channel_index(rows) -> dict:
    """rows: [(channel, gateway_id), ...] → {channel: [gateway_id, ...]}。
    同 channel 同 gateway 去重（保持首次出現順序）；channel 為空或 gateway_id 為 None 的略過。"""
    index: dict = {}
    for channel, gateway_id in rows:
        ch = _norm(channel)
        if not ch or gateway_id is None:
            continue
        gws = index.setdefault(ch, [])
        if gateway_id not in gws:
            gws.append(gateway_id)
    return index


def find_unmapped(locations: list, site_map: dict, zone_map: dict):
    """從 location_config 列出尚未被對應表涵蓋的棟別 / 園區，回傳 (sites, zones)，皆已去重排序。
      sites：該 site 自己沒有 site 對應，且其 zone 也沒有 zone 對應
             （與實際路由一致：同一 site 有多列時，採用最前面那列的 zone，同推導邏輯）
      zones：完全沒有 zone 對應的 zone
    locations: [{"site":..., "zone":...}, ...]（需依 id 排序，同 get_locations）
    """
    site_map = site_map or {}
    zone_map = zone_map or {}
    site_zone: dict = {}   # site -> 第一列的 zone
    all_zones: set = set()
    for loc in locations or []:
        site = _norm(loc.get("site"))
        zone = _norm(loc.get("zone"))
        if site and site not in site_zone:
            site_zone[site] = zone
        if zone:
            all_zones.add(zone)
    unmapped_sites = sorted(
        s for s, z in site_zone.items()
        if s not in site_map and not (z and z in zone_map)
    )
    unmapped_zones = sorted(z for z in all_zones if z not in zone_map)
    return unmapped_sites, unmapped_zones


_GW_REF = re.compile(r"Gateway (\d+)")


def localize_reason(reason: str, id_to_name: dict) -> str:
    """把 reason 中的「Gateway <id>」換成「Gateway「<名稱>」」，供上層顯示給使用者。
    查不到名稱的 id（例如該 Gateway 剛被刪除）保持原樣。"""
    def _sub(m):
        name = (id_to_name or {}).get(int(m.group(1)))
        return f"Gateway「{name}」" if name else m.group(0)
    return _GW_REF.sub(_sub, reason or "")
