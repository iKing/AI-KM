# -*- coding: utf-8 -*-
"""
混合检索引擎
============
职责：在权限允许的范围内，找出与用户查询最相关的知识片段。

核心算法（SRS FR-4.3）：
    1. 权限过滤前置  —— 先算出用户能看哪些文档，无权限的从一开始就不参与检索
    2. BM25 召回     —— SQLite FTS5，擅长精确匹配
    3. 向量召回      —— 余弦相似度，擅长语义匹配
    4. RRF 融合      —— 把两路排名合并
    5. 质量加权      —— verified 知识加权，draft 知识降权
    6. 截断返回      —— 取前 N 条

为什么用 RRF（Reciprocal Rank Fusion，倒数排名融合）：
BM25 的分数和余弦相似度的分数量纲完全不同（前者可能是 -12.3，后者是 0.87），
直接加权求和需要精细调参，且换个数据集就失效。
RRF 只看"排名"不看"分数"，score = Σ 1/(k + rank)，无需调参、鲁棒性强，
是业界公认的开箱即用方案。
"""

import struct  # 解包二进制向量
import time  # 计时
from dataclasses import dataclass  # 数据类
from typing import Any, Optional  # 类型注解

from .. import audit, config, db  # 项目模块
from ..providers import embedding  # 向量适配层
from . import tokenizer  # 分词器

# 尝试导入 numpy。有它则向量计算走矩阵运算（快 50-100 倍），没有则退回纯 Python 循环
try:
    import numpy as np  # 数值计算库
    _HAS_NUMPY = True  # 标记可用
except ImportError:  # 未安装 numpy
    np = None  # 置空
    _HAS_NUMPY = False  # 标记不可用，走降级路径


@dataclass
class SearchResult:
    """单条检索结果。"""
    chunk_id: int  # 切片 ID
    doc_id: int  # 文档 ID
    title: str  # 文档标题
    heading_path: str  # 章节路径
    content: str  # 切片正文
    score: float  # 融合后的最终得分
    bm25_rank: int = 0  # 在 BM25 路的排名，0 表示该路未召回
    vector_rank: int = 0  # 在向量路的排名，0 表示该路未召回
    category_l1: str = ""  # 一级分类
    department: str = ""  # 部门名
    security_level: str = "internal"  # 密级
    quality_level: str = "normal"  # 质量等级
    effective_date: Optional[str] = None  # 生效日期
    owner: str = ""  # 责任人
    highlight: str = ""  # 高亮片段（HTML）
    anchor: str = ""  # 段落锚点：可拼成 /doc/<id>#<anchor> 直达该章节（P1 段落锚点）

    def to_dict(self) -> dict:
        """转成字典，供 JSON 序列化返回给前端。"""
        return {
            "chunk_id": self.chunk_id,
            "doc_id": self.doc_id,
            "title": self.title,
            "heading_path": self.heading_path,
            "content": self.content,
            "score": round(self.score, 4),  # 分数保留 4 位小数，避免前端显示一长串
            "bm25_rank": self.bm25_rank,
            "vector_rank": self.vector_rank,
            "category_l1": self.category_l1,
            # 把分类编码转成中文名，前端直接显示无需再映射
            "category_name": config.CATEGORIES_L1.get(self.category_l1, self.category_l1),
            "department": self.department,
            "security_level": self.security_level,
            "security_name": config.SECURITY_LEVELS.get(self.security_level, ""),
            "quality_level": self.quality_level,
            "quality_name": config.QUALITY_LEVELS.get(self.quality_level, ""),
            "effective_date": self.effective_date,
            "owner": self.owner,
            "highlight": self.highlight,
            "anchor": self.anchor,  # 章节锚点，供前端生成直达链接
        }


def _unpack_vector(blob: bytes) -> list[float]:
    """把数据库中的二进制向量还原成浮点数列表。"""
    count = len(blob) // 4  # float32 每个 4 字节
    return list(struct.unpack(f"<{count}f", blob))  # 小端序解包


