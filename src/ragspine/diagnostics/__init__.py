"""Local configuration inspection and installation diagnostics.

The package is lazy so importing :mod:`ragspine` never imports TOML parsing or
optional provider SDKs.  Doctor checks are deliberately local and never make
network requests.

Submodules:
    config.py — 最小 TOML 配置、分层合并与逐字段来源。
    doctor.py — 依赖、密钥、模型和文件系统的零网络诊断。
"""

import importlib

from ragspine import _lazy_submodules

_submodule_getattr, _submodule_dir = _lazy_submodules(__name__, __path__)

_CURATED = {
    "ConfigError": "config",
    "EffectiveConfig": "config",
    "RuntimeConfig": "config",
    "init_config": "config",
    "load_effective_config": "config",
    "render_config": "config",
    "DoctorFinding": "doctor",
    "DoctorReport": "doctor",
    "run_doctor": "doctor",
}

__all__ = list(_CURATED)


def __getattr__(name: str) -> object:
    module_name = _CURATED.get(name)
    if module_name is not None:
        module = importlib.import_module(f"{__name__}.{module_name}")
        return getattr(module, name)
    return _submodule_getattr(name)


def __dir__() -> list[str]:
    return sorted({*__all__, *_submodule_dir()})
