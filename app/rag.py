# -*- coding: utf-8 -*-
"""
RAG 问答编排
============
职责：把"检索到的知识片段"和"用户问题"组装成提示词，交给大模型生成带引用的答案。

三条不可妥协的红线（SRS FR-5）：
1. **有据可依**：答案只能基于检索到的知识，不能靠模型自己的先验知识编造
2. **必须溯源**：每个事实性论断都要标注来源，用户能点开看原文
3. **没有就说没有**：检索不到相关内容时明确拒答，绝不硬编一个看起来合理的答案

为什么这三条这么重要：
知识管理平台的价值建立在"可信"之上。
一个会编造的 AI 比没有 AI 更糟糕——用户被误导一次，就再也不会信任这个系统，
整个 SG1/SG2 的投入都会打水漂。宁可拒答，不可胡说。
"""

import json  # JSON 序列化
from typing import Any, Generator, Optional  # 类型注解

from . import audit, config, db  # 项目模块
from .providers import llm  # 大模型适配层
from .search import engine  # 检索引擎


# ============================================================
# 一、提示词模板
# ============================================================

# SYSTEM_PROMPT 是系统提示词，定义 AI 的行为边界。
# 这段文字是整个问答质量的地基——写得越明确，模型越不容易跑偏。
SYSTEM_PROMPT = """你是企业知识库的专业问答助手，服务于医药器械采购领域。

【回答规则（必须严格遵守）】
1. 只能基于下方提供的【参考资料】回答，绝对不能使用参考资料之外的知识。
2. 每个关键论断后面必须标注来源编号，格式为 [1]、[2]，对应参考资料的序号。
3. 如果参考资料中没有足够信息回答问题，直接回答"知识库中未找到相关内容"，
   并简要说明缺什么信息。不要用你自己的知识补充，不要猜测，不要编造。
4. 如果参考资料存在互相矛盾的内容，如实指出矛盾并列出各方说法与来源。
5. 引用标注为"未验证"的资料时，必须在答案中提醒："该内容尚未经过质量验证，建议核实原文"。
6. 涉及政策法规时，必须注明政策的生效日期和发文来源，提醒用户核对是否为最新版本。

【回答风格】
- 直接给结论，不说"根据您的问题"这类废话。
- 结构清晰，条理分明，必要时用列表。
- 专业但不啰嗦。
"""

# NO_ANSWER_TEXT 是检索不到内容时的标准回复
NO_ANSWER_TEXT = "知识库中未找到相关内容。"


def build_context(results: list[engine.SearchResult]) -> tuple[str, list[dict]]:
    """
    把检索结果组装成提供给大模型的参考资料文本。

    参数：
        results: 检索结果列表
    返回：
        (参考资料文本, 引用信息列表)
    """
    if not results:  # 没有检索结果
        return "", []

    context_parts: list[str] = []  # 参考资料的各个片段
    citations: list[dict] = []  # 引用元信息，返回给前端渲染引用卡片
    total_chars = 0  # 已累计的字数，用于控制上下文总长度

    for idx, r in enumerate(results[:config.RAG_CONTEXT_CHUNKS], start=1):  # 编号从 1 开始
        # 质量标记：draft 等级的知识必须明确标注（FR-5.4）
        quality_mark = "【未验证】" if r.quality_level == "draft" else ""
        # 日期信息：政策类文档的时效性至关重要
        date_info = f"，生效日期 {r.effective_date}" if r.effective_date else ""

        # 组装单条参考资料。给模型的信息越结构化，它的引用越准确
        piece = (
            f"[{idx}] {quality_mark}《{r.title}》"  # 序号 + 质量标记 + 标题
            f"（{config.CATEGORIES_L1.get(r.category_l1, r.category_l1)}"  # 分类
            f"{date_info}）\n"  # 日期
            f"章节：{r.heading_path}\n"  # 章节路径，帮助模型理解上下文位置
            f"内容：{r.content}\n"  # 正文
        )

        # 控制总长度，防止超出模型的上下文窗口
        if total_chars + len(piece) > config.RAG_MAX_CONTEXT_CHARS:
            break  # 超出就不再添加

        context_parts.append(piece)  # 加入参考资料
        total_chars += len(piece)  # 累计字数

        # 记录引用元信息，前端据此渲染可点击的引用卡片
        citations.append({
            "index": idx,  # 引用编号，与文本中的 [1] 对应
            "doc_id": r.doc_id,  # 文档 ID，用于跳转
            "chunk_id": r.chunk_id,  # 切片 ID
            "title": r.title,  # 文档标题
            "heading_path": r.heading_path,  # 章节路径
            # 正文预览，截断到 200 字避免卡片过长
            "snippet": r.content[:200] + ("…" if len(r.content) > 200 else ""),
            "category": config.CATEGORIES_L1.get(r.category_l1, r.category_l1),  # 分类中文名
            "quality_level": r.quality_level,  # 质量等级
            "effective_date": r.effective_date,  # 生效日期
            "owner": r.owner,  # 责任人，用户有疑问可以找到人
            "score": round(r.score, 4),  # 相关度得分
        })

    return "\n".join(context_parts), citations  # 用换行连接所有片段


