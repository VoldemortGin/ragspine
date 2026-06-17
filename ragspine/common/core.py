"""跨切面全局常量的单一出处：数据根 + 各 sqlite 库的默认路径。

集中在此是为了统一命名与内容——这些默认过去散在多个脚本里各写一份（且 run_demo
用 DB_PATH、其余用 DEFAULT_DB，同物异名）。**域内调参常量**（如 retrieval 的
DEFAULT_TOP_K、chunking 的 DEFAULT_CHUNK_CHARS、agent 的 MAX_TOOL_ITERATIONS）**不**放
这里：它们是算法默认值、就近于所属模块、由调用方按参数覆盖，集中反而破坏深目录的就近原则。

路径以本文件位置推算（ragspine/common/core.py -> parents[2] = 仓库根），不依赖
cwd / rootutils，import 期零副作用（与 company_profile 的 __file__ 相对路径同源）。
这些是**开发 / demo 默认**，库 API 本身仍以显式传入的路径为准（FactStore / ServiceConfig
都接受路径参数）。
"""

from pathlib import Path

# 仓库根 + 数据根（单一出处）。
REPO_ROOT = Path(__file__).resolve().parents[2]
DATA_DIR = REPO_ROOT / "data"

# 各 sqlite 库默认路径（开发 / 脚本默认，可被 --db 等覆盖）。
DEFAULT_FACT_DB = DATA_DIR / "fact_metric.db"          # 结构化事实表 + 叙事块表（同一 sqlite，多表）
DEFAULT_MAPPING_DB = DATA_DIR / "color_mapping.db"      # 颜色映射注册表
DEFAULT_REVIEW_QUEUE_DB = DATA_DIR / "review_queue.db"  # SME 复核队列
