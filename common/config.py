"""配置加载：按 QUANT_ENV 选择 paper / live，默认 paper（CLAUDE.md §4.3）。

配置文件位于 <venue_dir>/config/<venue>.<env>.yaml，**永不含密钥**。

对外函数：
    current_env()                 当前环境，未设 QUANT_ENV 时返回 "paper"
    load_config(venue, env=None)  加载场所配置；live 环境校验 `live: true`
    assert_live_allowed(cfg)      提交真实委托前的最后一道断言
"""

from __future__ import annotations

__all__ = ["current_env", "load_config", "assert_live_allowed",
           "ENV_VAR", "ENV_PAPER", "ENV_LIVE", "VALID_ENVS"]

import os
from typing import Any

import yaml

from common.paths import config_dir

ENV_VAR = "QUANT_ENV"
ENV_PAPER = "paper"
ENV_LIVE = "live"
VALID_ENVS = (ENV_PAPER, ENV_LIVE)

def current_env() -> str:
    """当前环境。未设置 QUANT_ENV 时返回 paper——默认必须是 paper。"""
    env = os.environ.get(ENV_VAR, ENV_PAPER).strip().lower()
    if env not in VALID_ENVS:
        raise ValueError(f"{ENV_VAR}={env!r} 非法，可选 {VALID_ENVS}")
    return env

def load_config(venue: str, env: str | None = None) -> dict[str, Any]:
    """加载场所配置。

    Args:
        venue: 场所 slug，"okx" 或 "t212"。
        env: 覆盖环境；None 表示取 current_env()。

    Returns:
        配置字典。

    Raises:
        FileNotFoundError: 配置文件不存在。
        ValueError: live 环境但配置缺少 `live: true` 断言位。
    """
    env = env or current_env()
    path = config_dir(venue) / f"{venue}.{env}.yaml"
    if not path.is_file():
        raise FileNotFoundError(
            f"配置不存在：{path}（可从同目录 {venue}.example.yaml 复制后填写）"
        )
    cfg: dict[str, Any] = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    cfg["_env"] = env
    cfg["_path"] = str(path)

    if env == ENV_LIVE and cfg.get("live") is not True:
        raise ValueError(f"{path} 缺少 `live: true` 断言位，拒绝以实盘环境加载")
    return cfg

def assert_live_allowed(cfg: dict[str, Any]) -> None:
    """提交真实委托前的最后一道断言（CLAUDE.md §4.3）。

    执行层在调用任何下单接口前必须先调本函数。
    """
    if cfg.get("_env") != ENV_LIVE:
        raise RuntimeError(f"当前环境为 {cfg.get('_env')!r}，禁止提交真实委托")
    if cfg.get("live") is not True:
        raise RuntimeError(f"配置 {cfg.get('_path')} 缺少 `live: true`，禁止提交真实委托")