def _build_scope_sql(
    user: Optional[dict],
    filters: Optional[dict] = None,
) -> tuple[str, list[Any]]:
    """
    构造"可检索文档范围"的 SQL 条件。

    这是安全与业务过滤的统一入口。所有检索都必须经过它。
    权限在这里就被切断，无权限的文档根本进不了候选集——
    而不是查出来之后再在结果里剔除（那样有泄露风险且浪费性能）。

    返回：
        (WHERE 条件片段, 参数列表)
    """
    from ..auth import visibility_filter  # 局部导入，避免模块之间循环依赖

    # 先拿到权限过滤条件（部门隔离 + 密级 + 发布状态）
    where_sql, params = visibility_filter(user, "d")
    conditions = [where_sql]  # 条件列表，权限条件是第一条

    filters = filters or {}  # 没传过滤条件则用空字典

    # --- 业务过滤条件（FR-4.4）---
    if filters.get("category_l1"):  # 按一级分类过滤
        conditions.append("d.category_l1 = ?")
        params.append(filters["category_l1"])
    if filters.get("category_l2"):  # 按二级分类过滤
        conditions.append("d.category_l2 = ?")
        params.append(filters["category_l2"])
    if filters.get("department_id"):  # 按部门过滤
        conditions.append("d.department_id = ?")
        params.append(int(filters["department_id"]))
    if filters.get("security_level"):  # 按密级过滤
        conditions.append("d.security_level = ?")
        params.append(filters["security_level"])
    if filters.get("quality_level"):  # 按质量等级过滤
        conditions.append("d.quality_level = ?")
        params.append(filters["quality_level"])
    if filters.get("date_from"):  # 生效日期起始
        conditions.append("d.effective_date >= ?")
        params.append(filters["date_from"])
    if filters.get("date_to"):  # 生效日期截止
        conditions.append("d.effective_date <= ?")
        params.append(filters["date_to"])
    if filters.get("doc_ids"):  # 限定在指定文档范围内（评测和"文档内搜索"场景用）
        ids = [int(i) for i in filters["doc_ids"]]  # 转成整数列表，顺便防注入
        if ids:  # 非空才添加条件
            conditions.append(f"d.id IN ({','.join('?' for _ in ids)})")  # 构造 IN 占位符
            params.extend(ids)
    if filters.get("exclude_confidential"):  # 显式排除机密（AI 问答默认走这条，FR-5.5）
        conditions.append("d.security_level != 'confidential'")
    if filters.get("tag"):  # 按标签过滤（P1 标签体系）
        # 通过 doc_tags 与 tags 关联，找出打了该标签的文档；用子查询避免多表 JOIN 干扰排名
        conditions.append(
            "d.id IN (SELECT dt.doc_id FROM doc_tags dt "
            "JOIN tags t ON t.id = dt.tag_id WHERE t.name = ?)"
        )
        params.append(filters["tag"])  # 标签名参数

    return " AND ".join(conditions), params  # 用 AND 连接所有条件


def _bm25_recall(
    query_text: str,
    scope_sql: str,
    scope_params: list[Any],
    limit: int,
) -> list[tuple[int, float]]:
    """
    BM25 关键词召回。

    返回：
        [(chunk_id, bm25分数), ...] 按相关度降序
        注意：SQLite FTS5 的 bm25() 返回的是负数，越小（越负）表示越相关
    """
    fts_query = tokenizer.build_fts_query(query_text)  # 把自然语言转成 FTS5 查询表达式
    if not fts_query:  # 分词后没有有效词（如用户只输入了标点）
        return []  # 返回空，跳过这一路

    try:
        rows = db.query(
            f"""
            SELECT f.chunk_id AS cid, bm25(chunks_fts) AS score
            FROM chunks_fts f
            JOIN chunks c ON c.id = f.chunk_id
            JOIN documents d ON d.id = c.doc_id
            WHERE chunks_fts MATCH ? AND {scope_sql}
            ORDER BY score ASC
            LIMIT ?
            """,
            # 参数顺序必须与 SQL 中占位符的出现顺序一致：先 MATCH 的参数，再 scope 的参数，最后 LIMIT
            [fts_query] + scope_params + [limit],
        )
    except Exception as exc:  # FTS5 查询语法错误等异常
        # 不抛出异常中断整个检索，而是返回空让向量路继续工作（优雅降级）
        print(f"[BM25检索异常] {exc}")
        return []

    return [(r["cid"], r["score"]) for r in rows]  # 提取 (切片ID, 分数) 元组


