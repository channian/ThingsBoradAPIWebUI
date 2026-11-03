# -*- coding: utf-8 -*-
"""安全鎖模組 app/safety.py 的行為測試。

純邏輯測試：不需要 DB，也刻意不 import app.main（避免牽動資料庫環境）。

防止的退化：
- 批次上限的邊界（count == limit 必須放行、limit+1 必須擋下）被改成 >= / > 之類的 off-by-one。
- 預設值被悄悄改掉，或環境變數名稱與錯誤訊息裡提示管理者調整的名稱不一致。
- 環境變數填錯（非數字、0、負數）時讓服務啟動失敗，而不是退回預設並記錄警告。
- 「新增操作只需在 OPERATIONS 加一列」的表驅動設計被改回寫死的 if/elif。
"""
import dataclasses
import logging
import subprocess
import sys
from pathlib import Path

import pytest

from app import safety
from app.safety import (
    OPERATIONS,
    OperationSpec,
    SafetyError,
    SafetyLimits,
    check_batch_size,
    check_queue_depth,
    get_limits,
    load_limits,
    reload_limits,
)

# 四個環境變數（欄位名, 環境變數名, 預設值）
LIMIT_ENVS = [
    ("kw_execute_max", "KIS_LIMIT_KW_EXECUTE", 10000),
    ("scale_max", "KIS_LIMIT_SCALE", 10000),
    ("delete_max", "KIS_LIMIT_DELETE", 2000),
    ("queue_max_per_gateway", "KIS_LIMIT_QUEUE_PER_GATEWAY", 5),
]


@pytest.fixture(autouse=True)
def _isolate_limits_cache():
    """每個測試前後都清掉模組層級快取。

    autouse fixture 會比測試自己要求的 monkeypatch 更早建立、更晚拆除，
    所以離開時快取是在 monkeypatch 還原環境變數「之後」才被清掉，
    不會把測試用的環境值殘留給其他測試檔。
    """
    safety._cached_limits = None
    yield
    safety._cached_limits = None


def _clear_limit_envs(monkeypatch):
    """移除四個 KIS_LIMIT_* 環境變數，避免執行測試的 shell 殘留值干擾 get_limits()。"""
    for _field, env_name, _default in LIMIT_ENVS:
        monkeypatch.delenv(env_name, raising=False)


def _safety_warnings(caplog):
    return [r for r in caplog.records if r.name == "safety" and r.levelno == logging.WARNING]


# ── 預設值與環境變數讀取 ─────────────────────────────────

def test_defaults():
    """空環境 → 10000 / 10000 / 2000 / 5；SafetyLimits() 本身預設也一致。"""
    limits = load_limits({})
    assert limits == SafetyLimits(
        kw_execute_max=10000, scale_max=10000, delete_max=2000, queue_max_per_gateway=5
    )
    assert SafetyLimits() == limits


def test_limits_are_frozen():
    """SafetyLimits 為 frozen dataclass，不可被就地修改。"""
    limits = SafetyLimits()
    with pytest.raises(dataclasses.FrozenInstanceError):
        limits.delete_max = 1  # type: ignore[misc]


@pytest.mark.parametrize("field,env_name,default", LIMIT_ENVS)
def test_env_overrides_single_field(field, env_name, default):
    """每個環境變數只覆寫自己對應的欄位，其餘維持預設。"""
    limits = load_limits({env_name: "123"})
    assert getattr(limits, field) == 123
    for other_field, _other_env, other_default in LIMIT_ENVS:
        if other_field != field:
            assert getattr(limits, other_field) == other_default


def test_env_overrides_all_fields():
    limits = load_limits({
        "KIS_LIMIT_KW_EXECUTE": "11",
        "KIS_LIMIT_SCALE": "22",
        "KIS_LIMIT_DELETE": "33",
        "KIS_LIMIT_QUEUE_PER_GATEWAY": "44",
    })
    assert (limits.kw_execute_max, limits.scale_max, limits.delete_max, limits.queue_max_per_gateway) == (
        11, 22, 33, 44
    )


