#!/usr/bin/env python3
"""检查一个 Python 项目是否符合本规范的关键结构不变量。

用法:
    python check_conformance.py [PROJECT_ROOT]   # 默认当前目录

退出码非 0 表示有违规;由项目的 ci.sh 调用(`make hooks` 可把那条门钉进 git pre-push)。
这是规范里少数可"硬强制"的部分——其余靠 ci.sh 那条门 + skill 让 AI 默认遵循。

只依赖标准库,接受项目根参数,可独立运行:scaffold.py 会把本文件复制进生成项目的
scripts/,由那个项目的 ci.sh 调用。skill 里这份是源,项目里那份是快照。
不挂进门的检查器等于不存在——所以生成项目的 ci.sh 里必须有它。
"""

import ast
import re
import sys
import tomllib
from pathlib import Path

# 发行名 → import 名的已知例外(其余按 PEP 503 规范化后即是 import 名)
_DIST_IMPORT_ALIASES: dict[str, frozenset[str]] = {
    "pyyaml": frozenset({"yaml"}),
    "pillow": frozenset({"PIL"}),
    "beautifulsoup4": frozenset({"bs4"}),
    "python_dotenv": frozenset({"dotenv"}),
    "python_dateutil": frozenset({"dateutil"}),
    "scikit_learn": frozenset({"sklearn"}),
    "opencv_python": frozenset({"cv2"}),
    "protobuf": frozenset({"google"}),
    "attrs": frozenset({"attr", "attrs"}),
    "setuptools": frozenset({"setuptools", "pkg_resources"}),
    "zope_interface": frozenset({"zope"}),
}

_REQUIREMENT_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*")


def _import_names_of(requirement: str) -> frozenset[str]:
    """把一条依赖声明(如 `pydantic-settings[yaml]>=2`)映射成它可能的 import 名。"""
    m = _REQUIREMENT_NAME.match(requirement.strip())
    if not m:
        return frozenset()
    canon = re.sub(r"[-_.]+", "_", m.group(0)).lower()
    return _DIST_IMPORT_ALIASES.get(canon, frozenset()) | {canon}


def _find_package(root: Path) -> tuple[Path | None, list[str]]:
    problems: list[str] = []
    src = root / "src"
    if not src.is_dir():
        problems.append("缺少 src/ 目录:必须用 src 布局(src/<pkg>/)。")
        return None, problems
    pkgs = [d for d in src.iterdir() if d.is_dir() and (d / "__init__.py").is_file()]
    if not pkgs:
        problems.append("src/ 下没有含 __init__.py 的包目录。")
        return None, problems
    if len(pkgs) > 1:
        problems.append(
            f"src/ 下有多个包目录({', '.join(p.name for p in pkgs)});应只有一个。"
        )
    pkg = pkgs[0]
    if pkg.name == "src":
        problems.append("包名是 'src':请用真实包名(自用也别 import src)。")
    return pkg, problems


def _module_has_code(path: Path) -> bool:
    """模块是否含代码。

    只有「真正的 docstring」不算代码:模块的首个语句且是字符串字面量。
    其余任何语句都算代码——包括裸常量表达式(如 `42`)、非首语句的裸字符串、
    `pass`、赋值、import。空文件与纯注释文件解析后 body 为空,不算代码。
    """
    body = ast.parse(path.read_text(encoding="utf-8")).body
    if not body:
        return False
    first = body[0]
    is_docstring = (
        isinstance(first, ast.Expr)
        and isinstance(first.value, ast.Constant)
        and isinstance(first.value.value, str)
    )
    return not (is_docstring and len(body) == 1)


def _relative_import_lines(path: Path) -> list[int]:
    """所有相对导入的行号(ast.walk 会下探函数体,函数内的延迟导入同样被抓)。"""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    return sorted(
        n.lineno
        for n in ast.walk(tree)
        if isinstance(n, ast.ImportFrom) and n.level and n.level > 0
    )


def _first_party_import_lines(path: Path, package: str) -> list[int]:
    """所有导入本包的行号:相对导入,或以包名开头的绝对导入。"""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    lines: set[int] = set()
    for n in ast.walk(tree):
        if isinstance(n, ast.Import):
            if any(a.name.split(".")[0] == package for a in n.names):
                lines.add(n.lineno)
        elif isinstance(n, ast.ImportFrom) and (
            n.level or (n.module and n.module.split(".")[0] == package)
        ):
            lines.add(n.lineno)
    return sorted(lines)


def _installs_beartype_hook(path: Path) -> bool:
    """语法树里是否真有一次 beartype_this_package(...) 调用。

    不用字符串匹配:注释掉的 hook、docstring 里提到的名字都会骗过 grep,
    而"hook 被注释掉"恰恰是这条检查最该抓的那种失效。
    """
    for n in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if not isinstance(n, ast.Call):
            continue
        f = n.func
        name: str | None = None
        if isinstance(f, ast.Name):
            name = f.id
        elif isinstance(f, ast.Attribute):
            name = f.attr
        if name == "beartype_this_package":
            return True
    return False


def _toplevel_function_lines(path: Path) -> list[int]:
    """模块顶层定义的函数行号(不含嵌套在类/函数里的)。"""
    body = ast.parse(path.read_text(encoding="utf-8")).body
    return [
        n.lineno for n in body if isinstance(n, ast.FunctionDef | ast.AsyncFunctionDef)
    ]