def _vector_recall(
    query_text: str,
    scope_sql: str,
    scope_params: list[Any],
    limit: int,
) -> list[tuple[int, float]]:
    """
    向量语义召回。

    实现方式：把范围内所有切片的向量load 进内存，与查询向量算余弦相似度，取 TopK。
    这叫"暴力检索"（brute-force）。
    为什么不用专门的向量数据库：10 万条 1024 维向量的暴力检索在 numpy 下约 50 毫秒，
    完全满足需求，而引入 Milvus 会增加一整套运维负担。
    数据量超过 50 万条时再考虑升级（NFR-4 已预留接口）。

    返回：
        [(chunk_id, 余弦相似度), ...] 按相似度降序
    """
    try:
        query_vec = embedding.embed_query(query_text)  # 把查询转成向量
    except Exception as exc:  # 向量服务不可用
        # 降级：返回空，只靠 BM25 检索（NFR-1 优雅降级）
        print(f"[向量检索降级] 查询向量化失败：{exc}")
        return []

    # 取出范围内所有切片的向量。只取当前模型生成的向量，避免混入切换模型前的旧向量
    rows = db.query(
        f"""
        SELECT v.chunk_id AS cid, v.vector AS vec
        FROM chunk_vectors v
        JOIN chunks c ON c.id = v.chunk_id
        JOIN documents d ON d.id = c.doc_id
        WHERE {scope_sql} AND v.dim = ?
        """,
        scope_params + [config.EMBED_DIM],
    )
    if not rows:  # 范围内没有任何向量
        return []

    if _HAS_NUMPY:  # ---- numpy 加速路径 ----
        # 把查询向量转成 numpy 数组
        q = np.array(query_vec, dtype=np.float32)
        q_norm = np.linalg.norm(q)  # 计算模长
        if q_norm == 0:  # 零向量无法计算余弦，直接返回
            return []
        q = q / q_norm  # 归一化，这样余弦相似度就等于点积

        ids: list[int] = []  # 切片 ID 列表
        # np.frombuffer 直接把二进制字节解释成 float32 数组，零拷贝，非常快
        mats = [np.frombuffer(r["vec"], dtype=np.float32) for r in rows]
        ids = [r["cid"] for r in rows]  # 对应的切片 ID

        matrix = np.vstack(mats)  # 把所有向量堆成一个 (N, dim) 的矩阵
        # 计算每行的模长，keepdims 保持二维形状便于广播除法
        norms = np.linalg.norm(matrix, axis=1, keepdims=True)
        norms[norms == 0] = 1.0  # 防止除零：零向量的模长置为 1，结果仍是零向量
        matrix = matrix / norms  # 逐行归一化

        scores = matrix @ q  # 矩阵乘向量，一次算出所有余弦相似度

        # np.argsort 返回升序索引，加负号变降序；取前 limit 个
        top_idx = np.argsort(-scores)[:limit]
        return [(ids[int(i)], float(scores[int(i)])) for i in top_idx]  # 组装结果

    # ---- 纯 Python 降级路径（无 numpy 时）----
    import math  # 数学库

    q_norm = math.sqrt(sum(v * v for v in query_vec))  # 查询向量模长
    if q_norm == 0:  # 零向量
        return []
    q_unit = [v / q_norm for v in query_vec]  # 归一化

    scored: list[tuple[int, float]] = []  # 存放 (ID, 相似度)
    for r in rows:  # 逐条计算
        vec = _unpack_vector(r["vec"])  # 解包向量
        norm = math.sqrt(sum(v * v for v in vec))  # 模长
        if norm == 0:  # 跳过零向量
            continue
        # 点积除以模长即为余弦相似度
        dot = sum(a * b for a, b in zip(vec, q_unit))
        scored.append((r["cid"], dot / norm))

    scored.sort(key=lambda x: x[1], reverse=True)  # 按相似度降序排序
    return scored[:limit]  # 取前 limit 条


