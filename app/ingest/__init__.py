# -*- coding: utf-8 -*-
"""
文档入库包
==========
负责把各种格式的原始文档，转化为可被 AI 检索的知识切片。

完整流水线：
    原始文件 → parser 解析 → chunker 切片 → embedding 向量化 → 入库索引

三个子模块各司其职：
- parser.py   ：把 docx/pdf/xlsx/md/txt/html 统一解析成"带层级的结构化块"
- chunker.py  ：把结构化块按语义边界切成检索片段，并携带章节路径
- pipeline.py ：编排整个流程，含规范校验、状态机、事务、错误处理
"""

from .parser import parse_file, ParseError, SUPPORTED_EXTS  # 解析相关
from .chunker import chunk_blocks, Chunk  # 切片相关
from .pipeline import ingest_file, ingest_batch, reindex_document, reindex_all, IngestError  # 流水线相关
# 在线编辑与版本管理（P0-2 / P0-3）：从纯文本重建切片、保存编辑、回滚版本
from .pipeline import update_document, rollback_document, _assemble_text_from_chunks

__all__ = [
    "parse_file",       # 解析单个文件
    "ParseError",       # 解析异常
    "SUPPORTED_EXTS",   # 支持的扩展名集合
    "chunk_blocks",     # 切片函数
    "Chunk",            # 切片数据类
    "ingest_file",      # 单文件入库
    "ingest_batch",     # 批量入库
    "reindex_document", # 重建单个文档索引
    "reindex_all",      # 全量重建索引
    "IngestError",      # 入库异常
    "update_document",  # 在线编辑文档（产生版本快照）
    "rollback_document",# 回滚到历史版本
]
