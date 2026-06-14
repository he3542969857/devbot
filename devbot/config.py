"""Pydantic settings — 所有外部依赖与开关。

环境变量前缀：``DEVBOT_``，例如 ``DEVBOT_LLM_PROVIDER=openai``。
"""

from __future__ import annotations

from typing import Literal, Dict

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class LlmCfg(BaseSettings):
    """LLM 客户端配置。"""
    model_config = SettingsConfigDict(env_prefix="DEVBOT_LLM_", extra="ignore")

    provider: Literal['mock', 'openai'] = 'openai'
    base_url: str = "https://api.siliconflow.cn/v1"
    api_key: str = "YOUR_SILICONFLOW_API_KEY"
    # 不同 Agent / Critic 用不同档位模型 —— 在 prompt + cost 间平衡
    models: Dict[str, str] = Field(default_factory=lambda: {
        "default": "deepseek-ai/DeepSeek-V3",
        "requirement": "deepseek-ai/DeepSeek-V3",
        "codegen": "deepseek-ai/DeepSeek-V3",
        "testgen": "deepseek-ai/DeepSeek-V3",
        "correctness": "deepseek-ai/DeepSeek-V3",
        "design": "deepseek-ai/DeepSeek-V3",
        "security": "deepseek-ai/DeepSeek-V3",
        "readability": "deepseek-ai/DeepSeek-V3",
    })


class CodedocCfg(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="DEVBOT_CODEDOC_", extra="ignore")

    mode: Literal["mock", "http"] = "http"
    base_url: str = "http://127.0.0.1:8501"
    internal_key: str = "CHANGE_ME_INTERNAL_KEY"
    timeout_seconds: float = 8.0


class GithubCfg(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="DEVBOT_GITHUB_", extra="ignore")

    enabled: bool = False
    token: str = ""  # GitHub Personal Access Token or App token
    webhook_secret: str = "dev-secret"  # for verifying webhook signatures
    api_url: str = "https://api.github.com"


class AgentCfg(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="DEVBOT_AGENT_", extra="ignore")

    react_max_steps: int = 6
    per_critic_timeout_seconds: float = 30.0
    critic_concurrency: int = 4


class Settings(BaseSettings):
    """顶层 settings —— 由 FastAPI startup 实例化一次复用。"""
    model_config = SettingsConfigDict(env_prefix="DEVBOT_", extra="ignore")

    env: str = "dev"
    llm: LlmCfg = Field(default_factory=LlmCfg)
    codedoc: CodedocCfg = Field(default_factory=CodedocCfg)
    github: GithubCfg = Field(default_factory=GithubCfg)
    agent: AgentCfg = Field(default_factory=AgentCfg)


_settings: Settings | None = None


def get_settings() -> Settings:
    global _settings
    if _settings is None:
        _settings = Settings()
    return _settings


def reset_settings_for_tests(s: Settings | None = None) -> None:
    """测试用 —— 替换全局 settings 实例。"""
    global _settings
    _settings = s