def _rrf_fuse(
    bm25_results: list[tuple[int, float]],
    vector_results: list[tuple[int, float]],
    k: int,
) -> dict[int, dict]:
    """
    RRF 倒数排名融合。

    公式：score(d) = Σ_路 1 / (k + rank_路(d))

    直觉解释：
    一个片段如果在两路里都排前面，会拿到两份高分，总分自然最高；
    只在一路里出现的片段得分较低但仍有机会进入结果；
    k 的作用是压低头部差距，避免第 1 名和第 2 名的分差过于悬殊。

    返回：
        {chunk_id: {"score": 融合分, "bm25_rank": 排名, "vector_rank": 排名}}
    """
    fused: dict[int, dict] = {}  # 融合结果字典

    for rank, (cid, _score) in enumerate(bm25_results, start=1):  # 遍历 BM25 结果，排名从 1 开始
        # setdefault：键不存在时插入默认值并返回，存在则直接返回已有值
        entry = fused.setdefault(cid, {"score": 0.0, "bm25_rank": 0, "vector_rank": 0})
        entry["score"] += 1.0 / (k + rank)  # 累加本路贡献的分数
        entry["bm25_rank"] = rank  # 记录本路排名

    for rank, (cid, _score) in enumerate(vector_results, start=1):  # 遍历向量结果
        entry = fused.setdefault(cid, {"score": 0.0, "bm25_rank": 0, "vector_rank": 0})
        entry["score"] += 1.0 / (k + rank)  # 累加
        entry["vector_rank"] = rank  # 记录排名

    return fused


