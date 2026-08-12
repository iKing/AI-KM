# -*- coding: utf-8 -*-
"""
检索质量评测模块
================
用预先标注的"金标准"评测集，客观度量 AI 检索能力是否达到 SRS 的核心 KPI：
    **AI 检索准确率 ≥ 90%**（定义为 Recall@5 ≥ 90%）。

为什么需要它：
- 没有客观评测，"检索效果很好"只是主观感受，无法向业务方证明
- 模型/切片参数调整前跑一遍、调整后跑一遍，用数字对比验证改进有效（FR-6.3）
- 用户标记为"有误"的问答可沉淀为评测用例，形成持续优化闭环

评测逻辑：
    对每个用例，用给定模式检索 TopK，若返回结果命中任一"期望文档"即算通过。
    - Recall@K = 通过数 / 用例总数
    - MRR = 各用例 (1 / 首次命中排名) 的平均值，衡量排序质量
"""

import json  # JSON 序列化（配置快照）

from . import db, auth, config, audit  # 内部模块
from .search import engine  # 检索引擎


def run_eval(mode: str = "hybrid", top_k: int = 5, run_by: int = None) -> dict:
    """
    运行一次检索评测。

    参数：
        mode:   检索模式 hybrid / bm25 / vector
        top_k:   取前 K 个结果判定命中（KPI 用 K=5）
        run_by:  执行人用户 ID
    返回：
        {"ok": bool, "run_id": int, "total": N, "passed": N,
         "recall_at_k": 0.xx, "mrr": 0.xx, "mode": str}
    """
    # 取出所有启用的评测用例
    cases = db.query("SELECT * FROM eval_cases WHERE active = 1")
    if not cases:  # 评测集为空，无法评测
        return {"ok": False, "message": "评测集为空，请先导入用例（见 evalset/ 目录）"}

    # 评测用"上帝视角"：以管理员身份检索，排除权限对召回的干扰，
    # 这样才能纯粹反映"检索算法本身"的准确率
    admin = auth.get_user_by_username("admin")

    total = 0       # 用例总数
    passed = 0      # 命中数
    mrr_sum = 0.0   # 倒数排名累加
    detail_rows: list[tuple] = []  # 明细：(case_id, question, hit, rank, got_doc_ids)

    for c in cases:  # 逐用例评测
        # 期望命中的文档 ID 列表（金标准）
        expect_ids = [int(x) for x in (c["expect_doc_ids"] or "").split(",") if x.strip()]
        # 跑检索。log_search=False 避免评测查询污染真实统计
        results, _stats = engine.search(
            c["question"], user=admin, top_k=top_k, mode=mode, log_search=False,
        )
        got_doc_ids = [r.doc_id for r in results]  # 实际返回的文档 ID

        hit = 0   # 是否命中
        rank = 0  # 首次命中排名

        if not expect_ids or c["case_type"] == "outside":
            # 库外问题（outside）：正确的表现是"不返回相关知识"。
            # 只要结果中没有命中任何期望文档（这里期望为空），即视为正确识别，判定通过。
            hit = 1 if not got_doc_ids else 0
        else:
            # 正常 / 同义改写用例：检查返回结果是否命中任一期望文档
            for idx, did in enumerate(got_doc_ids, start=1):  # 按返回顺序找第一个命中
                if did in expect_ids:
                    hit = 1
                    rank = idx
                    break

        total += 1
        if hit:
            passed += 1
            # MRR 只对有真实排名（rank>0）的相关命中有意义；库外问题命中 rank=0 不计入
            if rank > 0:
                mrr_sum += 1.0 / rank
        detail_rows.append((c["id"], c["question"], hit, rank, got_doc_ids))

    # 计算指标
    recall = passed / total if total else 0.0       # Recall@K
    mrr = mrr_sum / total if total else 0.0          # Mean Reciprocal Rank
    started = audit.now_iso()                         # 评测完成时间（做一次足够快）

    # 配置快照：记录当次环境，便于事后复现"为什么这次分高/低"
    config_snap = json.dumps({
        "LLM_PROVIDER": config.LLM_PROVIDER,
        "EMBED_PROVIDER": config.EMBED_PROVIDER,
        "EMBED_MODEL": config.EMBED_MODEL,
        "EMBED_DIM": config.EMBED_DIM,
        "CHUNK_SIZE": config.CHUNK_SIZE,
        "RRF_K": config.RRF_K,
    }, ensure_ascii=False)

    # 写入本次评测运行记录
    run_id = db.execute(
        """
        INSERT INTO eval_runs
            (started_at, finished_at, total, passed, recall_at_k, mrr, top_k, mode, config_snap, run_by)
        VALUES (?,?,?,?,?,?,?,?,?,?)
        """,
        (started, started, total, passed, round(recall, 4), round(mrr, 4), top_k, mode, config_snap, run_by),
    )

    # 批量写入每条用例的明细
    db.execute_many(
        """
        INSERT INTO eval_results (run_id, case_id, question, hit, rank, got_docs)
        VALUES (?,?,?,?,?,?)
        """,
        [
            (run_id, cid, q, hit, rank, ",".join(str(d) for d in got))
            for (cid, q, hit, rank, got) in detail_rows
        ],
    )

    return {
        "ok": True,
        "run_id": run_id,
        "total": total,
        "passed": passed,
        "recall_at_k": round(recall, 4),
        "mrr": round(mrr, 4),
        "mode": mode,
        # 是否达成 KPI：Recall@K ≥ 90%
        "meet_target": recall >= 0.9,
    }


def list_eval_runs(limit: int = 20) -> list[dict]:
    """评测历史列表，按时间倒序。"""
    rows = db.query(
        "SELECT * FROM eval_runs ORDER BY id DESC LIMIT ?", (limit,)
    )
    return db.rows_to_dicts(rows)


def get_eval_run(run_id: int) -> dict:
    """
    取某次评测的完整信息（含明细），用于结果下钻。
    明细中 got_docs 是实际返回的文档 ID 列表（逗号分隔），hit/rank 标注是否命中。
    """
    run = db.query_one("SELECT * FROM eval_runs WHERE id = ?", (run_id,))
    if not run:
        return {}
    # 明细按命中情况排序（未命中的排前面，方便优先排查问题用例）
    details = db.query(
        "SELECT * FROM eval_results WHERE run_id = ? ORDER BY hit ASC, id ASC",
        (run_id,),
    )
    return {
        "run": db.row_to_dict(run),
        "details": db.rows_to_dicts(details),
    }