def test_env_value_with_surrounding_whitespace_is_accepted(caplog):
    """"  200  " 前後空白要容忍，成功解析為 200，且不產生警告。"""
    with caplog.at_level(logging.WARNING, logger="safety"):
        limits = load_limits({"KIS_LIMIT_DELETE": "  200  "})
    assert limits.delete_max == 200
    assert _safety_warnings(caplog) == []


def test_unset_env_uses_default_without_warning(caplog):
    """沒設定環境變數是正常情況：用預設值，不應該有警告。"""
    with caplog.at_level(logging.WARNING, logger="safety"):
        load_limits({})
    assert _safety_warnings(caplog) == []


# 非整數、空字串、純空白、0、負數、小數、科學記號、底線千分位 皆視為非法
INVALID_VALUES = ["abc", "0", "-5", "", "   ", "1.5", "1e3", "1_000", "0x10"]


@pytest.mark.parametrize("bad", INVALID_VALUES)
@pytest.mark.parametrize("field,env_name,default", LIMIT_ENVS)
def test_invalid_env_falls_back_to_default_and_warns(bad, field, env_name, default, caplog):
    """非法值退回該項預設、不拋例外、記錄恰好一行警告（內含環境變數名稱）；其他項不受影響。"""
    env = {env_name: bad}
    # 另一個欄位給合法值，確認一個壞值不會連累其他設定
    other_field, other_env, _ = next(t for t in LIMIT_ENVS if t[0] != field)
    env[other_env] = "77"

    with caplog.at_level(logging.WARNING, logger="safety"):
        limits = load_limits(env)  # 不可拋例外

    assert getattr(limits, field) == default
    assert getattr(limits, other_field) == 77
    warnings = _safety_warnings(caplog)
    assert len(warnings) == 1
    assert env_name in warnings[0].getMessage()


def test_multiple_invalid_values_each_warn_once(caplog):
    with caplog.at_level(logging.WARNING, logger="safety"):
        limits = load_limits({"KIS_LIMIT_KW_EXECUTE": "abc", "KIS_LIMIT_SCALE": "-1"})
    assert limits == SafetyLimits()
    assert len(_safety_warnings(caplog)) == 2


def test_load_limits_reads_os_environ_by_default(monkeypatch):
    """env 參數省略時讀 os.environ（呼叫當下才取用）。"""
    _clear_limit_envs(monkeypatch)
    monkeypatch.setenv("KIS_LIMIT_DELETE", "321")
    assert load_limits().delete_max == 321


# ── check_batch_size ───────────────────────────────────

@pytest.mark.parametrize("operation", ["kw_execute", "scale", "delete", "datatype"])
def test_batch_boundary(operation):
    """count == 上限 放行；上限 + 1 擋下；0 放行。"""
    limits = SafetyLimits(kw_execute_max=10, scale_max=20, delete_max=30, queue_max_per_gateway=5)
    limit = getattr(limits, OPERATIONS[operation].limit_field)

    check_batch_size(operation, 0, limits)
    check_batch_size(operation, limit - 1, limits)
    check_batch_size(operation, limit, limits)  # 等於上限：允許
    with pytest.raises(SafetyError) as exc_info:
        check_batch_size(operation, limit + 1, limits)
    assert exc_info.value.code == "batch_too_large"


def test_each_operation_uses_its_own_limit():
    """三個操作各自對應自己的上限，不會串用。"""
    limits = SafetyLimits(kw_execute_max=10, scale_max=20, delete_max=30, queue_max_per_gateway=5)
    check_batch_size("kw_execute", 10, limits)
    check_batch_size("scale", 20, limits)
    check_batch_size("delete", 30, limits)
    with pytest.raises(SafetyError):
        check_batch_size("kw_execute", 11, limits)
    with pytest.raises(SafetyError):
        check_batch_size("scale", 21, limits)
    with pytest.raises(SafetyError):
        check_batch_size("delete", 31, limits)


