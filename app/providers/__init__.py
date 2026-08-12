# -*- coding: utf-8 -*-
"""
模型适配层包
============
本包是 AI-KM"云 API ↔ 私有化部署"平滑切换能力的核心（SRS FR-9）。

设计意图：
上层业务代码（检索、问答）永远只调用本包暴露的统一函数，
完全不知道底层用的是 DeepSeek、OpenAI 还是本地 Ollama。
10 月做私有化部署时，只需修改 .env 中的 provider 配置，业务代码一行不改。
"""

from .llm import chat_completion, chat_completion_stream, llm_health  # 导出大模型相关函数
from .embedding import embed_texts, embed_query, embedding_health  # 导出向量相关函数

# __all__ 声明本包对外暴露的公共接口，import * 时只会导入这些
__all__ = [
    "chat_completion",         # 非流式问答
    "chat_completion_stream",  # 流式问答
    "llm_health",              # 大模型健康检查
    "embed_texts",             # 批量文本向量化（入库用）
    "embed_query",             # 单条查询向量化（检索用）
    "embedding_health",        # 向量服务健康检查
]
