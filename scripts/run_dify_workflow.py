"""源码树兼容入口；真实 L2 实现在可随 wheel 安装的包内模块。"""

from pathlib import Path

import rootutils

# 锚定项目根用本脚本自身位置（而非 os.getcwd）：runner 以 cwd=临时目录 spawn 本进程
# （进程私有 chdir(tmp) 语义），cwd 不指向项目根，故必须按 __file__ 找根。
ROOT_DIR = rootutils.setup_root(
    Path(__file__).resolve().parent, indicator=".project-root", pythonpath=True
)

from ragspine.service.dify.run_dify_workflow import main  # noqa: E402, I001


if __name__ == "__main__":
    raise SystemExit(main())
