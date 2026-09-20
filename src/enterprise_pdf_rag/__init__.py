"""包初始化:在导入任何子模块之前,按配置安装 beartype 运行时类型检查。

claw hook 只对其安装之后导入的模块生效,所以本文件必须最先执行;
hook 之前只能导入 settings(叶子)——不要导入任何想被检查的一方模块。

本文件只放 hook 与 re-export,**不要在此定义函数**:hook 安装时本文件已在执行中,
其中定义的函数永远不会被 instrument。

策略固定为 beartype 默认的 O(1) 抽样(容器只随机抽一个元素检查):
BeartypeStrategy.On(线性全量)在 beartype 里至今未实现(官方源码标注
"currently unimplemented"),与 O1 生成的 wrapper 完全相同,分档只是自欺。
容器深处的坏数据由边界处的 pydantic 校验兜底(见规范 §4)。
"""

# 叶子;有意在 hook 之前导入(本身不被检查)
from enterprise_pdf_rag.core.settings import settings

if settings.beartype_on:
    from beartype.claw import beartype_this_package

    beartype_this_package()

# ↓ 其它包级导入/导出一律放在 hook 之后