def test_default_limits_values_enforced():
    """用預設上限驗證：建點/Scale/資料型態校正 10000，刪除 2000。"""
    limits = load_limits({})
    check_batch_size("kw_execute", 10000, limits)
    check_batch_size("scale", 10000, limits)
    check_batch_size("delete", 2000, limits)
    check_batch_size("datatype", 10000, limits)
    with pytest.raises(SafetyError):
        check_batch_size("kw_execute", 10001, limits)
    with pytest.raises(SafetyError):
        check_batch_size("scale", 10001, limits)
    with pytest.raises(SafetyError):
        check_batch_size("delete", 2001, limits)
    with pytest.raises(SafetyError):
        check_batch_size("datatype", 10001, limits)


def test_datatype_shares_the_kw_execute_limit_and_env_var():
    """資料型態檢查／校正沿用建點的上限欄位與環境變數，不是 scale / delete 的，也沒有自己的新上限。"""
    # 只有 kw_execute_max 小：datatype 受它限制
    small_kw = SafetyLimits(kw_execute_max=5, scale_max=1000, delete_max=1000)
    check_batch_size("datatype", 5, small_kw)
    with pytest.raises(SafetyError) as exc_info:
        check_batch_size("datatype", 6, small_kw)
    e = exc_info.value
    assert (e.operation, e.limit, e.actual, e.code) == ("datatype", 5, 6, "batch_too_large")
    assert "Kepware 資料型態校正" in str(e)
    assert "KIS_LIMIT_KW_EXECUTE" in str(e)
    assert "KIS_LIMIT_SCALE" not in str(e) and "KIS_LIMIT_DELETE" not in str(e)
    # kw_execute_max 大、scale / delete 小：datatype 不受影響
    small_others = SafetyLimits(kw_execute_max=1000, scale_max=1, delete_max=1)
    check_batch_size("datatype", 1000, small_others)
    # 環境變數 KIS_LIMIT_KW_EXECUTE 同時調整兩者
    limits = load_limits({"KIS_LIMIT_KW_EXECUTE": "7"})
    assert limits.kw_execute_max == 7
    check_batch_size("datatype", 7, limits)
    with pytest.raises(SafetyError):
        check_batch_size("datatype", 8, limits)
    assert not hasattr(limits, "datatype_max"), "不新增獨立的上限欄位"


def test_batch_error_attributes_and_message():
    limits = SafetyLimits(delete_max=2000)
    with pytest.raises(SafetyError) as exc_info:
        check_batch_size("delete", 2500, limits)
    e = exc_info.value
    assert (e.operation, e.limit, e.actual, e.code) == ("delete", 2000, 2500, "batch_too_large")
    msg = str(e)
    assert "Kepware 批次刪除" in msg        # 中文操作名稱
    assert "2500" in msg                    # 實際點數
    assert "2000" in msg                    # 上限
    assert "分批處理" in msg and "ids" in msg  # 補救方式
    assert "KIS_LIMIT_DELETE" in msg        # 該操作實際對應的環境變數


@pytest.mark.parametrize("operation,label,env_name", [
    ("kw_execute", "Kepware 建點", "KIS_LIMIT_KW_EXECUTE"),
    ("scale", "Kepware Scale 設定", "KIS_LIMIT_SCALE"),
    ("delete", "Kepware 批次刪除", "KIS_LIMIT_DELETE"),
    ("datatype", "Kepware 資料型態校正", "KIS_LIMIT_KW_EXECUTE"),
])
def test_error_message_names_operation_and_its_own_env_var(operation, label, env_name):
    """訊息要寫出「該操作」的中文名稱與環境變數，不可混用別的操作的。"""
    with pytest.raises(SafetyError) as exc_info:
        check_batch_size(operation, 99, SafetyLimits(kw_execute_max=1, scale_max=1, delete_max=1))
    msg = str(exc_info.value)
    assert label in msg
    assert env_name in msg
    other_envs = {"KIS_LIMIT_KW_EXECUTE", "KIS_LIMIT_SCALE", "KIS_LIMIT_DELETE"} - {env_name}
    assert not any(other in msg for other in other_envs)


def test_safety_error_is_plain_exception_with_message():
    """SafetyError 是 Exception 子類，str() 就是訊息（呼叫端可直接當 HTTP detail 使用）。"""
    e = SafetyError("delete", 1, 2, "batch_too_large", "訊息")
    assert isinstance(e, Exception)
    assert str(e) == "訊息"