def build_messages(
    question: str,
    context: str,
    history: Optional[list[dict]] = None,
) -> list[dict]:
    """
    组装发送给大模型的完整消息列表。

    参数：
        question: 用户问题
        context:  参考资料文本
        history:  历史对话，格式 [{"role": "user"/"assistant", "content": "..."}]
    """
    messages: list[dict] = [{"role": "system", "content": SYSTEM_PROMPT}]  # 系统提示词打头

    if history:  # 有历史对话则加入，实现多轮追问（FR-5.6）
        # 只保留最近 N 轮，防止上下文无限膨胀导致成本飙升和模型注意力分散
        recent = history[-config.RAG_HISTORY_TURNS * 2:]  # 一轮包含用户和助手两条消息
        for msg in recent:  # 逐条添加
            # 只接受合法的角色，防止脏数据导致接口报错
            if msg.get("role") in ("user", "assistant") and msg.get("content"):
                messages.append({"role": msg["role"], "content": msg["content"]})

    # 把参考资料和问题组装成最终的用户消息
    user_content = f"""【参考资料】
{context}

【用户问题】
{question}

请严格基于上述参考资料回答，并用 [编号] 标注来源。如果参考资料不足以回答，请直接说明。"""

    messages.append({"role": "user", "content": user_content})  # 加入用户消息
    return messages


# ============================================================
# 二、问答主流程
# ============================================================

def answer(
    question: str,
    user: Optional[dict] = None,
    filters: Optional[dict] = None,
    include_confidential: bool = False,
    history: Optional[list[dict]] = None,
    stream: bool = False,
) -> dict | Generator[dict, None, None]:
    """
    RAG 问答主函数。

    参数：
        question:             用户问题
        user:                 当前用户
        filters:              检索过滤条件
        include_confidential: 是否把机密文档纳入上下文（需权限，且会记审计）
        history:              历史对话
        stream:               是否流式返回
    返回：
        stream=False 时返回结果字典
        stream=True  时返回生成器，逐段产出 {"type": "...", "data": ...}
    """
    from .auth import can_view_confidential  # 局部导入避免循环依赖

    question = (question or "").strip()  # 清理问题文本
    if not question:  # 空问题
        result = {"ok": False, "answer": "请输入问题", "citations": [], "stats": {}}
        return _wrap_stream(result) if stream else result

    # ---- 步骤 1：机密内容权限判定（FR-5.5）----
    search_filters = dict(filters or {})  # 复制一份过滤条件，避免修改调用方传入的字典
    allow_conf = False  # 最终是否允许机密内容进入上下文

    if include_confidential:  # 用户请求包含机密
        if can_view_confidential(user):  # 校验用户是否有机密许可
            allow_conf = True  # 允许
            # 关键：每次开启机密上下文都必须留痕，这是合规审计的硬要求
            audit.log(
                "confidential_access",
                user_id=user["id"] if user else None,
                username=user["username"] if user else None,
                detail={"场景": "AI问答开启机密上下文", "问题": question[:200]},
                sensitive=True,  # 标记为敏感操作，可被一键筛选
            )
        # 无权限时静默降级为不含机密，不报错——避免暴露"存在机密文档"这一信息

    if not allow_conf:  # 不允许机密内容
        search_filters["exclude_confidential"] = True  # 在检索层面就排除掉

    # ---- 步骤 2：检索 ----
    results, search_stats = engine.search(
        question,
        user=user,
        filters=search_filters,
        top_k=config.RAG_CONTEXT_CHUNKS * 2,  # 多召回一些，组装上下文时再按长度截断
        mode="hybrid",  # 问答场景固定用混合检索，效果最好
        log_search=True,
        source="chat",
    )

    # ---- 步骤 3：拒答判定（FR-5.3，这是不可妥协的红线）----
    # 两种情况拒答：一是完全没检索到，二是检索到了但相关度都太低
    if not results or (results and results[0].score < config.RAG_MIN_SCORE):
        no_answer = {
            "ok": True,  # 流程本身是成功的，只是没有答案
            "answer": (
                f"{NO_ANSWER_TEXT}\n\n"
                "可能的原因：\n"
                "1. 相关知识尚未入库——建议联系对应部门的知识联络人补充\n"
                "2. 提问用词与文档表述差异较大——可尝试换个说法，或使用文档中的专业术语\n"
                "3. 该内容属于您当前权限范围之外"
            ),
            "citations": [],  # 无引用
            "no_answer": True,  # 明确标记这是拒答，前端可做特殊样式
            "stats": search_stats,
        }
        return _wrap_stream(no_answer) if stream else no_answer

    # ---- 步骤 4：组装上下文 ----
    context, citations = build_context(results)  # 构造参考资料与引用列表
    messages = build_messages(question, context, history)  # 组装完整消息

    # ---- 步骤 5：调用大模型 ----
    if stream:  # 流式返回
        return _stream_answer(messages, citations, search_stats)

    # 非流式：一次性拿完整答案
    try:
        answer_text = llm.chat_completion(messages)  # 调用大模型
    except llm.LLMError as exc:  # 模型调用失败
        # 降级策略：模型挂了，但检索结果是有效的，
        # 直接把检索到的原文片段返回给用户，总比什么都没有强（NFR-1 优雅降级）
        fallback = "AI 生成服务当前不可用，以下是检索到的相关知识原文：\n\n"
        for c in citations:  # 逐条列出
            fallback += f"[{c['index']}]《{c['title']}》\n{c['snippet']}\n\n"
        return {
            "ok": True,
            "answer": fallback,
            "citations": citations,
            "degraded": True,  # 标记为降级模式，前端可提示用户
            "error": str(exc),
            "stats": search_stats,
        }

    return {
        "ok": True,
        "answer": answer_text,  # 模型生成的答案
        "citations": citations,  # 引用列表
        "no_answer": NO_ANSWER_TEXT in answer_text,  # 检测模型是否自行判定为无答案
        "stats": search_stats,
    }


