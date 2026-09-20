# 合并方案：enterprise-pdf-rag → ragspine

调研日期 2026-09-19。只读结论，未改任何文件。
- A = `/Users/linhan/startup/enterprise-pdf-rag`（包 `enterprise_pdf_rag`，PyPI 名 `enterprise-pdf-rag` v0.1.0.dev0）
- B = `/Users/linhan/startup/spine/ragspine`（包 `ragspine`，PyPI 名 `rag-spine` v0.13.0，remote github.com/VoldemortGin/ragspine）
- 目标：A 并入 B 仓库，并把 B 的依赖升级到最新。

原始细节见同目录 `ragspine-raw.md`、`epr-raw.md`、`dep-versions.md`。

---

## 1. ragspine（B）结构要点
- build：hatchling；`[tool.hatch.build.targets.wheel] packages=["src/ragspine"]`；另有 `force-include`（llms.txt→ragspine/_llms）、`artifacts=["/src/ragspine/service/studio_dist"]`、sdist exclude uv.lock。**src 布局**。
- `requires-python=">=3.12"`（无上界；classifiers 覆盖 3.12/3.13/3.14；CI matrix 三版本都跑）。`.python-version` 不存在。
- base deps 8 个：corespine>=0.1.1（`[tool.uv.sources]` → `../corespine` editable）、python-pptx>=0.6.23、openpyxl>=3.1、rootutils>=1.0.7、pypdfium2>=5.0、PyYAML>=6.0、beartype>=0.18、pydantic>=2.0。
- 其余 ~20 组全在 optional-extras（延迟 import）：pdf=pdfspine>=0.0.4、doc=docspine>=0.1、ppt=pptspine>=0.1、llm=anthropic>=0.40/openai>=1.0、embed/rerank/colbert/splade/colpali/embed-onnx=fastembed>=0.7,<0.9、vector=sqlite-vec/pg8000/qdrant-client、service=fastapi>=0.110/uvicorn/rq/redis/httpx、graph=networkx、tsr=torch/transformers/pillow、pdf-docling=docling(darwin)、dev=pytest>=8/ruff>=0.14/mypy>=1.18/build/twine/reportlab/pdoc/markdown。`all` 聚合 extra。
- uv.lock 存在(~735K, version=1)。lock 已解析：pydantic 2.13.4、beartype 0.22.9、mypy 2.1.0、ruff 0.15.17、pdfspine/docspine/pptspine **0.4.0**、corespine 0.5.1、fastembed 0.8.0、pypdfium2 5.10.1、anthropic 0.109.2、openai 2.42.0、fastapi 0.137.1。⚠ lock 的 pdfspine 0.4.0 与 git 里 spine-family 同步到 v0.9.1、与 A 钉的 0.11.0 冲突（lock 疑似陈旧）。
- src/ragspine 顶层 18 目录：agent cli common compat config diagnostics dify eval extraction fixtures graph ingestion n8n pipeline retrieval service storage workflows + facade.py session.py。
- tests：镜像 src 分域 + conformance/e2e/budget；**根有 test 文件**（test_facade.py 等）依赖 `tests/conftest.py`。conftest：`rootutils.setup_root(indicator=".project-root", pythonpath=True)`、模块级 `from ragspine.fixtures.excel import …`、3 个 autouse session fixture 只做合成 fixture 再生（data/fixtures/{,pdf,pptx}）。**无 socket/no_network 阻断**，网络仅靠 marker `-m "not network"`。
- tool：ruff line-length=100 target=py312 select=E/F/I/W/UP/B ignore=E501,UP042，`src=["src"]`，门里只对 `src/ragspine` 跑；mypy `strict=true files=["src/ragspine"]`（tests/scripts 不在 scope），**无** pydantic.mypy plugin、**无** warn_unreachable、约 30 个第三方 ignore_missing_imports；pytest markers gpu/docling/network、filterwarnings=["error"]+3 条第三方 ignore（httpx/starlette TestClient、docling、qdrant/sqlite ResourceWarning）、无 addopts/testpaths（默认 tests/，非 importlib）。
- docs：adr **0001–0020 连续**；家族地图 docs/spine-family.md（真源 ~/startup/spine/docs/）；根有 CLAUDE.md、CHANGELOG.md、llms.txt、LICENSE、NOTICE；**无 AGENTS.md**。ADR 0009=依赖许可门(≤Apache-2.0)、0011=python-project-standard 偏离记录。
- 门 = `scripts/ci.sh`（`.githooks/pre-push`、`make ci` 转发），8 步：check_docstring_refs → check_doc_drift → mypy → ruff check src/ragspine → pytest(隔离 catalog + `-m "not gpu and not docling and not network"`) → pytest docling(独进程 exit5 容忍) → run_qa_eval(tool+agent, baseline ratchet data/golden/qa_baseline.json) → run_demo smoke。`.github/workflows/ci.yml` **DORMANT**（仅 workflow_dispatch；内部 matrix 3.12/3.13/3.14 跑 `bash scripts/ci.sh`）；另有 image-publish.yml、release.yml。
- scripts/ 22 个（ask/build_docs/check_doc_drift/check_docstring_refs/ci.sh/classify_pdfs/eval_retrieval_ab/export_workflow_catalog/generate_workflow_catalog/ingest_narrative/**ingest.py**/lint.sh/make_fixtures_*/make_synthetic_deck/run_demo/run_dify_workflow/run_qa_eval/run_server/run_worker/topology）。
- .gitignore：`data/*` + `!data/golden/` + `!data/.gitkeep`（保留 golden 评测集）；.venv/caches/dist/studio_dist/docs/generated。
- git：仅 main（origin/main），工作树**完全干净**（status --short=0）。
- 其它顶层：benchmarks/ config/ data/ deploy/(compose+helm) examples/ studio/(vite) .githooks/。