def search(
    query_text: str,
    user: Optional[dict] = None,
    filters: Optional[dict] = None,
    top_k: Optional[int] = None,
    mode: str = "hybrid",
    log_search: bool = True,
    source: str = "web",
) -> tuple[list[SearchResult], dict]:
    """
    检索主入口。

    参数：
        query_text: 查询文本
        user:       当前用户（决定可见范围）
        filters:    过滤条件字典
        top_k:      返回条数
        mode:       检索模式 hybrid 混合 / bm25 仅关键词 / vector 仅语义
        log_search: 是否记录检索日志（评测时置 False，避免污染统计数据）
        source:     来源 web / api
    返回：
        (结果列表, 统计信息字典)
    """
    start_time = time.time()  # 开始计时
    top_k = top_k or config.SEARCH_TOP_K  # 没指定则用配置默认值
    query_text = (query_text or "").strip()  # 清理查询文本

    if not query_text:  # 空查询直接返回
        return [], {"total": 0, "cost_ms": 0, "mode": mode, "bm25_count": 0, "vector_count": 0}

    # ---- 步骤 1：构造可检索范围（权限 + 业务过滤）----
    scope_sql, scope_params = _build_scope_sql(user, filters)
    recall_k = config.RECALL_TOP_K  # 每路召回条数

    # ---- 步骤 2：两路召回 ----
    bm25_results: list[tuple[int, float]] = []  # BM25 结果
    vector_results: list[tuple[int, float]] = []  # 向量结果

    if mode in ("hybrid", "bm25"):  # 需要关键词路
        bm25_results = _bm25_recall(query_text, scope_sql, scope_params, recall_k)
    if mode in ("hybrid", "vector"):  # 需要语义路
        vector_results = _vector_recall(query_text, scope_sql, scope_params, recall_k)

    # ---- 步骤 3：融合 ----
    if mode == "hybrid":  # 混合模式走 RRF 融合
        fused = _rrf_fuse(bm25_results, vector_results, config.RRF_K)
    elif mode == "bm25":  # 仅关键词模式：直接按排名给分
        fused = {
            cid: {"score": 1.0 / (config.RRF_K + rank), "bm25_rank": rank, "vector_rank": 0}
            for rank, (cid, _) in enumerate(bm25_results, start=1)
        }
    else:  # 仅语义模式：用余弦相似度作为分数
        fused = {
            cid: {"score": score, "bm25_rank": 0, "vector_rank": rank}
            for rank, (cid, score) in enumerate(vector_results, start=1)
        }

    if not fused:  # 两路都没召回到任何内容
        cost = int((time.time() - start_time) * 1000)  # 计算耗时
        if log_search:  # 记录零结果查询（FR-10.3，这是发现知识缺口的关键信号）
            _log_search(query_text, user, mode, 0, cost, source)
        return [], {"total": 0, "cost_ms": cost, "mode": mode, "bm25_count": 0, "vector_count": 0}

    # ---- 步骤 4：取出候选切片的完整信息 ----
    candidate_ids = list(fused.keys())  # 所有候选切片 ID
    placeholders = ",".join("?" for _ in candidate_ids)  # 构造 IN 占位符
    rows = db.query(
        f"""
        SELECT c.id AS chunk_id, c.doc_id, c.heading_path, c.content, c.anchor,
               d.title, d.category_l1, d.security_level, d.quality_level,
               d.effective_date, d.owner,
               COALESCE(dept.name, '') AS dept_name
        FROM chunks c
        JOIN documents d ON d.id = c.doc_id
        LEFT JOIN departments dept ON dept.id = d.department_id
        WHERE c.id IN ({placeholders})
        """,
        candidate_ids,
    )

    # ---- 步骤 5：质量加权并组装结果 ----
    results: list[SearchResult] = []  # 结果列表
    for r in rows:  # 遍历查出的每条切片
        info = fused[r["chunk_id"]]  # 取出该切片的融合信息
        # 按质量等级加权：verified ×1.2，normal ×1.0，draft ×0.8
        # 这实现了《规范》第七章"高质量知识优先"的策略
        weight = config.QUALITY_WEIGHTS.get(r["quality_level"], 1.0)
        final_score = info["score"] * weight  # 计算最终得分

        results.append(SearchResult(
            chunk_id=r["chunk_id"],
            doc_id=r["doc_id"],
            title=r["title"],
            heading_path=r["heading_path"] or "",
            content=r["content"],
            score=final_score,
            bm25_rank=info["bm25_rank"],
            vector_rank=info["vector_rank"],
            category_l1=r["category_l1"],
            department=r["dept_name"],
            security_level=r["security_level"],
            quality_level=r["quality_level"],
            effective_date=r["effective_date"],
            owner=r["owner"] or "",
            # 生成高亮片段，让用户一眼看出为什么这条被搜到（FR-4.5）
            highlight=tokenizer.highlight_terms(r["content"], query_text),
            anchor=r["anchor"] or "",  # 章节锚点，支持结果直达具体段落
        ))

    results.sort(key=lambda x: x.score, reverse=True)  # 按最终得分降序排序
    results = results[:top_k]  # 截取前 top_k 条

    cost = int((time.time() - start_time) * 1000)  # 总耗时

    if log_search:  # 记录检索日志
        _log_search(query_text, user, mode, len(results), cost, source)

    stats = {  # 统计信息，前端可展示"耗时 xx ms，两路各召回多少"
        "total": len(results),
        "cost_ms": cost,
        "mode": mode,
        "bm25_count": len(bm25_results),
        "vector_count": len(vector_results),
        "candidates": len(fused),
    }
    return results, stats