def _wrap_stream(result: dict) -> Generator[dict, None, None]:
    """
    把非流式结果包装成流式格式。
    用于拒答等无需调用模型的场景，让前端可以用统一的方式处理所有响应。
    """
    yield {"type": "citations", "data": result.get("citations", [])}  # 先发引用
    yield {"type": "content", "data": result.get("answer", "")}  # 再发内容
    yield {"type": "done", "data": {  # 最后发结束标记
        "no_answer": result.get("no_answer", False),
        "stats": result.get("stats", {}),
    }}


def _stream_answer(
    messages: list[dict],
    citations: list[dict],
    search_stats: dict,
) -> Generator[dict, None, None]:
    """
    流式生成答案。

    产出的事件类型：
        citations —— 引用列表（最先发出，前端可立即渲染引用卡片，不用等答案生成完）
        content   —— 文本增量
        error     —— 错误信息
        done      —— 结束标记，附带完整答案文本
    """
    # 先把引用发出去。这是个体验优化：用户在等答案的同时就能看到"找到了哪些资料"，
    # 心理等待感大幅降低
    yield {"type": "citations", "data": citations}

    full_text = ""  # 累积完整答案，用于最后落库
    try:
        for piece in llm.chat_completion_stream(messages):  # 逐段接收模型输出
            full_text += piece  # 累积
            yield {"type": "content", "data": piece}  # 转发给前端
    except llm.LLMError as exc:  # 模型调用失败
        # 降级：把检索到的原文吐给用户
        fallback = "\n\n[AI 生成服务不可用，以下为检索到的原文片段]\n\n"
        for c in citations:
            fallback += f"[{c['index']}]《{c['title']}》\n{c['snippet']}\n\n"
        full_text += fallback  # 也累积进完整文本
        yield {"type": "content", "data": fallback}  # 发给前端
        yield {"type": "error", "data": str(exc)}  # 同时发出错误信息供前端提示

    # 发送结束标记，附带完整文本供前端保存
    yield {"type": "done", "data": {
        "full_text": full_text,
        "no_answer": NO_ANSWER_TEXT in full_text,
        "stats": search_stats,
    }}


