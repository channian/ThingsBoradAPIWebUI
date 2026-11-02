# -*- coding: utf-8 -*-
"""Gateway 自動路由純邏輯（app/gateway_router.py）：不需要 DB。

涵蓋 resolve_gateway 全部 status 分支與判斷順序、site 優先於 zone、
單台旁路、空值/空白處理、group_by_gateway、build_channel_index、
find_unmapped、localize_reason。
"""
import dataclasses

import pytest

from app.gateway_router import (
    ROUTABLE_STATUSES,
    RouteResult,
    build_channel_index,
    find_unmapped,
    group_by_gateway,
    is_routable,
    localize_reason,
    resolve_gateway,
)


def _resolve(channel="CHA", site="S1", zone="Z1", channel_index=None,
             site_map=None, zone_map=None, only=None):
    return resolve_gateway(channel, site, zone,
                           channel_index or {}, site_map or {}, zone_map or {}, only)


# ── 各 status 分支 ───────────────────────────────────────────────

def test_ok_when_channel_and_map_agree():
    r = _resolve(channel_index={"CHA": [1]}, site_map={"S1": 1})
    assert (r.status, r.gateway_id) == ("ok", 1)


def test_conflict_when_channel_and_map_disagree():
    r = _resolve(channel_index={"CHA": [1]}, site_map={"S1": 2})
    assert r.status == "conflict"
    assert r.gateway_id is None
    # reason 要寫明 channel 指向哪台、對應表指向哪台（用 gateway_id 數字，上層再翻成名稱）
    assert "Gateway 1" in r.reason and "Gateway 2" in r.reason
    assert r.reason.index("Gateway 1") < r.reason.index("Gateway 2"), "先寫 channel（結構快取）再寫對應表"
    assert not is_routable(r)


def test_conflict_channel_when_channel_in_multiple_gateways():
    r = _resolve(channel_index={"CHA": [1, 2]}, site_map={"S1": 1})
    assert r.status == "conflict_channel"
    assert r.gateway_id is None
    assert "CHA" in r.reason and "2 台" in r.reason


def test_conflict_channel_beats_everything_but_single():
    """channel 同時存在多台時，不論對應表怎麼說都擋下（不能拿對應表去「挑一台」）。"""
    for site_map in ({}, {"S1": 1}, {"S1": 2}, {"S1": 3}):
        r = _resolve(channel_index={"CHA": [1, 2]}, site_map=site_map)
        assert r.status == "conflict_channel", site_map


def test_channel_only_when_no_map():
    r = _resolve(channel_index={"CHA": [2]})
    assert (r.status, r.gateway_id) == ("channel_only", 2)
    assert is_routable(r)


def test_map_only_when_channel_not_in_any_structure():
    r = _resolve(channel="CNEW", channel_index={"CHA": [1]}, site_map={"S1": 2})
    assert (r.status, r.gateway_id) == ("map_only", 2)
    assert is_routable(r)


def test_unrouted_when_nothing_known():
    r = _resolve(channel="CNEW", channel_index={"CHA": [1]})
    assert (r.status, r.gateway_id) == ("unrouted", None)
    assert "找不到 channel" in r.reason and "未設定對應" in r.reason
    assert not is_routable(r)


def test_reasons_are_non_empty_chinese_for_every_status():
    cases = [
        _resolve(only=7),
        _resolve(channel_index={"CHA": [1]}, site_map={"S1": 1}),
        _resolve(channel_index={"CHA": [1]}, site_map={"S1": 2}),
        _resolve(channel_index={"CHA": [1, 2]}),
        _resolve(channel_index={"CHA": [1]}),
        _resolve(channel="CNEW", site_map={"S1": 1}),
        _resolve(channel="CNEW"),
    ]
    assert {c.status for c in cases} == {
        "single", "ok", "conflict", "conflict_channel", "channel_only", "map_only", "unrouted"}
    for c in cases:
        assert c.reason.strip()
        assert any("一" <= ch <= "鿿" for ch in c.reason), c


# ── 單台旁路 ─────────────────────────────────────────────────────

def test_single_gateway_bypass_ignores_everything_else():
    # 沒結構、沒對應表 → 照樣放行
    r = _resolve(channel="WHATEVER", only=5)
    assert (r.status, r.gateway_id) == ("single", 5)
    assert is_routable(r)
    # 即使資料「看起來衝突」也不理會（行為與改版前一致）
    r = _resolve(channel_index={"CHA": [1, 2]}, site_map={"S1": 9}, only=5)
    assert (r.status, r.gateway_id) == ("single", 5)


def test_single_bypass_uses_is_not_none_check():
    """only_gateway_id 為 None 才不旁路；任何非 None 值（含 0）都要旁路。"""
    assert _resolve(only=0).status == "single"
    assert _resolve(only=None).status == "unrouted"


# ── 判斷優先順序 ─────────────────────────────────────────────────