def _log_search(
    query_text: str,
    user: Optional[dict],
    mode: str,
    result_count: int,
    cost_ms: int,
    source: str,
) -> None:
    """
    记录检索日志。
    这些数据支撑两个业务目标：
    1. FR-10.2 使用度量：证明 SG2"查询效率+50%"
    2. FR-10.3 零结果清单：暴露知识缺口，指导补充哪些知识
    """
    try:
        db.execute(
            """
            INSERT INTO search_logs (ts, user_id, query, mode, result_count, cost_ms, source)
            VALUES (?,?,?,?,?,?,?)
            """,
            (
                audit.now_iso(),  # 时间
                user["id"] if user else None,  # 用户 ID
                query_text[:500],  # 查询词，截断防止超长
                mode,  # 检索模式
                result_count,  # 结果数
                cost_ms,  # 耗时
                source,  # 来源
            ),
        )
    except Exception as exc:  # 日志失败不影响检索主流程
        print(f"[检索日志写入失败] {exc}")


def search_stats(days: int = 30) -> dict:
    """
    检索统计数据（FR-10.2、FR-10.3），供效能看板使用。

    参数：
        days: 统计最近多少天
    """
    from datetime import datetime, timedelta  # 局部导入日期工具

    # 计算统计起始时间
    since = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d 00:00:00")

    # 总检索次数
    total_row = db.query_one("SELECT COUNT(*) AS c FROM search_logs WHERE ts >= ?", (since,))
    # 零结果次数（知识缺口的直接信号）
    zero_row = db.query_one(
        "SELECT COUNT(*) AS c FROM search_logs WHERE ts >= ? AND result_count = 0", (since,)
    )
    # 平均耗时（性能监控）
    avg_row = db.query_one("SELECT AVG(cost_ms) AS a FROM search_logs WHERE ts >= ?", (since,))
    # 活跃用户数
    users_row = db.query_one(
        "SELECT COUNT(DISTINCT user_id) AS c FROM search_logs WHERE ts >= ? AND user_id IS NOT NULL",
        (since,),
    )
    # 热门查询 TOP 20
    top_queries = db.query(
        """
        SELECT query, COUNT(*) AS c FROM search_logs
        WHERE ts >= ? GROUP BY query ORDER BY c DESC LIMIT 20
        """,
        (since,),
    )
    # 零结果查询清单 TOP 30（这是最有业务价值的一张表——直接告诉你该补什么知识）
    zero_queries = db.query(
        """
        SELECT query, COUNT(*) AS c FROM search_logs
        WHERE ts >= ? AND result_count = 0
        GROUP BY query ORDER BY c DESC LIMIT 30
        """,
        (since,),
    )
    # 按天统计趋势，用于画折线图
    daily = db.query(
        """
        SELECT substr(ts, 1, 10) AS day, COUNT(*) AS c FROM search_logs
        WHERE ts >= ? GROUP BY day ORDER BY day
        """,
        (since,),
    )

    total = total_row["c"] if total_row else 0  # 总次数
    zero = zero_row["c"] if zero_row else 0  # 零结果次数

    return {
        "days": days,  # 统计天数
        "total_searches": total,  # 总检索数
        "zero_result_count": zero,  # 零结果数
        # 零结果率：这个指标越低说明知识覆盖越完整
        "zero_result_rate": round(zero / total * 100, 1) if total else 0.0,
        "avg_cost_ms": int(avg_row["a"]) if avg_row and avg_row["a"] else 0,  # 平均耗时
        "active_users": users_row["c"] if users_row else 0,  # 活跃用户
        "top_queries": [{"query": r["query"], "count": r["c"]} for r in top_queries],  # 热门查询
        "zero_queries": [{"query": r["query"], "count": r["c"]} for r in zero_queries],  # 知识缺口
        "daily": [{"day": r["day"], "count": r["c"]} for r in daily],  # 每日趋势
    }
