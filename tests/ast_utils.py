# -*- coding: utf-8 -*-
"""共用的 AST 靜態檢查輔助函式。

用來對 app/main.py 做「結構化」的靜態檢查（例如：確認 Gateway 鎖一定會在 finally
區塊釋放、確認 create_tag(...) 呼叫一定有帶 data_type 參數），取代原驗收腳本用
regex／字串比對原始碼的作法——regex 容易被巢狀括號、排版、註解字面上出現同樣的
字串騙過，AST 是直接剖析程式結構，不會有這類假陽性/假陰性。

這個模組本身不是測試檔（檔名不是 test_*.py），pytest 不會蒐集它，純粹提供工具函式
給 tests/test_gateway_lock.py、tests/test_data_type.py 等測試使用。
"""
import ast
import functools
import inspect


@functools.lru_cache(maxsize=1)
def _main_source_and_tree():
    """剖析 app/main.py 目前的原始碼，回傳 (原始碼字串, AST)。整個 pytest 執行過程只剖析一次。"""
    import app.main as m
    src = inspect.getsource(m)
    tree = ast.parse(src)
    return src, tree


def get_main_ast():
    """回傳 app/main.py 的 AST。"""
    _, tree = _main_source_and_tree()
    return tree


def get_main_source():
    """回傳 app/main.py 的原始碼字串。"""
    src, _ = _main_source_and_tree()
    return src


def find_nested_function(tree, name):
    """在整個模組 AST 中尋找名稱為 name 的函式定義（不論巢狀層級，例如定義在某個
    async def 路由 handler 內部的背景任務函式），回傳第一個符合的 FunctionDef/AsyncFunctionDef，
    找不到回傳 None。"""
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return node
    return None


def body_without_docstring(func_node):
    """回傳函式主體的陳述式清單，略過開頭的 docstring（如果有的話）。"""
    body = func_node.body
    if (body and isinstance(body[0], ast.Expr)
            and isinstance(body[0].value, ast.Constant)
            and isinstance(body[0].value.value, str)):
        return body[1:]
    return body


def calls_named_in_nodes(nodes):
    """在給定的一組陳述式節點內（例如某個 try 的 finally 區塊），
    找出所有以簡單名稱呼叫的函式名（例如 _release_gateway_lock(...) → '_release_gateway_lock'）。"""
    names = []
    for stmt in nodes:
        for node in ast.walk(stmt):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
                names.append(node.func.id)
    return names


def find_all_calls_named(tree, method_name):
    """找出整個模組中所有 obj.<method_name>(...) 形式的方法呼叫節點
    （ast.Call，其 func 為 ast.Attribute 且 attr == method_name）。"""
    return [
        node for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == method_name
    ]


def call_has_keyword(call_node, kw_name):
    """呼叫節點是否帶有指定的關鍵字參數（例如 data_type=...）。"""
    return any(kw.arg == kw_name for kw in call_node.keywords)


def get_function_source(tree, name, src=None):
    """取得指定（可巢狀）函式定義的原始碼片段字串，供需要在片段上做字串/regex 檢查的
    測試使用。找不到函式時回傳 None。"""
    node = find_nested_function(tree, name)
    if node is None:
        return None
    if src is None:
        src = get_main_source()
    return ast.get_source_segment(src, node)
