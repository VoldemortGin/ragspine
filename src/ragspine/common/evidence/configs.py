"""全局配置、常量与路径的唯一来源:环境变量 + 项目根 .env + config/enterprise-pdf-rag/settings.yaml。

根锚点 ROOT_DIR、运行期可写目录 DATA_DIR / LOG_DIR、包内资源定位 resource_path()
全部在此定义;其他模块一律从这里导入,不要自己再算一次项目根。

beartype 叶子约束:本模块不得 import 任何本项目内、希望被检查的模块。
只依赖标准库 + pydantic / pydantic-settings(第三方依赖不受叶子约束限制)。
"""

import json
import os
from collections.abc import Iterable
from functools import lru_cache
from importlib.resources import files
from pathlib import Path
from typing import Literal

from pydantic import AliasChoices, Field, SecretStr, field_validator
from pydantic_settings import (
    BaseSettings,
    EnvSettingsSource,
    PydanticBaseSettingsSource,
    SettingsConfigDict,
    YamlConfigSettingsSource,
)


def _find_project_root() -> Path:
    """从 CWD 逐级向上找标记文件 .project-root,定位仓库根。

    用专用标记而不是 pyproject.toml:monorepo 下每个子包都有 pyproject.toml,
    向上找会停在子包根;.project-root 只放在仓库根,"向上找第一个"天然停对地方。

    找不到即抛错,绝不静默退回 Path.cwd() —— 那会让 ROOT_DIR 变成"你碰巧在哪"。
    非源码部署(wheel / 容器)或需要在项目树外运行时,用 APP_ROOT_DIR 显式指定。
    注意 APP_ROOT_DIR 不是 Settings 的字段(ROOT_DIR 在 Settings 实例化之前就要用),
    只能在这里直接 os.getenv 读;前缀 APP_ 只是与 Settings 的 env_prefix 保持一致。

    APP_ROOT_DIR 只校验「存在且是目录」,**不要求它含 .project-root**:
    wheel / 容器部署里根本没有那个标记文件(它只在仓库根、不进 wheel),
    这正是这个逃生口存在的意义。但路径本身写错必须当场炸——否则 config/enterprise-pdf-rag/ 读不到、
    data/ 与 logs/ 悄悄写去别处,配置静默退化成一堆默认值而没人发现。
    """
    if env_root := os.getenv("APP_ROOT_DIR"):
        root = Path(env_root).resolve()
        if not root.is_dir():
            raise RuntimeError(
                f"APP_ROOT_DIR={env_root!r} 指向的路径不存在或不是目录"
                f"(解析为 {root})。\n请把它设成一个已存在的目录的绝对路径"
                "——ROOT_DIR 由它决定,config/enterprise-pdf-rag/ data/ logs/ 都相对它解析。"
            )
        return root
    cwd = Path.cwd()
    # cwd 自身必须参与匹配:在仓库根运行时,锚就在当前目录而不在 parents 里
    for parent in [cwd, *cwd.parents]:
        if (parent / ".project-root").is_file():
            return parent
    raise RuntimeError(
        f"未找到仓库根标记文件 .project-root(已从 {cwd} 逐级向上找到文件系统根)。\n"
        "请在项目树内运行,或设 APP_ROOT_DIR=<已存在的工作目录绝对路径>。"
    )


ROOT_DIR: Path = _find_project_root()


def _dotenv_disabled() -> bool:
    # 与 python-dotenv 同一约定:受控子进程(local_model_launcher / webui_preview / webui_gate)
    # 都设了 PYTHON_DOTENV_DISABLED=1,它们只看父进程显式交给的白名单环境,不再自己读 .env。
    value = os.getenv("PYTHON_DOTENV_DISABLED", "").strip().lower()
    return value in {"1", "true", "t", "yes", "y"}


def _resolve_directory(value: Path) -> Path:
    path = value.expanduser()
    return (path if path.is_absolute() else ROOT_DIR / path).resolve()


