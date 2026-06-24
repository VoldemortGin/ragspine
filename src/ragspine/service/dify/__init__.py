"""service/dify —— Dify 编译产物的服务端安全执行层（L0 静态闸 + L1/L2 受限 runner）。

把 ragspine.dify 编译出的 GeneratedCode 安全地跑起来：先过 L0 静态闸（warnings 非空即拒、
import 白名单），再经受限 __builtins__ 命名空间 + 超时 + :memory: + chdir(tmp) 的 L1 runner
执行（可选 L2 子进程隔离）。本层只装机制，具体不变量由服务端 env 与各端点绑。

Submodules:
    safety.py — L0 静态安全闸（warnings 拒跑 + import 白名单 AST 校验）。
"""

from ragspine import _lazy_submodules

__getattr__, __dir__ = _lazy_submodules(__name__, __path__)