def test_unknown_operation_raises_value_error():
    # 未知操作屬程式錯誤，拋 ValueError（SafetyError 不是 ValueError 子類，不會被誤判通過）
    with pytest.raises(ValueError):
        check_batch_size("nope", 1, SafetyLimits())
    assert not issubclass(SafetyError, ValueError)


def test_negative_count_raises_value_error():
    with pytest.raises(ValueError):
        check_batch_size("delete", -1, SafetyLimits())


def test_check_batch_size_defaults_to_get_limits(monkeypatch):
    """limits 為 None 時使用 get_limits()（讀環境變數的快取值）。"""
    _clear_limit_envs(monkeypatch)
    monkeypatch.setenv("KIS_LIMIT_DELETE", "10")
    reload_limits()
    check_batch_size("delete", 10)
    with pytest.raises(SafetyError) as exc_info:
        check_batch_size("delete", 11)
    assert exc_info.value.limit == 10


def test_explicit_limits_override_get_limits(monkeypatch):
    """傳入 limits 時不應受環境變數／快取影響。"""
    _clear_limit_envs(monkeypatch)
    monkeypatch.setenv("KIS_LIMIT_DELETE", "1")
    reload_limits()
    check_batch_size("delete", 50, SafetyLimits(delete_max=50))


# ── check_queue_depth ──────────────────────────────────

def test_queue_boundary():
    """depth == max-1 放行；depth == max 擋下；超過也擋下；0 放行。"""
    limits = SafetyLimits(queue_max_per_gateway=5)
    check_queue_depth(0, limits)
    check_queue_depth(4, limits)
    with pytest.raises(SafetyError) as exc_info:
        check_queue_depth(5, limits)
    assert exc_info.value.code == "queue_full"
    with pytest.raises(SafetyError):
        check_queue_depth(6, limits)


def test_queue_error_attributes_and_message():
    with pytest.raises(SafetyError) as exc_info:
        check_queue_depth(5, SafetyLimits(queue_max_per_gateway=5))
    e = exc_info.value
    assert (e.limit, e.actual, e.code) == (5, 5, "queue_full")
    assert isinstance(e.operation, str) and e.operation
    msg = str(e)
    assert "佇列已滿" in msg
    assert "稍後再試" in msg
    assert "取消" in msg and "排隊" in msg
    assert "KIS_LIMIT_QUEUE_PER_GATEWAY" in msg


def test_queue_default_limit_is_five():
    check_queue_depth(4, load_limits({}))
    with pytest.raises(SafetyError):
        check_queue_depth(5, load_limits({}))


def test_queue_negative_depth_raises_value_error():
    with pytest.raises(ValueError):
        check_queue_depth(-1, SafetyLimits())


def test_queue_defaults_to_get_limits(monkeypatch):
    _clear_limit_envs(monkeypatch)
    monkeypatch.setenv("KIS_LIMIT_QUEUE_PER_GATEWAY", "2")
    reload_limits()
    check_queue_depth(1)
    with pytest.raises(SafetyError):
        check_queue_depth(2)


# ── get_limits 快取 / reload_limits ────────────────────

def test_get_limits_is_cached_until_reload(monkeypatch):
    """get_limits 第一次載入後即快取；環境變數改了也不變，直到 reload_limits()。"""
    _clear_limit_envs(monkeypatch)
    monkeypatch.setenv("KIS_LIMIT_DELETE", "100")
    first = get_limits()
    assert first.delete_max == 100
    assert get_limits() is first  # 同一個快取物件

    monkeypatch.setenv("KIS_LIMIT_DELETE", "200")
    assert get_limits().delete_max == 100  # 仍是快取值

    reloaded = reload_limits()
    assert reloaded.delete_max == 200
    assert get_limits() is reloaded
    assert get_limits().delete_max == 200