def _imported_top_modules(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    mods: set[str] = set()
    for n in ast.walk(tree):
        if isinstance(n, ast.Import):
            for a in n.names:
                mods.add(a.name.split(".")[0])
        elif isinstance(n, ast.ImportFrom) and not n.level and n.module:
            mods.add(n.module.split(".")[0])
    return mods


def check(root: Path) -> list[str]:
    problems: list[str] = []

    # 先读 pyproject:运行时依赖清单是下面「封闭式 import 白名单」的输入
    runtime_deps: list[str] = []
    pyproject = root / "pyproject.toml"
    if not pyproject.is_file():
        problems.append("缺少 pyproject.toml。")
    else:
        data = tomllib.loads(pyproject.read_text(encoding="utf-8"))
        if data.get("tool", {}).get("mypy", {}).get("strict") is not True:
            problems.append("pyproject [tool.mypy] 未设 strict = true。")
        project = data.get("project")
        if isinstance(project, dict):
            declared = project.get("dependencies")
            if isinstance(declared, list):
                runtime_deps = [d for d in declared if isinstance(d, str)]

    pkg, pkg_problems = _find_package(root)
    problems.extend(pkg_problems)

    if pkg is not None:
        init = pkg / "__init__.py"
        if not _installs_beartype_hook(init):
            problems.append(
                f"{init.relative_to(root)} 未安装 beartype claw hook:"
                "语法树里找不到 beartype_this_package(...) 调用"
                "(注释掉、只在 docstring 里提到都不算)。"
            )
        if lines := _toplevel_function_lines(init):
            where = ", ".join(map(str, lines))
            problems.append(
                f"{init.relative_to(root)} 在顶层定义了函数(行 {where}):"
                "hook 安装时本文件已在执行中,这里定义的函数**永远不会被 instrument**。"
                "顶层 __init__.py 只放 hook 和 re-export,函数请挪进子模块。"
            )

        core = pkg / "core"
        if not core.is_dir():
            problems.append("缺少 core/:settings/logging/prompts 的统一来源。")
        else:
            core_init = core / "__init__.py"
            if not core_init.is_file():
                problems.append("缺少 core/__init__.py。")
            elif _module_has_code(core_init):
                problems.append(
                    "core/__init__.py 含代码:必须保持空"
                    "(否则 logging/prompts 等会在 hook 前被导入而漏检)。"
                )
            settings = core / "settings.py"
            if not settings.is_file():
                problems.append("缺少 core/settings.py。")
            elif _first_party_import_lines(settings, pkg.name):
                problems.append(
                    "core/settings.py import 了一方模块:它必须是 beartype 叶子"
                    "(只依赖标准库 + 第三方)。"
                )

        # 包内一律绝对导入(见规范 §7.1):相对导入在散文件跑法 / IDE 右键 Run 下必挂
        for py in pkg.rglob("*.py"):
            lines = _relative_import_lines(py)
            if lines:
                where = ", ".join(map(str, lines))
                problems.append(
                    f"{py.relative_to(root)} 有相对导入(行 {where}):"
                    f"包内一律用绝对导入 from {pkg.name}.X import Y。"
                )

        # 模型无关:adapters/ 之外的 import 面是**封闭**的白名单,不是厂商黑名单。
        # 白名单 = 标准库 + 本包自身 + pyproject [project.dependencies] 的运行时依赖。
        # 黑名单永远漏(下一个新 SDK 没人登记就畅通无阻);封闭式则默认拒绝:
        # 厂商 SDK 放 optional-dependencies,自然不在白名单里,一出现在 adapters/ 外就红。
        allowed = set(sys.stdlib_module_names) | {pkg.name}
        for dep in runtime_deps:
            allowed |= _import_names_of(dep)

        adapters = pkg / "adapters"
        for py in pkg.rglob("*.py"):
            if py.is_relative_to(adapters):
                continue  # 在 adapters/ 下,允许(且应当 lazy)import 厂商 SDK
            leaked = sorted(_imported_top_modules(py) - allowed)
            if leaked:
                problems.append(
                    f"{py.relative_to(root)} 导入了白名单外的顶层包 {leaked}:"
                    "adapters/ 之外只允许标准库、本包自身,以及 pyproject "
                    "[project.dependencies] 里声明的运行时依赖。厂商 SDK 请进 "
                    "[project.optional-dependencies],并只在 adapters/ 下 lazy import,"
                    "核心/领域代码经 ports/ 的 Protocol 调用。"
                )

    # 仓库根锚点:core/settings.py 的 ROOT_DIR 从 CWD 向上找这个标记文件,缺了就抛错
    if not (root / ".project-root").is_file():
        problems.append(
            "缺少 .project-root:它标记仓库根,core/settings.py 靠它定位 ROOT_DIR。"
        )

    return problems


def main() -> int:
    root = Path(sys.argv[1] if len(sys.argv) > 1 else ".").resolve()
    problems = check(root)
    if problems:
        print(f"✗ 不符合规范({len(problems)} 项):\n")
        for p in problems:
            print(f"  - {p}")
        return 1
    print("✓ 通过关键结构检查。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
