import os
from enum import Enum
from dataclasses import dataclass, fields
from typing import Any, Optional, Dict 

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.runnables import RunnableConfig
from dataclasses import dataclass

DEFAULT_REPORT_STRUCTURE = """请使用以下结构为用户提供的主题撰写报告：

1. 引言（无需检索）
   - 简要介绍该主题领域

2. 主体部分：
   - 每个小节聚焦于主题的一个子话题

3. 结论
   - 尽量用一个结构化元素（如列表或表格）提炼主体部分的要点
   - 对报告内容进行简明扼要的总结"""

class SearchAPI(Enum):
    PERPLEXITY = "perplexity"
    TAVILY = "tavily"
    EXA = "exa"
    ARXIV = "arxiv"
    PUBMED = "pubmed"
    LINKUP = "linkup"
    DUCKDUCKGO = "duckduckgo"
    GOOGLESEARCH = "googlesearch"

@dataclass(kw_only=True)
class Configuration:
    """聊天机器人可配置的字段。"""
    # 通用配置
    report_structure: str = DEFAULT_REPORT_STRUCTURE # 默认为默认报告结构
    search_api: SearchAPI = SearchAPI.TAVILY # 默认为 TAVILY
    search_api_config: Optional[Dict[str, Any]] = None # 搜索 API 的配置参数

    # 图相关配置
    number_of_queries: int = 4 # 每次迭代生成的搜索查询数量
    max_search_depth: int = 2 # 最大反思+搜索迭代次数
    planner_provider: str = "openai"  # 规划模型的提供商，默认为 Anthropic
    planner_model: str = "qwen2.5_72b_instruct-gptq-int4" # 规划模型，默认为 claude-3-7-sonnet-latest
    planner_model_kwargs: Optional[Dict[str, Any]] = None # 规划模型的额外参数
    planer_model_base_url = "http://172.17.3.88:8021/v1"
    writer_provider: str = "openai" # 撰写模型的提供商，默认为 Anthropic
    writer_model: str = "qwen2.5_72b_instruct-gptq-int4" # 撰写模型，默认为 claude-3-5-sonnet-latest
    writer_model_kwargs: Optional[Dict[str, Any]] = None # 撰写模型的额外参数
    writer_model_base_url = "http://172.17.3.88:8021/v1"
    search_api: SearchAPI = SearchAPI.TAVILY # 默认为 TAVILY
    search_api_config: Optional[Dict[str, Any]] = None # 搜索 API 的配置参数

    # 多智能体相关配置
    supervisor_model: str = "openai:gpt-4.1" # 多智能体设置中主管代理的模型
    researcher_model: str = "openai:gpt-4.1" # 多智能体设置中研究代理的模型

    @classmethod
    def from_runnable_config(
        cls, config: Optional[RunnableConfig] = None
    ) -> "Configuration":
        """Create a Configuration instance from a RunnableConfig."""
        configurable = (
            config["configurable"] if config and "configurable" in config else {}
        )
        values: dict[str, Any] = {
            f.name: os.environ.get(f.name.upper(), configurable.get(f.name))
            for f in fields(cls)
            if f.init
        }
        return cls(**{k: v for k, v in values.items() if v})
