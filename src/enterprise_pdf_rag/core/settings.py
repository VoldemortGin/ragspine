"""全局配置、常量与路径的唯一来源:环境变量 + config/enterprise-pdf-rag/settings.yaml。

根锚点 ROOT_DIR、运行期可写目录 DATA_DIR / LOG_DIR、包内资源定位 resource_path()
全部在此定义;其他模块一律从这里导入,不要自己再算一次项目根。

beartype 叶子约束:本模块不得 import 任何本项目内、希望被检查的模块。
只依赖标准库 + pydantic / pydantic-settings(第三方依赖不受叶子约束限制)。
"""

import os
from functools import lru_cache
from importlib.resources import files
from pathlib import Path
from typing import Literal

from pydantic import field_validator
from pydantic_settings import (
    BaseSettings,
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


def _resolve_directory(value: Path) -> Path:
    path = value.expanduser()
    return (path if path.is_absolute() else ROOT_DIR / path).resolve()


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="APP_",  # APP_IS_DEBUG、APP_BEARTYPE_ON ...
        env_nested_delimiter="__",  # APP_RETRIEVER__TOP_K 覆盖嵌套字段
        yaml_file=ROOT_DIR / "config" / "enterprise-pdf-rag" / "settings.yaml",
        extra="ignore",
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
    # 兼容根:每项是一个 processing store 根,其父目录即 source store 根;默认空
    legacy_document_roots: tuple[Path, ...] = ()

    @field_validator("data_dir", "log_dir")
    @classmethod
    def resolve_runtime_directory(cls, value: Path) -> Path:
        return _resolve_directory(value)

    @field_validator("ingestion_dir")
    @classmethod
    def resolve_optional_directory(cls, value: Path | None) -> Path | None:
        return None if value is None else _resolve_directory(value)

    @field_validator("legacy_document_roots")
    @classmethod
    def resolve_legacy_roots(cls, value: tuple[Path, ...]) -> tuple[Path, ...]:
        return tuple(_resolve_directory(item) for item in value)

    @property
    def ingestion_root(self) -> Path:
        return self.ingestion_dir if self.ingestion_dir is not None else self.data_dir / "ingestion"

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        # 优先级从高到低:构造参数 > 环境变量 > yaml > secrets。不读取 .env。
        # 末位的 file_secret_settings 只有在 model_config 设了 secrets_dir 时才读文件;
        # 本模板没设,所以它当前是 no-op —— 留在链尾是为了「要用 Docker secrets
        # 时只需加一行 secrets_dir」,而不是它现在在起作用。
        return (
            init_settings,
            env_settings,
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

_PKG = __name__.split(".")[0]  # 顶层包名,重命名安全

DATA_DIR: Path = settings.data_dir
LOG_DIR: Path = settings.log_dir


def resource_path(relative: str) -> Path:
    """包内自带资源的路径(假设文件系统安装,如 Docker/服务器部署)。

    zip 安装场景请改用 importlib.resources.as_file 上下文管理器。
    """
    return Path(str(files(_PKG))) / relative