## 2. enterprise-pdf-rag（A）结构要点
- build：hatchling；`packages=["src/enterprise_pdf_rag"]`（hatch 自动带包内非 .py 资源）。src 布局。
- `requires-python=">=3.12,<3.13"` ← **锁 <3.13**。无显式注释；推断因钉死原生/渲染依赖 pdfspine==0.11.0 / resvg-py==0.5.0 / fonttools==4.65.0（wheel 仅覆盖 3.12），且 .python-version=3.12、mypy python_version=3.12、ruff target=py312。
- deps：pydantic>=2.12,<3；pydantic-settings[yaml]>=2.2,<3；beartype(无约束)；jinja2(无)；fastapi(无)；uvicorn(无)；**pdfspine==0.11.0**、**resvg-py==0.5.0**、**fonttools==4.65.0**（三钉死）。**无 corespine**。
- extras：pdf=[]、processing=[]（空别名，能力已并入默认）。
- `[dependency-groups] dev`（PEP735）：mypy==2.3.0、ruff==0.16.1（钉死）、pytest、hypothesis、httpx>=2.13.0。无 `[tool.uv]` 段；uv.lock 存在。
- `[project.scripts] enterprise-pdf-rag = "enterprise_pdf_rag.cli:main"`。
- src/enterprise_pdf_rag 顶层：cli.py(17K)、py.typed + 子包 adapters/(65 项, SDK/IO 层)、core/(settings.py+logging.py)、documents/、figures/、processing/、resources/。**无独立 config/services 包**（配置在 core/settings.py，业务在 adapters/）。documents/figures/processing 是**纯领域包**（禁 IO，受 check_architecture 约束）。
- tests：__init__.py、conftest.py、adapters/、documents/、figures/、fixtures/、processing/。~370 个 test_。conftest：`pytest_configure` 若 CWD 无 .project-root 则 `raise UsageError("Run pytest from the repository root")`；**autouse fixture no_network** monkeypatch socket.socket.connect / socket.create_connection → 抛错（离线门）。
- tool：mypy python_version=3.12、`strict=true`、`warn_unreachable=true`、`enable_error_code=["ignore-without-code"]`、`exclude=['^data/']`，无 plugin，**无第三方 ignore_missing_imports 覆盖**；ruff target=py312、`src=["src","tests"]`、select=E4/E7/E9/F/I/UP/**ANN/B/SIM/RUF/TID/BLE/PGH/T20**、`ban-relative-imports="all"`、banned-api 禁 `__future__.annotations`、per-file `scripts/*.py`=[T20]；pytest `addopts="-q --import-mode=importlib"`、testpaths=["tests"]、`filterwarnings=["error"]`。
- 门 = `ci.sh`（唯一门；先 `test -f .project-root`、`export UV_OFFLINE=true`，全部 `uv run --locked`）：ruff format --check . → ruff check --no-fix . → mypy . → check_conformance.py . → check_architecture.py → check_schema.py → check_drift.py → pytest。Makefile check→./ci.sh。
- ci.sh 调用的检查脚本 + 硬编码路径：
  - **check_schema.py**：`ROOT=parent.parent`；读 `ROOT/docs/schemas/{name}.json` 对比 pydantic `model_json_schema`；从 `enterprise_pdf_rag.adapters.http.*` import 模型。契约：chart-qa-v1/v2、figure-api-v1、aia-source-review-v1、openai-demo-v1、aia-processing-v1。
  - **check_architecture.py**：`ROOT=parent.parent`；`PACKAGES=("enterprise_pdf_rag.figures","enterprise_pdf_rag.documents","enterprise_pdf_rag.processing")`；扫 `ROOT/src/<pkg>/**.py`；禁 FORBIDDEN_STDLIB(os/pathlib/io/socket/sqlite3/subprocess/http/urllib/importlib) 与 open/eval/exec/__import__。
  - **check_drift.py**：`ROOT=parent.parent`；rglob `ROOT/**.md` 找 `covers:` 行，校验引用路径 `ROOT/p` 存在；SKIP_DIRS 排 .git/.venv/site-packages。
  - **check_conformance.py**：默认 `.`；`src=root/"src"`；要求 `root/.project-root`；检查 import 规范/hook 安装；注释说自身是 skill 快照。
  - ingest.py：仅 `from enterprise_pdf_rag.cli import main`，无硬编码路径。
- docs/adr：**0001–0010**（0001-architecture … 0010-generic-pdf-ingestion-entry）。
- .github/workflows/ci.yml：**ACTIVE**（push[main]/PR/dispatch，ubuntu-24.04，py3.12，setup-uv 0.6.2）：uv sync --locked → ./ci.sh → plain pip install . + pip check → checkout 外 smoke（APP_ROOT_DIR/APP_DATA_DIR 指向 RUNNER_TEMP，验证 pdfspine/resvg_py/fontTools import + resources json + render_svg_png + create_app().openapi()）。
- .gitignore：`**/data/`（**data 被忽略**）、/.venv/、caches、/output/、/logs/、.env*(!.env.example)、*.pdf/*.png/*.svg、*.key/*.pem。
- **仓库相对路径常量（core/settings.py 及派生）**：
  - `ROOT_DIR = _find_project_root()`：CWD 向上找 `.project-root`；env `APP_ROOT_DIR` 覆盖（wheel/容器逃生口）；找不到 RuntimeError。
  - `Settings.model_config`：yaml_file=`ROOT_DIR/configs/settings.yaml`，env_prefix="APP_"，nested_delimiter="__"。
  - `data_dir=ROOT_DIR/data`（env `APP_DATA_DIR` 覆盖）；`log_dir=ROOT_DIR/logs`（`APP_LOG_DIR`）。模块级导出 `DATA_DIR`、`LOG_DIR`。
  - `resource_path(rel)=files("enterprise_pdf_rag")/rel`（包内资源）。
  - adapters/aia_ingestion.py：`AIA_OUTPUT=DATA_DIR/output/aia-2026-interim`；`AIA_INPUT=DATA_DIR/samples/<spec.filename>`。
  - adapters/aia_candidates.py：`DEFAULT_CATALOG=resource_path("resources/aia-first-20-regions.json")`。
  - cli.py：ingest 默认输出 `APP_DATA_DIR/ingestion`（每文档 SHA 隔离目录）。
  - http/webui_gate.py：暴露 `"DATA_DIR": str(data_dir)`。
  - `configs/settings.yaml` 存在。→ 结论：运行期可写路径都锚 `ROOT_DIR/.project-root` 或 `APP_*` env；**合并后只要 .project-root 与 APP_* 一致即可，不必改代码；但 check_* 脚本硬编码 parent.parent 与 src/<pkg> 需改**。
- data/：**11G**，已 gitignore。子目录 aia-inventory/aia-svg-review/chart-qa-validation/local-models/open-webui-preview/output/packaging-review/samples/validation + 日志。
- git：未提交 **77 项**；HEAD=4e3fd82。根有 CLAUDE.md + AGENTS.md。

---

## 3. 冲突清单

### 3a. 同名文件/目录（合并即撞）
| 路径 | B(ragspine) | A(epr) | 处理 |
|---|---|---|---|
| `pyproject.toml` | v0.13.0 | v0.1.0 | 手工合并（见 §7），A 的不保留 |
| `uv.lock` | 735K | 存在 | 以 B 为基重新 `uv lock` |
| `tests/conftest.py` | rootutils+fixture 再生，**无网络阻断** | pytest_configure 强制 .project-root + **autouse no_network(socket 阻断)** | **不能共存于 tests/ 根**；A 的下沉到 tests/enterprise_pdf_rag/conftest.py（否则 socket 阻断会波及 B 的 redis/qdrant/pg8000/service 测试） |
| `scripts/ci.sh` | 8 步 spine 门 | 8 步 epr 门 | 合并成一个根门（§7）；A 的 ci.sh 逻辑内联进去，不同名保留 |
| `scripts/ingest.py` | narrative/通用 ingest | `epr.cli ingest` 转发 | 改名，如 `scripts/epr_ingest.py` |
| `CLAUDE.md`（根） | 有 | 有 | 合并；保留 B 的家族入口，追加 A 的段落 |
| `AGENTS.md`（根） | **无** | 有 | 直接搬入 B 根 |
| `README.md`（根） | 有 | 有 | 以 B 为主，A 内容并入 docs/enterprise-pdf-rag/ |
| `.github/workflows/ci.yml` | DORMANT | ACTIVE(push/PR) | 用 A 的激活策略更新 B 的 ci.yml；smoke 步骤保留 A 版 |
| `docs/adr/00XX` | 0001–0020 | 0001–0010 | **编号直接撞 0001–0010**：A 的 ADR 重编号为 0021–0030（或落 docs/enterprise-pdf-rag/adr/ 独立编号） |
| `.project-root` | 空锚点 | 空锚点 | 合并后单一根锚点，OK |
| `.python-version` | 无 | 3.12 | 见 §3c 的 requires-python |
| `config/`（顶层目录） | 有 | 无(A 的 config 在 configs/) | 不撞 |
| `configs/` | 无 | settings.yaml | 搬入 B 根 configs/（或 configs/enterprise-pdf-rag/），配合 settings 路径 |
| `data/`（11G） | data/* gitignore(留 golden) | data/ gitignore | 两边都不入 git；**手工 rsync**，见 §6 |

子包名交集：A{adapters,core,documents,figures,processing,resources} ∩ B{agent,cli,common,compat,config,diagnostics,dify,eval,extraction,fixtures,graph,ingestion,n8n,pipeline,retrieval,service,storage,workflows} = **空**（方案甲零撞）。方案乙下唯一撞：A 的 `cli.py`(模块) vs B 的 `cli/`(包)。

### 3b. tool 配置差异
- **ruff**：B 宽(E/F/I/W/UP/B, ignore E501/UP042, 仅 src/ragspine 跑)；A 严(+ANN/B/SIM/RUF/TID/BLE/PGH/T20, ban-relative-imports=all, 禁 __future__.annotations, 跑 src+tests)。→ 统一成一份 `ruff check .` 会让 B 的 src 爆 ANN/SIM/TID/BLE/T20（B 从没启用过）。**需按目录分级**：全局用 A 的严格集，`per-file-ignores` 对 `src/ragspine/**`、`tests/<ragspine 子树>/**` 关掉 ANN/SIM/TID/BLE/PGH/T20（等于维持 B 现状）。
- **mypy**：B strict + files=["src/ragspine"] + 无 warn_unreachable/ignore-without-code + ~30 第三方 override；A strict + warn_unreachable + ignore-without-code + exclude ^data/ + 无 override。→ 合并 files 加 `src/enterprise_pdf_rag`；**warn_unreachable/ignore-without-code 若跨到 B 会爆新错**，用 `[[tool.mypy.overrides]] module="ragspine.*"` 关掉这两项；两边 ignore_missing_imports 取并集。
- **pytest**：都 filterwarnings=["error"]。合并须保留 B 的 3 条第三方 ignore（否则 B 测试炸），markers 取并集(gpu/docling/network)。A 用 `--import-mode=importlib`，B 用默认(prepend)+rootutils pythonpath → **import mode 冲突**：强上 importlib 可能破坏 B 的 rootutils.setup_root 假设。建议门里对两套测试分别 invoke，或 B 子树保留默认。
- **requires-python**：见 3c。

### 3c. 版本/约束冲突（重点）
- **requires-python**：B `>=3.12`(支持 3.12/3.13/3.14，CI 三版本) vs A `>=3.12,<3.13`。合并后取交集 = `>=3.12,<3.13` → **把 B 从 3.12–3.14 砍到只剩 3.12，破坏 B 的 CI matrix**。除非把 A 的原生钉死升到有 3.13/3.14 wheel 的最新版（见 §4）。**头号阻塞**。
- **pdfspine**：B `>=0.0.4`(lock **陈旧** 0.4.0) vs A `==0.11.0`。**PyPI 最新=0.11.0，即两边实际目标一致**；re-lock 后 B 也到 0.11.0，**无真实版本冲突**（B 的 pdf 是可选延迟 import，仍建议回归 B 的抽取路径确认 0.4→0.11 加法无碍）。
- **httpx vs httpx2**：B 用 `httpx`(>=0.27)，A dev 用 `httpx2`(>=2.13.0，Tom Christie 的 httpx 2.x 分支包，**不同名不同分发**)。非版本冲突，合并须统一 HTTP 客户端选型（建议全仓统一到 httpx 或 httpx2 之一）。
- **mypy**：B `>=1.18`(lock 2.1.0) vs A `==2.3.0` → 统一到最新（2.x 对 B 已 OK）。
- **ruff**：B `>=0.14`(lock 0.15.17) vs A `==0.16.1` → 统一到最新。
- **pydantic**：B `>=2.0`(lock 2.13.4) vs A `>=2.12,<3` → 统一 `>=2.12,<3`，兼容。
- **beartype**：B `>=0.18`(lock 0.22.9) vs A 无约束 → 取 B。
- **corespine**：仅 B（editable ../corespine）；A 不用 → 保留 B。
- **resvg-py / fonttools**：仅 A（均钉死）→ 升最新（关系到 <3.13 能否解锁）。
- **httpx**：B 多处 `>=0.27` vs A dev `>=2.13.0`（注：A 写的是 httpx>=2.13？疑为 httpx 版本笔误或不同包，见 dep 表核对）。

---

## 4. 依赖升级表（PyPI 实时，2026-09-19；48 唯一包全表见 `dep-versions.md`）

### 需 major 升级的（**全部在 ragspine 侧，全在 optional-extras/dev**；epr 侧 pin 已=最新/较新）
| 包 | ragspine 约束 | 最新 | 跳变 | 所属 extra |
|---|---|---|---|---|
| python-pptx | >=0.6.23 | 1.0.2 | 0→1 | base |
| pytest | >=8.0.0 | 9.1.1 | 8→9 | dev |
| reportlab | >=4.0 | 5.0.1 | 4→5 | dev |
| pdoc | >=14 | 16.0.0 | 14→16 | dev |
| mypy | >=1.18 | 2.3.1 | 1→2 | dev（epr 已 2.3.0） |
| twine | >=5.0 | 7.0.0 | 5→7 | dev |
| transformers | >=4.40（darwin<5.9.0） | 5.17.0 | 4→5 | tsr/pdf-docling |
| anthropic | >=0.40.0 | 1.7.0 | 0→1 | llm |
| openai | >=1.0 | 3.16.2 | 1→3 | llm |
| sentence-transformers | >=3.0 | 6.1.0 | 3→6 | embed |
| pillow | >=10.0 | 12.3.0 | 10→12 | tsr |
| pandas | >=2.0 | 3.0.6 | 2→3 | graphrag-compat |
| pyarrow | >=14.0 | 25.0.1 | 14→25 | graphrag-compat |
| rq | >=1.16 | 2.12.0 | 1→2 | service |
| redis | >=5.0 | 8.1.0 | 5→8 | service |

### 两边共用/关键包（无 major 或已一致）
| 包 | ragspine | epr | 最新 | 说明 |
|---|---|---|---|---|
| **pdfspine** | >=0.0.4(lock 陈旧 0.4.0) | ==0.11.0 | **0.11.0** | **两边其实一致=最新**，re-lock 后 ragspine 也到 0.11.0，**无真实冲突**；0.x alpha，近期为加法(Table.slots/get_paint_profile) |
| pydantic | >=2.0 | >=2.12,<3 | 2.13.5 | 交集 `>=2.12,<3`；epr `<3` 挡未来 v3 |
| beartype | >=0.18 | 无 | 0.22.9 | 0.20+ 容器深检可查出潜伏类型错，一般安全 |
| ruff | >=0.14 | ==0.16.1 | 0.16.8 | 见下 breaking |
| corespine | >=0.1.1(本地 path) | — | 0.5.1 | 仅 ragspine，`../corespine` editable；PyPI 亦有 0.5.1 |
| docspine/pptspine | >=0.1.0 | — | 0.5.1 | 仅 ragspine，公有 PyPI(本地工作树仍标 0.0.1 未发) |
| fastapi/uvicorn | >=0.110/>=0.27 | 无 | 0.141.1/0.53.0 | 两边共用 |
| **httpx / httpx2** | httpx>=0.27 | **httpx2>=2.13.0** | 0.28.1 / 2.13.0 | **不同包**（httpx2=Tom Christie 的 httpx 2.x 分支），合并须统一选型 |
| resvg-py / fonttools | — | ==0.5.0 / ==4.65.0 | 0.5.0 / 4.65.0 | 仅 epr，均已=最新；<3.13 之谜的原生依赖 |

### 重点 breaking 摘要
- **pydantic**：无 major，皆 2.x，仅 minor，无破坏动作（v1→v2 破坏早已发生）。
- **ruff**：0.x 无 major，但 **0.16.0 默认规则 59→413 条**、移除 18 条 E/F 默认(E401/402/7xx/711-714/721/731/741-743/F403/405/406/722)、JSON location 可为 null → unpinned 必爆红。epr 已 pin 0.16.1，ragspine `>=0.14` 浮到 0.16.x 须显式配 `select`。
- **mypy**：ragspine **1→2**（epr 已 2.3.0）。2.0 默认开 `--local-partial-types`+`--strict-bytes`(PEP688，bytearray/memoryview 不再赋给 bytes)、`--allow-redefinition` 语义变、丢弃 `--python-version 3.9`(要 3.10+)。两边 strict=True，升级后易冒新错。
- **beartype**：0.x 无 major，0.20 起对容器做 O(1) 深度运行时检查——潜伏类型不符会在 import/调用期被查出；升 0.22.9 一般安全。
- **pdfspine**：alpha/pre-1.0（public API/on-disk 格式仍可能变），**两边已一致 0.11.0=最新**，合并无需动；风险已由 epr 的 pin 承担。
- **corespine**：pre-1.0 无 CHANGELOG，走本地 editable，实际=磁盘代码(0.5.1)，随 monorepo 同步管控。
- 其它 major 包（anthropic 0→1、openai 1→3、transformers 4→5、pandas 2→3、pyarrow 14→25、rq 1→2、redis 5→8、pytest 8→9 等）均为常规大版本，需在 re-lock 后跑门回归；因全在 ragspine 的 optional-extras，仅装了对应 extra 的测试受影响。

---

## 5. 目标布局（两方案）

### 方案甲：enterprise_pdf_rag 作为独立顶层包（推荐）
文件落点（均在 B 根 `/Users/linhan/startup/spine/ragspine/`）：
- `src/enterprise_pdf_rag/`（整包搬入，**import 零改动**）。
- `tests/enterprise_pdf_rag/`（A 的 tests/* 搬入，**带自己的 conftest.py**，no_network autouse 只作用于本子树）。
- `docs/enterprise-pdf-rag/`（A 的 docs 搬入）；ADR → `docs/enterprise-pdf-rag/adr/`（保留 0001–0010 本地编号，避开 B 的 0001–0020）。
- `docs/schemas/` 或 `docs/enterprise-pdf-rag/schemas/`（chart-qa 等契约 JSON；配合 check_schema.py 路径改动）。
- scripts：A 的检查脚本加前缀搬入——`scripts/epr_check_schema.py`、`scripts/epr_check_architecture.py`、`scripts/epr_check_drift.py`、`scripts/epr_check_conformance.py`、`scripts/epr_ingest.py`；`ci.sh` 内联进 B 的门。
- `configs/settings.yaml` → B 根 `configs/`（B 已有 config/ 目录，注意 configs vs config 区分；或放 configs/enterprise-pdf-rag/）。
- `benchmarks/aia-2026-interim/`、`resources`（随包）。
- pyproject 声明两个 wheel 包：
  ```toml
  [tool.hatch.build.targets.wheel]
  packages = ["src/ragspine", "src/enterprise_pdf_rag"]
  ```
  （A 的资源随目录带入；A 的 `[project.scripts]` 合并为第二个 entry point。）
- 工作量：**小**。import 不动；主要是 tests/docs/scripts 重排 + pyproject/门/tool 分级 + 检查脚本路径改。风险集中在 tool 统一（§3b）与 requires-python（§3c）。

### 方案乙：并入 ragspine.* 命名空间
两种子形：
- 乙-1 平铺到顶层：`enterprise_pdf_rag.adapters→ragspine.adapters`、documents/figures/processing/core/resources 同理。子包名与 B 唯一冲突 = `cli`（A cli.py 模块 vs B cli/ 包）→ A 的 cli 需并入 B 的 cli/ 或改名 `ragspine.epr_cli`。
- 乙-2 单一子包：`enterprise_pdf_rag.*→ragspine.epr.*`（零命名冲突，单前缀替换）。
- 改 import 工作量：`grep -rl "enterprise_pdf_rag"` = **180 个 .py**（src 91 / tests 82 / scripts 7）、**853 行 import/from**；另 pyproject.toml、scripts/start.sh、.github/workflows/ci.yml。
- 连带改：check_architecture.py 的 `PACKAGES=("enterprise_pdf_rag.figures",…)`→ragspine.*；check_schema.py 的 `import enterprise_pdf_rag.adapters.http.*`→ragspine.*；`[project.scripts]`、resource_path("enterprise_pdf_rag")、beartype import-hook 目标包名。
- 工作量：**大**（180 文件机械替换 + 上述连带 + 全量回归）。收益：单一命名空间、更符合家族风格。**建议作为甲落地并 CI 绿后的后续 ADR，不在首次合并做**。

---

## 6. git 策略
现状：A 未提交 77 项 + 11G gitignore data；B main 干净、仅 main 分支、有 GitHub remote。
建议流程（保留历史版）：
1. **A 先落一个 commit**：交接文档要求"保留全部工作树改动"→ 在 A 上 `git checkout -b merge-snapshot && git add -A && git commit`（不推 origin，仅为可被 subtree 捕获；worktree 内容不丢）。
2. **B 开合并分支**：`cd B && git checkout -b merge/enterprise-pdf-rag`（**不在 main 上直接做**，CI 绿前 main 保持安全）。
3. **subtree 带历史引入**：`git subtree add --prefix=_incoming/epr <A路径> merge-snapshot`（把 A 全树落到 _incoming/epr，保留历史）。
4. **git mv 重排** 到方案甲落点，`git rm` 掉不要的（A 的 pyproject.toml、A 的 ci.sh、A 的 uv.lock、重复 README/根 CLAUDE 片段），删空的 _incoming。
5. **手工搬 11G data**（gitignore，不进 subtree）：`rsync -a A/data/ B/data/enterprise-pdf-rag/`（放子目录避开 B 的 data/fixtures、data/golden；再设 `APP_DATA_DIR` 或改 A 的 data_dir 默认为 `ROOT_DIR/data/enterprise-pdf-rag`）。configs、benchmarks 同法。
6. 重建 lock：`cd B && uv lock`（含升级，§4）。
7. 跑合并后的门；绿后再 PR 合入 B main。
简化备选：若不在意 A 的细粒度历史（A 处 dev 期、worktree 未提交），直接 `rsync` 源码到甲落点，一次 squashed commit 注明 `epr@4e3fd82`——省去 subtree 的 prefix/mv 折腾，但丢历史。
注意：B main 是否受保护未知（远程仅 main）→ 走新分支 + PR 最稳。

---

## 7. 门禁统一
合并后单门（B 的 `scripts/ci.sh` 扩展）：
1. B 段：check_docstring_refs → check_doc_drift → mypy(src/ragspine，或合并 files) → ruff check src/ragspine → pytest(spine 子树, `-m "not gpu and not docling and not network"`) → pytest docling → run_qa_eval → run_demo。
2. A 段（内联，全 `uv run --locked`）：ruff format --check + ruff check（对 A 子树严格集）→ epr_check_conformance → epr_check_architecture → epr_check_schema → epr_check_drift → pytest tests/enterprise_pdf_rag。
- A 检查脚本要改的路径假设：
  - check_architecture.py：`ROOT=parent.parent` 合并后仍=B 根(scripts/ 在根)，OK；但 `PACKAGES` 全名（方案甲不变，方案乙改 ragspine.*）；扫的 `ROOT/src/<pkg>` 甲下仍对。
  - check_schema.py：`ROOT/docs/schemas` → 若 schema 落 docs/enterprise-pdf-rag/schemas 则改此常量；import 路径甲下不变。
  - check_drift.py：rglob 全仓库 → 合并后会扫到 B 的 docs；B 的 md 无 `covers:` 故无害，但确认 SKIP_DIRS 含 B 的 docs/generated、studio、deploy。
  - check_conformance.py：是 skill 快照、硬编码 src/ 与 import 规范 → **scope 到 src/enterprise_pdf_rag**，否则会拿 A 的规范（如禁相对 import）判 B 的代码，B 未必合规。
- ruff/mypy 分级：见 §3b（per-file-ignores 关 B 严格项；mypy overrides 对 ragspine.* 关 warn_unreachable/ignore-without-code；ignore_missing_imports 并集）。
- pytest：合并 filterwarnings（error + B 的 3 条 ignore）、markers 并集；import-mode 冲突 → 门里对两子树分别 `uv run pytest tests/<spine>` 与 `pytest tests/enterprise_pdf_rag`（后者 importlib），规避全局切换。
- **mypy strict 跨两包风险点**：A 的 warn_unreachable + ignore-without-code 若作用到 B 的 18 子包 src（从未跑过这两项）→ 会冒出成片 unreachable/缺 code 的 type:ignore 报错。务必用 overrides 把这两项限制在 `enterprise_pdf_rag.*`。反向：A 无第三方 ignore_missing_imports，合并后统一 mypy 若扫到 A 引的第三方（resvg_py/fonttools 等）需补 override。

---

## 最大风险 Top3
1. **requires-python `<3.13`**：A 的原生钉死(pdfspine==0.11.0/resvg-py==0.5.0/fonttools==4.65.0)把整仓交集砍到只支持 3.12，摧毁 B 的 3.13/3.14 支持与 CI matrix。**待确认**：这三者(尤其 resvg-py 0.5.0、pdfspine 0.11.0 的 Rust wheel)是否出 3.13/3.14 wheel（fonttools 纯 py 无碍）。出→保留 `>=3.12`；不出→B 被迫降级到仅 3.12。合并前必须与用户确认取舍。
2. **依赖统一升级的门禁回归面**（不是版本互斥，而是量）：15 个 major 跳变全落 ragspine 侧(anthropic 0→1、openai 1→3、transformers 4→5、pandas 2→3、pyarrow 14→25、mypy 1→2、pytest 8→9、rq 1→2、redis 5→8…) + ruff 0.16 默认规则大改 + mypy 2.x 新默认(strict-bytes 等)，一次 `uv lock` 升级后须跑通全门；虽都在 optional-extras、只影响装了对应 extra 的测试，但 B 的 dev 工具(ruff/mypy/pytest)升级会波及整门。corespine 走本地 path source，脱离 spine monorepo 时需决定 PyPI vs 本地。
3. **门禁与 tool 统一的连锁爆炸**：ruff 严格集(ANN/SIM/TID/BLE/T20) + mypy(warn_unreachable/ignore-without-code) 一旦无差别套到 B 的 src，会产生成百 lint/type 报错；conftest 的 no_network autouse 若留在 tests/ 根会阻断 B 的 redis/qdrant/pg8000/service 测试；check_conformance(禁相对 import 等) 套到 B 未必合规。必须目录分级 + conftest 下沉 + 检查脚本 scope 到 A 子树，否则门永远红。
