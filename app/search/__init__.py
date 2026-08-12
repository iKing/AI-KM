# -*- coding: utf-8 -*-
"""
检索包
======
实现 SRS FR-4 的混合检索能力。

两路召回 + 一次融合：
- BM25 关键词检索：擅长精确匹配专有名词、编号、法规名称
- 向量语义检索：擅长同义表述（"集采新规" 命中 "带量采购管理办法"）
- RRF 融合：把两路结果合并排序，效果稳定优于任一单路

这是"10 月底检索准确率 ≥ 90%"能否达成的技术基础。
"""

from .tokenizer import tokenize, tokenize_to_string, build_fts_query, highlight_terms  # 分词工具
from .engine import search, SearchResult, search_stats  # 检索主函数

__all__ = [
    "tokenize",             # 分词
    "tokenize_to_string",   # 分词并拼接
    "build_fts_query",      # 构造 FTS5 查询表达式
    "highlight_terms",      # 结果高亮
    "search",               # 检索主入口
    "SearchResult",         # 检索结果数据类
    "search_stats",         # 检索统计
]