# ============================================================
# 三、会话管理
# ============================================================

def create_session(user_id: int, first_question: str) -> int:
    """
    创建一个新的对话会话。
    标题取首个问题的前 30 字，便于用户在历史列表中辨认。
    """
    now = audit.now_iso()  # 当前时间
    title = first_question[:30] + ("…" if len(first_question) > 30 else "")  # 生成标题
    return db.execute(
        "INSERT INTO chat_sessions (user_id, title, created_at, updated_at) VALUES (?,?,?,?)",
        (user_id, title, now, now),
    )


def add_message(
    session_id: int,
    role: str,
    content: str,
    citations: Optional[list[dict]] = None,
) -> int:
    """
    往会话中追加一条消息。

    参数：
        role:      user 或 assistant
        citations: 助手消息的引用列表，会序列化成 JSON 存储
    """
    now = audit.now_iso()  # 当前时间
    # 更新会话的最后活跃时间，便于按活跃度排序
    db.execute("UPDATE chat_sessions SET updated_at = ? WHERE id = ?", (now, session_id))
    return db.execute(
        "INSERT INTO chat_messages (session_id, role, content, citations, created_at) VALUES (?,?,?,?,?)",
        (
            session_id,
            role,
            content,
            # ensure_ascii=False 保证中文正常存储而不是 \uXXXX 转义
            json.dumps(citations, ensure_ascii=False) if citations else None,
            now,
        ),
    )


def get_session_history(session_id: int, limit: int = 20) -> list[dict]:
    """
    取出会话的历史消息，用于多轮对话的上下文。

    参数：
        limit: 最多取多少条（按时间倒序取最近的，再翻转回正序）
    """
    rows = db.query(
        "SELECT role, content FROM chat_messages WHERE session_id = ? ORDER BY id DESC LIMIT ?",
        (session_id, limit),
    )
    # 数据库是倒序取的（拿最近的 N 条），这里翻转回正序供模型理解对话顺序
    return [{"role": r["role"], "content": r["content"]} for r in reversed(rows)]


def list_sessions(user_id: int, limit: int = 30) -> list[dict]:
    """列出用户的历史会话，按最后活跃时间倒序。"""
    rows = db.query(
        """
        SELECT s.id, s.title, s.created_at, s.updated_at,
               (SELECT COUNT(*) FROM chat_messages m WHERE m.session_id = s.id) AS msg_count
        FROM chat_sessions s
        WHERE s.user_id = ?
        ORDER BY s.updated_at DESC
        LIMIT ?
        """,
        (user_id, limit),
    )
    return db.rows_to_dicts(rows)  # 转成字典列表


def get_session_messages(session_id: int, user_id: int) -> list[dict]:
    """
    取出会话的完整消息用于前端展示。
    注意校验 user_id，防止用户通过改 URL 参数看别人的会话（越权防护）。
    """
    # 先验证会话归属
    owner = db.query_one("SELECT user_id FROM chat_sessions WHERE id = ?", (session_id,))
    if not owner or owner["user_id"] != user_id:  # 会话不存在或不属于该用户
        return []  # 返回空，不暴露"该会话存在但你无权访问"这一信息

    rows = db.query(
        "SELECT id, role, content, citations, created_at FROM chat_messages WHERE session_id = ? ORDER BY id",
        (session_id,),
    )
    messages: list[dict] = []  # 结果列表
    for r in rows:  # 逐条处理
        item = db.row_to_dict(r)  # 转字典
        if item.get("citations"):  # 有引用则反序列化成对象
            try:
                item["citations"] = json.loads(item["citations"])
            except json.JSONDecodeError:  # 数据损坏时降级为空列表，不让整个接口崩溃
                item["citations"] = []
        messages.append(item)
    return messages


def save_feedback(
    message_id: Optional[int],
    user_id: Optional[int],
    question: str,
    answer_text: str,
    rating: str,
    comment: str = "",
) -> int:
    """
    保存用户对答案的反馈（FR-5.7）。

    这些数据的价值在于：
    标记为 wrong（有误）的问答可以一键转成评测用例，
    形成"发现问题 → 沉淀用例 → 优化后回归验证"的闭环。
    """
    return db.execute(
        """
        INSERT INTO feedback (message_id, user_id, question, answer, rating, comment, created_at)
        VALUES (?,?,?,?,?,?,?)
        """,
        (message_id, user_id, question[:1000], answer_text[:5000], rating, comment[:1000], audit.now_iso()),
    )