def test_site_map_takes_priority_over_zone_map():
    # site 指向 1、zone 指向 2：以 site 為準
    r = _resolve(channel_index={"CHA": [1]}, site_map={"S1": 1}, zone_map={"Z1": 2})
    assert (r.status, r.gateway_id) == ("ok", 1)
    # 反過來：site 指向 2（與 channel 衝突）、zone 指向 1（與 channel 一致）→ 仍以 site 為準 → conflict
    r = _resolve(channel_index={"CHA": [1]}, site_map={"S1": 2}, zone_map={"Z1": 1})
    assert r.status == "conflict"
    # map_only 時同樣 site 優先
    r = _resolve(channel="CNEW", site_map={"S1": 1}, zone_map={"Z1": 2})
    assert (r.status, r.gateway_id) == ("map_only", 1)


def test_falls_back_to_zone_map_when_site_not_mapped():
    r = _resolve(channel="CNEW", site="S9", zone_map={"Z1": 2})
    assert (r.status, r.gateway_id) == ("map_only", 2)
    r = _resolve(channel_index={"CHA": [2]}, site="S9", zone_map={"Z1": 2})
    assert (r.status, r.gateway_id) == ("ok", 2)


def test_channel_exists_in_one_gateway_beats_map_for_choice_but_must_agree():
    """channel 恰 1 台時，對應表只用來交叉檢查，不能改變去向。"""
    r = _resolve(channel_index={"CHA": [1]}, site_map={"S1": 2})
    assert r.gateway_id is None  # 不是 2


# ── 空值 / 空白 / 精確比對 ───────────────────────────────────────

def test_strip_before_matching():
    r = resolve_gateway("  CHA ", " S1 ", " Z1 ", {"CHA": [1]}, {"S1": 1}, {}, None)
    assert (r.status, r.gateway_id) == ("ok", 1)


def test_empty_none_and_blank_are_treated_as_missing():
    # channel 為 None / 空 / 空白 → 當作沒有 channel；maps/index 裡即使有 "" 這個 key 也不能命中
    idx = {"": [1], "CHA": [1]}
    smap = {"": 2}
    zmap = {"": 3}
    for ch in (None, "", "   "):
        r = resolve_gateway(ch, "", "", idx, smap, zmap, None)
        assert r.status == "unrouted", (ch, r)
    for site in (None, "", "  "):
        r = resolve_gateway("CNEW", site, None, {}, smap, zmap, None)
        assert r.status == "unrouted", (site, r)
    # site 空白但 zone 有對應 → 用 zone
    r = resolve_gateway("CNEW", "  ", "Z1", {}, {"S1": 1}, {"Z1": 2}, None)
    assert (r.status, r.gateway_id) == ("map_only", 2)


def test_exact_case_sensitive_match():
    r = _resolve(channel="cha", channel_index={"CHA": [1]}, site="s1", site_map={"S1": 1})
    assert r.status == "unrouted"


def test_duplicate_gateway_ids_in_index_are_not_a_conflict():
    r = _resolve(channel_index={"CHA": [1, 1]}, site_map={"S1": 1})
    assert (r.status, r.gateway_id) == ("ok", 1)


def test_route_result_is_frozen():
    r = _resolve(only=1)
    assert dataclasses.is_dataclass(r)
    with pytest.raises(dataclasses.FrozenInstanceError):
        r.gateway_id = 2


# ── ROUTABLE_STATUSES / is_routable ──────────────────────────────

def test_routable_statuses_constant():
    assert ROUTABLE_STATUSES == frozenset({"single", "ok", "channel_only", "map_only"})
    assert isinstance(ROUTABLE_STATUSES, frozenset)


def test_is_routable_requires_status_and_gateway_id():
    for st in ROUTABLE_STATUSES:
        assert is_routable(RouteResult(1, st, "x"))
        assert not is_routable(RouteResult(None, st, "x")), "status 可放行但沒有 gateway_id 也不能放行"
    for st in ("conflict", "conflict_channel", "unrouted", "whatever"):
        assert not is_routable(RouteResult(1, st, "x"))
        assert not is_routable(RouteResult(None, st, "x"))


# ── group_by_gateway ─────────────────────────────────────────────

def test_group_by_gateway_keeps_order_and_collects_blocked():
    items = ["a", "b", "c", "d", "e", "f"]
    results = [
        RouteResult(2, "ok", ""),
        RouteResult(1, "channel_only", ""),
        RouteResult(None, "unrouted", "u"),
        RouteResult(2, "map_only", ""),
        RouteResult(None, "conflict", "c"),
        RouteResult(1, "single", ""),
    ]
    groups, blocked = group_by_gateway(items, results)
    assert groups == {2: ["a", "d"], 1: ["b", "f"]}
    assert list(groups) == [2, 1], "群組依首次出現順序"
    assert [(i, r.status) for i, r in blocked] == [("c", "unrouted"), ("e", "conflict")]
    assert blocked[0][1] is results[2]