def test_reload_limits_picks_up_removed_env(monkeypatch):
    """環境變數被移除後 reload，應回到預設值。"""
    _clear_limit_envs(monkeypatch)
    monkeypatch.setenv("KIS_LIMIT_SCALE", "9")
    assert reload_limits().scale_max == 9
    monkeypatch.delenv("KIS_LIMIT_SCALE")
    assert reload_limits().scale_max == 10000


def test_get_limits_invalid_env_does_not_raise(monkeypatch, caplog):
    """環境變數非法時，get_limits（啟動路徑）不拋例外，退回預設。"""
    _clear_limit_envs(monkeypatch)
    monkeypatch.setenv("KIS_LIMIT_KW_EXECUTE", "abc")
    with caplog.at_level(logging.WARNING, logger="safety"):
        limits = reload_limits()
    assert limits.kw_execute_max == 10000
    assert len(_safety_warnings(caplog)) == 1


# ── OPERATIONS 表與表驅動 ──────────────────────────────

def test_operations_table_contents():
    assert set(OPERATIONS) == {"kw_execute", "scale", "delete", "datatype"}
    assert OPERATIONS["kw_execute"] == OperationSpec("Kepware 建點", "KIS_LIMIT_KW_EXECUTE", "kw_execute_max")
    assert OPERATIONS["scale"] == OperationSpec("Kepware Scale 設定", "KIS_LIMIT_SCALE", "scale_max")
    assert OPERATIONS["delete"] == OperationSpec("Kepware 批次刪除", "KIS_LIMIT_DELETE", "delete_max")
    assert OPERATIONS["datatype"] == OperationSpec("Kepware 資料型態校正", "KIS_LIMIT_KW_EXECUTE", "kw_execute_max")


@pytest.mark.parametrize("operation", ["kw_execute", "scale", "delete", "datatype"])
def test_operations_table_matches_loader(operation):
    """OPERATIONS 登記的環境變數／欄位，必須真的是 load_limits 讀取的那個。

    否則錯誤訊息會叫管理者去調整一個沒有作用的環境變數。
    """
    spec = OPERATIONS[operation]
    assert spec.limit_field in {f.name for f in dataclasses.fields(SafetyLimits)}
    limits = load_limits({spec.env_var: "4242"})
    assert getattr(limits, spec.limit_field) == 4242


def test_new_operation_needs_only_a_table_row(monkeypatch):
    """表驅動：只在 OPERATIONS 加一列，check_batch_size 就支援新操作（不需改函式）。"""
    monkeypatch.setitem(
        OPERATIONS,
        "bulk_rename",
        OperationSpec("Kepware 批次改名", "KIS_LIMIT_BULK_RENAME", "delete_max"),
    )
    limits = SafetyLimits(delete_max=7)

    check_batch_size("bulk_rename", 7, limits)
    with pytest.raises(SafetyError) as exc_info:
        check_batch_size("bulk_rename", 8, limits)
    e = exc_info.value
    assert e.operation == "bulk_rename"
    assert (e.limit, e.actual, e.code) == (7, 8, "batch_too_large")
    assert "Kepware 批次改名" in str(e)
    assert "KIS_LIMIT_BULK_RENAME" in str(e)


def test_injected_operation_is_gone_after_monkeypatch_undo():
    """上一個測試注入的暫時項目不會殘留（確認 OPERATIONS 沒被污染）。"""
    assert "bulk_rename" not in OPERATIONS
    with pytest.raises(ValueError):
        check_batch_size("bulk_rename", 1, SafetyLimits())


# ── 模組獨立性 ─────────────────────────────────────────

def test_module_has_no_web_or_db_dependency():
    """app.safety 必須是純邏輯：import 它不可連帶載入 FastAPI / psycopg2 / app.main 等。

    在乾淨的子程序檢查（pytest 本身的 conftest 已 import 了 psycopg2，不能在本程序檢查）。
    """
    code = (
        "import sys\n"
        "import app.safety\n"
        "bad = [m for m in ('fastapi', 'starlette', 'psycopg2', 'app.main', 'app.pg_client') "
        "if m in sys.modules]\n"
        "sys.exit('unexpected imports: ' + ', '.join(bad) if bad else 0)\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", code],
        cwd=str(Path(safety.__file__).resolve().parent.parent),
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
