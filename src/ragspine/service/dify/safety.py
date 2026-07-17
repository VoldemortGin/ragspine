"""L0 静态安全闸：在执行 GeneratedCode 之前做不可绕过的静态拒跑判定。

三道闸（全接受默认——任一不过即拒，绝不"宽容放行"）：
1. warnings 非空即拒：编译器对不支持节点 / tool 占位会生成带 NotImplementedError 的骨架
   钩子并记 warning（emitter._emit_one）。这类骨架一跑就抛，且语义上"未补全即不可信"，
   故有任何 warning 一律不执行（HTTP 层整形为 422）。
2. import 白名单：AST 静态遍历生成源码里的 import / from-import，逐个比对顶层根模块；
   任何根模块不在白名单（如 os / subprocess / socket / shutil）即拒。白名单只放生成器
   实际会产出的安全模块（stdlib 计算/序列化 + 家族包），不含任何 I/O / 进程 / 网络模块。
3. HTTP 出网默认关：源码含 http-request 节点（GeneratedCode.requires_http，或从源码静态
   检出 _HTTP_CLIENT 槽位——worker / 子进程重建 GeneratedCode 丢失 flag 也拦得住）而环境
   变量 RAGSPINE_DIFY_HTTP_ENABLED 未显式开启即拒。开启后由受信 runner 注入受控 urllib
   客户端（service.dify.http_client）；import 白名单【不】为此放宽——生成代码从不 import
   网络模块，唯一出网面是 runner 注入的槽位。

这是机制（拒跑判据），不是保证：执行隔离的纵深（受限 builtins / 超时 / 子进程）由 runner
负责，本闸只在更早的静态期把明显不可执行 / 含越权 import 的产物挡在门外。
"""

from __future__ import annotations

import ast
import os
from collections.abc import Mapping

from corespine import CorespineError

from ragspine.dify.codegen.emitter import GeneratedCode

# HTTP 出网开关（信任边界，默认关）。真值集合对齐 corespine load_from_env 的 bool 解析
# （RAGSPINE_DIFY_RUN_ENABLED 同款惯例）：{1,true,yes,on} 为真，大小写不敏感；其余为假。
HTTP_ENABLED_ENV = "RAGSPINE_DIFY_HTTP_ENABLED"
_ENV_TRUE: frozenset[str] = frozenset({"1", "true", "yes", "on"})

# import 白名单：生成代码只应 import 这些顶层根模块（按根名匹配，覆盖其全部子模块，
# 故 ragspine 覆盖 ragspine.retrieval.* 等）。刻意【不含】任何 I/O / 进程 / 网络模块
# （os / sys / subprocess / socket / shutil / pathlib / importlib ...）。
ALLOWED_IMPORT_ROOTS: frozenset[str] = frozenset(
    {
        "__future__",
        "dataclasses",
        "typing",
        "string",
        "concurrent",  # concurrent.futures.ThreadPoolExecutor（并行/iteration 节点）
        "json",        # parameter-extractor 节点
        "corespine",
        "ragspine",    # 含 ragspine.retrieval.*（knowledge-retrieval / answer_question 折叠）
        "spineagent",  # tool 节点 @function_tool（占位，实际会因 warning 先被闸 1 拒）
    }
)


class DifyUnsafeError(CorespineError):
    """GeneratedCode 未通过 L0 静态安全闸：含 warning 骨架或越权 import，拒绝执行。

    继承家族统一异常基类，稳定 code 为 "dify.unsafe"（ADR errors 缝），HTTP 层整形为 422。
    """

    code = "dify.unsafe"


def http_enabled(env: Mapping[str, str] | None = None) -> bool:
    """HTTP 出网开关是否显式开启（默认读 os.environ；测试可注入 env mapping）。"""
    source = os.environ if env is None else env
    return source.get(HTTP_ENABLED_ENV, "").strip().lower() in _ENV_TRUE


def _declares_http_slot(source: str) -> bool:
    """源码是否声明模块级 _HTTP_CLIENT 槽位（http-request 节点的唯一出网面）。

    静态从源码检出，不依赖 GeneratedCode.requires_http flag——worker / L2 子进程从纯
    可序列化 payload 重建 GeneratedCode 时 flag 不随行，本判据保证防御式复检照样拦截。
    """
    for node in ast.parse(source).body:
        if isinstance(node, ast.Assign):
            if any(
                isinstance(t, ast.Name) and t.id == "_HTTP_CLIENT" for t in node.targets
            ):
                return True
        elif isinstance(node, ast.AnnAssign):
            if isinstance(node.target, ast.Name) and node.target.id == "_HTTP_CLIENT":
                return True
    return False


def _import_roots(source: str) -> set[str]:
    """AST 遍历源码，收集所有 import / from-import 的顶层根模块名。

    相对 import（from . import x，level>0）无 module 名——生成代码从不产生相对 import，
    保守起见把它视作空根（不会落在白名单内，从而被闸拒）。
    """
    tree = ast.parse(source)
    roots: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                roots.add(alias.name.split(".", 1)[0])
        elif isinstance(node, ast.ImportFrom):
            if node.level and not node.module:
                roots.add("")  # 相对 import，无根名 -> 必被白名单拒
            elif node.module is not None:
                roots.add(node.module.split(".", 1)[0])
    return roots


def assert_runnable(code: GeneratedCode) -> None:
    """L0 静态闸：通过则静默返回，任一不过抛 DifyUnsafeError（拒绝执行）。

    闸 1：warnings 非空 -> 拒（含 NotImplementedError 骨架 / 不支持节点）。
    闸 2：源码 import 的任一顶层根模块不在 ALLOWED_IMPORT_ROOTS -> 拒。
    闸 3：含 http-request 节点而 RAGSPINE_DIFY_HTTP_ENABLED 未显式开启 -> 拒。
    """
    if code.warnings:
        raise DifyUnsafeError(
            "生成代码含未补全的骨架钩子（不支持节点 / tool 占位），拒绝执行；"
            f"warnings={list(code.warnings)}",
            n_warnings=len(code.warnings),
        )
    disallowed = sorted(_import_roots(code.source) - ALLOWED_IMPORT_ROOTS)
    if disallowed:
        raise DifyUnsafeError(
            f"生成代码 import 不在白名单的模块，拒绝执行：{disallowed}",
            disallowed=disallowed,
        )
    if (code.requires_http or _declares_http_slot(code.source)) and not http_enabled():
        raise DifyUnsafeError(
            "工作流含 http-request 节点，HTTP 出网默认关闭：设环境变量 "
            f"{HTTP_ENABLED_ENV}=true 显式开启后再执行（开启后由受信 runner 注入"
            "受控 HTTP 客户端：超时 ≤30s、仅 http/https、响应体 1MB 上限）。",
            requires_http=True,
        )