def _drop_blank_primary_questions_path(
    source: PydanticBaseSettingsSource,
) -> PydanticBaseSettingsSource:
    """让留空的 ``NB_QUESTIONS_PATH=`` 不挡住别名 ``DATASET_PATH``(把它当作未设置)。

    pydantic-settings 在同一来源内取别名列表里「第一个存在」的名字,空串也算存在,
    于是 ``NB_QUESTIONS_PATH=`` 会让 ``DATASET_PATH`` 永远没机会回落。这里在取值前把来源里
    空白的主名剔掉(真实环境变量与 .env 两个来源同样处理);键名大小写按来源自身配置而定,故用 casefold 比较。
    """
    if isinstance(source, EnvSettingsSource) and isinstance(source.env_vars, dict):
        primary = "NB_QUESTIONS_PATH".casefold()
        blank = [
            k
            for k, v in source.env_vars.items()
            if k.casefold() == primary and not (v or "").strip()
        ]
        for name in blank:
            del source.env_vars[name]
    return source


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="APP_",  # APP_IS_DEBUG、APP_BEARTYPE_ON ...
        env_nested_delimiter="__",  # APP_RETRIEVER__TOP_K 覆盖嵌套字段
        yaml_file=ROOT_DIR / "config" / "enterprise-pdf-rag" / "settings.yaml",
        # 固定在项目根,不随 CWD 变(notebook 的 CWD 通常是子目录);文件不存在时静默跳过。
        env_file=ROOT_DIR / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
        # 带 validation_alias 的字段(LLM 三项的 APP_LLM_* 别名、NB_*)仍可按字段名构造。
        populate_by_name=True,
    )

    is_debug: bool = False  # 日志级别、prompt 缓存等(与 beartype 解耦)
    beartype_on: bool = True  # 运行时类型检查总开关;仅生产设 APP_BEARTYPE_ON=false

    # Explicit application mode: no implicit mock fallback.
    execution_mode: Literal[
        "unconfigured", "offline-demo", "production", "aia-source-review", "document-catalog"
    ] = "unconfigured"

    # 运行期可写目录(默认锚定项目根;部署可用 APP_*_DIR 覆盖)
    data_dir: Path = ROOT_DIR / "data"
    log_dir: Path = ROOT_DIR / "logs"

    # 通用入库/文档目录根;None → data_dir / "ingestion"(与 ingest 默认输出一致)
    ingestion_dir: Path | None = None
    # "onnx-layout" 版面策略(ADR 0030)的 PP-DocLayoutV3 模型文件路径,也可以填模型所在目录
    # (按默认文件名 pp_doc_layoutv3.onnx 拼接)。未设或空串 → 退回环境变量 PDFSPINE_ONNX_MODELS。
    # 权重不随任何 wheel 分发;Databricks 上放 Volume 并填绝对路径。
    onnx_layout_model: str | None = None
    # 兼容根:每项是一个 processing store 根,其父目录即 source store 根;默认空
    legacy_document_roots: tuple[Path, ...] = ()
    # document-catalog 聊天端点整个进程的模型真实调用预算(缓存回放不计;用尽即 503)
    answer_max_live_calls: int = 200
    # 回答模型的采样种子。温度恒为 0(贪心),种子只是在支持它的 provider 上把剩下的
    # 那点抖动也钉死;None → 不发 seed 字段(provider 不认识它时用)。采样参数进入请求
    # 指纹,所以改这个值等于换一份补全缓存——这是有意的,一次发布只该有一种采样。
    answer_seed: int | None = 0
    # 审计开关:每个请求都重做挂载时的全量校验(重读并核对钉死发布的每一个资产摘要、
    # 重新解析整个向量索引)。默认关闭——挂载时完整校验一次,之后每次请求只比对钉死
    # 清单这一个文件的摘要,漂移照样拒绝。打开会让真实文档的一次问答慢一个数量级。
    # 它同时是 LocalDocumentStore 的 verify_every_load 默认值:打开后入库流程每次读取都
    # 全量校验,不复用本存储实例已校验过的快照与对象摘要。
    verify_every_request: bool = False
    # 持久化校验凭据(enterprise-pdf-rag ADR 0034):某个存储实例亲自 stat + 读 + 哈希过一份
    # 不可变快照的全部文件后,在快照目录 verification-receipts/ 下记一张凭据;之后的新实例 /
    # 新进程只要凭据完好、清单与文件集合相同、每个文件的 size / mtime / ctime 都没变,就跳过
    # 整份快照的重读。关掉(false)即回到 ADR 0024:每个实例各全量读一遍;verify_every_request
    # 打开时凭据也一律不用。最终发布(publish_draft)与所有消费性读取始终真读。
    verify_persisted_receipts: bool = True
    # 单次模型调用的等待上限(秒)。默认与 JsonCompletionClient 自身的默认一致;页级上下文
    # (ADR 0017)让"总结某一节"这类问题的 prompt 与生成都更长,超过默认即 503,所以它可配。
    # 上限 180 与该客户端的构造校验一致。
    answer_timeout_seconds: float = 45.0
    # 问答审计库(adapters/answer_audit):每次回答开一行,记下送进模型的最终 prompt 原文
    # 与这次问答的来龙去脉。它是本地回溯用的文件,含证据正文,不外发;写失败只告警。
    answer_audit_enabled: bool = True
    # None → <ingestion_root>/answers-audit.sqlite;给绝对路径即用它。
    answer_audit_path: Path | None = None

    # ---- 对象库后端(common/evidence/object_backend;sqlite 对象库 PR-1)----------------
    # 存储后端:auto(探测可用即 sqlite,否则回退文件布局)| sqlite(显式;探测失败即
    # 报错,绝不静默回退)| files(今天的文件布局)。
    # 注意:PR-1 只新增后端包,没有任何调用方读本字段——默认 auto 要等 PR-2/3 把
    # store / 模型缓存接到 open_backend 之后才会实际改变行为;在那之前全部路径仍走
    # 现有文件布局,行为与字节逐位不变。
    object_store_backend: Literal["auto", "sqlite", "files"] = "auto"
    # 内联进 db 的对象大小上限(字节);更大的对象外置到 objects/sha256-sharded/。
    object_store_inline_max_bytes: int = 262_144
    # 无论大小一律外置的媒体类型(逗号分隔;PDF 原件永远留文件系统)。
    object_store_external_media_types: str = "application/pdf"
    # sqlite 的 PRAGMA synchronous:FULL(默认,最稳)或 NORMAL(WAL 下可降)。
    object_store_synchronous: Literal["FULL", "NORMAL"] = "FULL"
    # 单个 store db 的保护阈(字节;默认 400 MiB,Workspace 单文件 500 MB 限制之下):
    # 超过后 > 16 KiB 的新对象一律外置。
    object_store_max_db_bytes: int = 400 * 1024 * 1024

    # notebook / 一键流程(run_folder_pipeline)的输入输出位置,环境变量名不带 APP_ 前缀。
    # 都可缺省且不校验存在性;相对路径相对项目根,~ 展开;空串视为未设置。
    pdf_source_dir: Path | None = Field(default=None, validation_alias="NB_PDF_DIR")
    questions_path: Path | None = Field(
        default=None, validation_alias=AliasChoices("NB_QUESTIONS_PATH", "DATASET_PATH")
    )
    report_dir: Path | None = Field(default=None, validation_alias="NB_REPORT_DIR")
    # 口令加密 PDF 的打开口令(入库与之后每次重新打开源 PDF 都用它);环境变量名不带 APP_ 前缀,
    # 与 SuperIndex 同名。空串视为未设置;SecretStr 保证不进 repr / 日志 / 报告。
    pdf_ingest_password: SecretStr | None = Field(
        default=None, validation_alias="PDF_INGEST_PASSWORD"
    )
    # 整本 PDF 抽取(extract_document)逐页并行的线程数;None → min(4, CPU 数),1 → 串行。ADR 0040。
    pdf_extract_workers: int | None = Field(default=None, ge=1, le=64)

    # 模型与 SSH 隧道。全部可缺省:import / 构造时不校验,哪一组缺失或不合法,只在真正用到
    # 那一组的阶段报错(providers.load_*_config / local_model_tunnel.load_tunnel_config)。
    # 端口也保持字符串,由隧道加载器做原有的严格校验。
    # 云端 OpenAI 兼容 LLM。这三项以 OPENAI_* 为首选名、APP_LLM_* 为别名,逐字段取值(别名
    # 顺序即同一来源内的优先级;别名不带 env_prefix,须写全名)。embedding 模型名同理以
    # OPENAI_EMBEDDING_MODEL 为首选名;embedding 其余两项、rerank、隧道没有别名。
    llm_api_key: SecretStr | None = Field(
        default=None, validation_alias=AliasChoices("OPENAI_API_KEY", "APP_LLM_API_KEY")
    )
    llm_base_url: str | None = Field(
        default=None, validation_alias=AliasChoices("OPENAI_BASE_URL", "APP_LLM_BASE_URL")
    )
    llm_model: str | None = Field(
        default=None, validation_alias=AliasChoices("OPENAI_MODEL", "APP_LLM_MODEL")
    )
    # 采样温度:不设 / 留空 → 0.0(贪心);数值 → 发该值;omit → 请求体不带 temperature。
    # 与其余模型字段一样原样保存、import 时不校验,由 providers.load_llm_config 校验。
    llm_temperature: str | None = Field(
        default=None, validation_alias=AliasChoices("OPENAI_TEMPERATURE", "APP_LLM_TEMPERATURE")
    )
    # embedding 默认与 LLM 共用 OPENAI_BASE_URL 网关(只需 OPENAI_EMBEDDING_MODEL);
    # 设了 APP_EMBEDDING_BASE_URL 才是独立的 loopback 服务。解析规则见 as_environment。
    embedding_api_key: SecretStr | None = None
    embedding_base_url: str | None = None
    embedding_model: str | None = Field(
        default=None,
        validation_alias=AliasChoices("OPENAI_EMBEDDING_MODEL", "APP_EMBEDDING_MODEL"),
    )
    rerank_api_key: SecretStr | None = None  # 本地 rerank(loopback)
    rerank_base_url: str | None = None
    rerank_model: str | None = None
    tunnel_ssh_host: str | None = None
    tunnel_ssh_port: str | None = None
    tunnel_embedding_local_port: str | None = None
    tunnel_embedding_remote_port: str | None = None
    tunnel_embedding_remote_container: str | None = None
    tunnel_rerank_local_port: str | None = None
    tunnel_rerank_remote_port: str | None = None
    tunnel_rerank_remote_container: str | None = None

    @field_validator("answer_timeout_seconds")
    @classmethod
    def bounded_answer_timeout(cls, value: float) -> float:
        # Same window ``JsonCompletionClient`` enforces; rejected here so a bad environment
        # fails at startup instead of when the first question arrives.
        if not 0 < value <= 180:
            raise ValueError("answer_timeout_seconds must be within (0, 180]")
        return value

    @field_validator("data_dir", "log_dir")
    @classmethod
    def resolve_runtime_directory(cls, value: Path) -> Path:
        return _resolve_directory(value)

    @field_validator("pdf_source_dir", "questions_path", "report_dir", mode="before")
    @classmethod
    def blank_path_is_unset(cls, value: object) -> object:
        # `NB_PDF_DIR=` 留空不能解析成项目根(整个仓库会被当成 PDF 目录)。
        return None if isinstance(value, str) and not value.strip() else value

    @field_validator("pdf_ingest_password", mode="before")
    @classmethod
    def blank_pdf_password_is_unset(cls, value: object) -> object:
        return None if isinstance(value, str) and not value.strip() else value

    @field_validator("embedding_api_key", "embedding_base_url", "embedding_model", mode="before")
    @classmethod
    def blank_embedding_is_unset(cls, value: object) -> object:
        # 旧版 .env.example 留了 `APP_EMBEDDING_BASE_URL=` 空行;空串若算"已设",就永远走不到网关回退。
        return None if isinstance(value, str) and not value.strip() else value

    @field_validator("llm_temperature", mode="before")
    @classmethod
    def blank_temperature_is_unset(cls, value: object) -> object:
        # yaml 里写的数值也收成字符串,统一交给 load_llm_config 解析。
        if isinstance(value, int | float) and not isinstance(value, bool):
            return str(value)
        return None if isinstance(value, str) and not value.strip() else value

    @field_validator("ingestion_dir", "pdf_source_dir", "report_dir")
    @classmethod
    def resolve_optional_directory(cls, value: Path | None) -> Path | None:
        return None if value is None else _resolve_directory(value)

    @field_validator("answer_audit_path", "questions_path")
    @classmethod
    def resolve_optional_file(cls, value: Path | None) -> Path | None:
        return None if value is None else _resolve_directory(value)

    @field_validator("legacy_document_roots")
    @classmethod
    def resolve_legacy_roots(cls, value: tuple[Path, ...]) -> tuple[Path, ...]:
        return tuple(_resolve_directory(item) for item in value)

    @property
    def ingestion_root(self) -> Path:
        return self.ingestion_dir if self.ingestion_dir is not None else self.data_dir / "ingestion"

    @property
    def answer_audit_file(self) -> Path:
        if self.answer_audit_path is not None:
            return self.answer_audit_path
        return self.ingestion_root / "answers-audit.sqlite"

    def as_environment(self, names: Iterable[str]) -> dict[str, str]:
        """把已配置的字段还原成 ``APP_*`` 环境变量(值已按 环境变量 > .env > yaml 合并)。

        供按名字取值的加载器与显式白名单的子进程环境使用;未配置的名字不出现。
        键名恒为 ``APP_*``(LLM 三项虽以 ``OPENAI_*`` 为首选名,这里仍以 ``APP_LLM_*`` 作答,
        子进程白名单与 Open WebUI 隔离依赖它)。返回值含密钥明文,只能交给加载器或子进程环境,不要打印或记录。

        ``APP_EMBEDDING_BASE_URL`` / ``APP_EMBEDDING_API_KEY`` 给的是**解析后**的值:未设独立的
        ``APP_EMBEDDING_BASE_URL`` 而设了 embedding 模型名时,embedding 共用 LLM 网关,两者分别
        答 LLM 的 base URL 与(未单独设 embedding key 时)LLM key。子进程因此拿到完整的 embedding 组。
        """
        environment: dict[str, str] = {}
        for name in names:
            field = name.removeprefix("APP_").lower()
            if not name.startswith("APP_") or field not in type(self).model_fields:
                raise KeyError(name)
            value = self._resolved(field)
            if value is None:
                continue
            if isinstance(value, SecretStr):
                environment[name] = value.get_secret_value()
            elif isinstance(value, tuple):
                environment[name] = json.dumps([str(item) for item in value])
            else:
                environment[name] = str(value)
        return environment

    def _configured(self, field: str) -> object:
        return getattr(self, field) if field in self.model_fields_set else None

    def _resolved(self, field: str) -> object:
        value = self._configured(field)
        shares_gateway = (
            self._configured("embedding_base_url") is None
            and self._configured("embedding_model") is not None
        )
        if value is None and shares_gateway:
            if field == "embedding_base_url":
                return self._configured("llm_base_url")
            if field == "embedding_api_key":
                return self._configured("llm_api_key")
        return value

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        # 优先级从高到低:构造参数 > 环境变量 > 项目根 .env > yaml > secrets。
        # PYTHON_DOTENV_DISABLED 为真时跳过 .env(见 _dotenv_disabled)。
        # 末位的 file_secret_settings 只有在 model_config 设了 secrets_dir 时才读文件;
        # 本模板没设,所以它当前是 no-op —— 留在链尾是为了「要用 Docker secrets
        # 时只需加一行 secrets_dir」,而不是它现在在起作用。
        dotenv = (
            () if _dotenv_disabled() else (_drop_blank_primary_questions_path(dotenv_settings),)
        )
        return (
            init_settings,
            _drop_blank_primary_questions_path(env_settings),
            *dotenv,
            YamlConfigSettingsSource(settings_cls),
            file_secret_settings,
        )


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """配置的访问器:进程级单例,但**可测**。

    运行期读配置的代码一律走这里,别捕获模块级的 `settings`——那个实例在 import
    期就冻结了,测试里 `monkeypatch.setenv(...)` 对它毫无作用,于是「不同配置 /
    不同 provider」这一整类分支根本测不到。

    测试要换配置:设好环境变量后 `get_settings.cache_clear()`,下次调用即按新环境
    重建(模板 tests/conftest.py 的 override_settings fixture 就是这么做的)。
    """
    return Settings()


settings = get_settings()
"""模块级单例:专供 import 期使用者。

包 `__init__.py` 的 beartype hook 在 import 期读 `settings.beartype_on`,那时既不
可能也不需要覆盖配置,用单例最省事。**运行期**的读取请改用 `get_settings()`。
"""

DATA_DIR: Path = settings.data_dir
LOG_DIR: Path = settings.log_dir


def resource_path(package: str, relative: str) -> Path:
    """``package`` 自带资源的路径(假设文件系统安装,如 Docker/服务器部署)。

    包名必须显式给出:本模块已不在资源所属的包里(ADR 0022),不能再从 ``__name__`` 推断。
    zip 安装场景请改用 importlib.resources.as_file 上下文管理器。
    """
    return Path(str(files(package))) / relative