def test_group_by_gateway_blocks_non_routable_status_even_with_gateway_id():
    groups, blocked = group_by_gateway(["x"], [RouteResult(1, "conflict", "")])
    assert groups == {} and len(blocked) == 1


def test_group_by_gateway_empty_and_length_mismatch():
    assert group_by_gateway([], []) == ({}, [])
    with pytest.raises(ValueError):
        group_by_gateway(["a", "b"], [RouteResult(1, "ok", "")])


# ── build_channel_index ──────────────────────────────────────────

def test_build_channel_index_dedupes_and_groups():
    rows = [("CHA", 1), ("CHA", 1), ("CHB", 2), ("CHA", 2), ("CHB", 2), ("CHA", 1)]
    idx = build_channel_index(rows)
    assert idx == {"CHA": [1, 2], "CHB": [2]}


def test_build_channel_index_skips_blank_channel_or_missing_gateway():
    idx = build_channel_index([("", 1), (None, 1), ("  ", 1), ("CHA", None), ("CHB", 2)])
    assert idx == {"CHB": [2]}


def test_build_channel_index_strips_channel_and_accepts_any_iterable():
    assert build_channel_index(iter([(" CHA ", 1)])) == {"CHA": [1]}
    assert build_channel_index([]) == {}


# ── find_unmapped ────────────────────────────────────────────────

def _locs(*triples):
    return [{"bu": "B", "site": s, "zone": z} for s, z in triples]


def test_find_unmapped_site_covered_by_site_or_by_its_zone():
    locs = _locs(("K18", "Z1"), ("K19", "Z1"), ("K20", "Z2"), ("K21", "Z3"))
    sites, zones = find_unmapped(locs, {"K21": 2}, {"Z1": 1})
    assert sites == ["K20"]              # K18/K19 由 zone Z1 涵蓋，K21 自己有 site 對應
    assert zones == ["Z2", "Z3"]         # 只看有沒有 zone 對應（K21 雖被 site 涵蓋，Z3 仍沒有 zone 對應）


def test_find_unmapped_all_mapped():
    locs = _locs(("K18", "Z1"))
    assert find_unmapped(locs, {"K18": 1}, {"Z1": 1}) == ([], [])


def test_find_unmapped_dedupes_sorts_and_ignores_blank():
    locs = _locs(("K2", "Z9"), ("K1", "Z9"), ("K1", "Z9"), ("", "Z8"), ("K3", ""), ("  ", "  "))
    sites, zones = find_unmapped(locs, {}, {})
    assert sites == ["K1", "K2", "K3"]
    assert zones == ["Z8", "Z9"]


def test_find_unmapped_site_with_blank_zone_needs_its_own_mapping():
    locs = _locs(("K3", ""))
    assert find_unmapped(locs, {}, {"": 1})[0] == ["K3"], "空 zone 不能被 zone_map 裡的空字串 key 涵蓋"
    assert find_unmapped(locs, {"K3": 1}, {})[0] == []


def test_find_unmapped_uses_first_zone_for_site_like_derive():
    """同一 site 有多列時，推導取最前面那列的 zone；未涵蓋判斷與實際路由一致。"""
    locs = _locs(("K1", "Z1"), ("K1", "Z2"))
    assert find_unmapped(locs, {}, {"Z1": 1})[0] == []
    assert find_unmapped(locs, {}, {"Z2": 1})[0] == ["K1"]


# ── localize_reason ──────────────────────────────────────────────

def test_localize_reason_replaces_ids_with_names():
    reason = "結構快取顯示在 Gateway 1，但對應表指定 Gateway 2"
    out = localize_reason(reason, {1: "K8 廠區", 2: "K18 廠區"})
    assert out == "結構快取顯示在 Gateway「K8 廠區」，但對應表指定 Gateway「K18 廠區」"


def test_localize_reason_multi_digit_and_unknown_ids():
    out = localize_reason("Gateway 10 與 Gateway 1 與 Gateway 3", {10: "TEN", 1: "ONE"})
    assert out == "Gateway「TEN」 與 Gateway「ONE」 與 Gateway 3"


def test_localize_reason_handles_empty_inputs():
    assert localize_reason("", {1: "A"}) == ""
    assert localize_reason(None, {1: "A"}) == ""
    assert localize_reason("Gateway 1", {}) == "Gateway 1"
    assert localize_reason("Gateway 1", None) == "Gateway 1"


def test_resolve_then_localize_roundtrip_for_conflict_reasons():
    names = {1: "GW-A", 2: "GW-B"}
    r = _resolve(channel_index={"CHA": [1]}, site_map={"S1": 2})
    out = localize_reason(r.reason, names)
    assert "GW-A" in out and "GW-B" in out and "Gateway 1" not in out and "Gateway 2" not in out
    r = _resolve(channel_index={"CHA": [1, 2]})
    out = localize_reason(r.reason, names)
    assert "GW-A" in out and "GW-B" in out
