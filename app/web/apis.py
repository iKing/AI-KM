# -*- coding: utf-8 -*-
"""
JSON API 路由
=============
所有以 /api/ 开头的接口都在此定义。
约定：
- 页面请求未登录时重定向登录页；/api 请求未登录返回 401 JSON（由 auth 装饰器自动处理）
- 统一的返回结构：{"ok": bool, ...}，失败时带 "error" 字段
- 检索类接口内部走权限过滤，无权限数据从数据库层就被排除
"""

import json  # JSON 序列化
import os  # 文件删除
import sqlite3  # SQLite 行类型注解（sqlite3.Row）
import tempfile  # 临时文件
from datetime import datetime, timedelta  # 编辑锁超时计算
from pathlib import Path  # 路径处理
from typing import Optional  # 可选类型注解

from flask import (
    request,          # 请求对象
    jsonify,          # 返回 JSON
    Response,         # 原始响应（SSE / 文件下载）
    stream_with_context,  # 流式响应上下文
    session,          # 会话对象（MFA 暂存待验证用户态用）
    send_from_directory,  # 安全地发送目录内文件（/uploads 静态服务用）
)
from werkzeug.utils import secure_filename  # 文件名安全化（防目录穿越）

from . import bp  # 当前蓝图
from .. import auth, audit, db, config, rag, search, ingest  # 内部模块
from .. import export_docx, export_pdf  # 文档导出（Docx / PDF，P 完整版补齐）
from ..search import engine  # 检索引擎（取其 SearchResult 类型）
from ..providers import llm, embedding  # 模型适配层
from .. import eval as eval_mod  # 评测模块
from .. import mfa as mfa_mod  # P2 MFA 多因子认证模块
from .. import sso as sso_mod  # P2 SSO/LDAP/OIDC 模块


# ============================================================
# 一、认证接口
# ============================================================

@bp.route("/api/login", methods=["POST"])
def api_login():
    """登录接口：接受 JSON 或表单，校验成功后写入会话。"""
    # 兼容 JSON 与表单两种提交方式
    if request.is_json:  # 前端 fetch 一般发 JSON
        data = request.get_json(silent=True) or {}
        username = data.get("username", "")
        password = data.get("password", "")
    else:  # 传统表单提交
        username = request.form.get("username", "")
        password = request.form.get("password", "")

    # 暴力破解防护：先检查该「账号+IP」是否被临时锁定
    ip = auth.client_ip()  # 取真实客户端 IP（兼容反向代理）
    rate_key = f"{username}|{ip}"  # 限流标识
    allowed, remain = auth.login_rate_check(rate_key)  # 查询是否放行
    if not allowed:  # 已触发锁定
        return jsonify({"ok": False, "error": f"尝试次数过多，请 {remain} 秒后再试"}), 429

    user, err = auth.authenticate(username, password)  # 校验账号密码
    if not user:  # 失败
        auth.login_rate_register_failure(rate_key)  # 登记失败，逼近锁定阈值
        audit.log("login_fail", username=username, ip=ip,
                  detail={"原因": err}, result="fail")
        return jsonify({"ok": False, "error": err}), 401

    # P2 MFA：若用户已开启多因子认证，密码正确后还不能发会话，
    # 而是把待验证的用户 ID 暂存到会话，要求前端再提交动态码完成二次验证
    if mfa_mod.is_mfa_enabled(user["id"]):
        session["pending_mfa_user_id"] = user["id"]  # 暂存，等 /api/login/mfa 校验
        audit.log("mfa_verify", user_id=user["id"], username=user["username"], ip=auth.client_ip(),
                  detail={"阶段": "密码通过，待动态码"}, result="success")
        # 返回 mfa_required 标记，前端跳到动态码输入页
        return jsonify({"ok": True, "mfa_required": True})

    auth.login_rate_register_success(rate_key)  # 登录成功，清空失败记录
    auth.login_session(user)  # 写入会话（无 MFA 时直接登录）
    audit.log("login", user_id=user["id"], username=user["username"], ip=auth.client_ip())
    # 返回用户基本信息，前端据此决定跳转（强制改密则去修改密码页）
    return jsonify({
        "ok": True,
        "user": {
            "username": user["username"],
            "display_name": user["display_name"],
            "role": user["role"],
            "must_change_pwd": bool(user["must_change_pwd"]),
        },
    })


@bp.route("/api/login/mfa", methods=["POST"])
def api_login_mfa():
    """
    MFA 二次验证：在密码通过后，用 6 位动态码（或备用码）完成登录。
    只有会话里暂存了 pending_mfa_user_id 才允许调用，防止绕过密码直接试码。
    """
    user_id = session.get("pending_mfa_user_id")  # 取出待验证用户
    if not user_id:  # 没有待验证状态（说明没先过密码）
        return jsonify({"ok": False, "error": "请先完成密码登录"}), 400
    data = request.get_json(silent=True) or {}
    code = (data.get("code") or "").strip()  # 用户输入的动态码或备用码
    if not code:
        return jsonify({"ok": False, "error": "请输入动态码"}), 400

    user = auth.get_user_by_id(user_id)  # 取出用户
    if not user or not user["active"]:  # 用户不存在或停用
        session.pop("pending_mfa_user_id", None)
        return jsonify({"ok": False, "error": "用户状态异常"}), 403

    # 先用 TOTP 动态码校验；失败再尝试备用码（手机丢失场景）
    if mfa_mod.verify_code(user["mfa_secret"], code):
        ok = True  # 动态码正确
    elif mfa_mod.consume_backup_code(user_id, code):
        ok = True  # 备用码正确且已作废
    else:
        ok = False  # 两者都不对

    if not ok:  # 二次验证失败
        # 动态码试错也算一次失败，连续多次后该账号+IP 被锁定，遏制暴力破解
        auth.login_rate_register_failure(f"{user['username']}|{auth.client_ip()}")
        audit.log("mfa_verify", user_id=user_id, username=user["username"], ip=auth.client_ip(),
                  detail={"结果": "动态码错误"}, result="fail")
        return jsonify({"ok": False, "error": "动态码错误"}), 401

    # 验证通过：清除待验证态，正式写入会话
    session.pop("pending_mfa_user_id", None)
    auth.login_session(user)
    audit.log("mfa_verify", user_id=user_id, username=user["username"], ip=auth.client_ip(),
               detail={"结果": "登录成功"}, result="success")
    return jsonify({
        "ok": True,
        "user": {
            "username": user["username"],
            "display_name": user["display_name"],
            "role": user["role"],
            "must_change_pwd": bool(user["must_change_pwd"]),
        },
    })


@bp.route("/api/user/change-password", methods=["POST"])
@auth.login_required
def api_change_password():
    """
    自助修改密码：已登录用户凭旧密码设置新密码（含强度校验）。
    用于「首次登录强制改密」与日常自主改密两种场景。
    """
    user = auth.current_user()  # 当前登录用户（装饰器已保证非 None）
    data = request.get_json(silent=True) or {}  # 读取 JSON 入参
    old_pwd = data.get("old_password", "")  # 原密码
    new_pwd = data.get("new_password", "")  # 新密码

    # 第一步：校验原密码，防止他人拿着会话乱改密码
    ok_user, _ = auth.authenticate(user["username"], old_pwd)  # 重新校验旧密码
    if not ok_user:  # 原密码不对
        audit.log("user_manage", user_id=user["id"], username=user["username"], ip=auth.client_ip(),
                  detail={"动作": "修改密码", "结果": "原密码错误"}, result="fail")
        return jsonify({"ok": False, "error": "原密码错误"}), 400

    # 第二步：新密码强度校验，弱口令直接拒绝
    ok, reason = auth.validate_password(new_pwd)  # 校验新密码
    if not ok:  # 不达标
        return jsonify({"ok": False, "error": f"密码不符合安全策略：{reason}"}), 400

    # 第三步：落库（change_password 内部会再校验一次，并清除强制改密标记）
    try:
        auth.change_password(user["id"], new_pwd)  # 修改密码
    except ValueError as e:  # 兜底捕获
        return jsonify({"ok": False, "error": str(e)}), 400
    audit.log("user_manage", user_id=user["id"], username=user["username"], ip=auth.client_ip(),
              detail={"动作": "修改密码"}, result="success")
    return jsonify({"ok": True})  # 成功


# ============================================================
# 二、检索接口
# ============================================================

@bp.route("/api/search")
@auth.login_required
def api_search():
    """知识检索：混合/关键词/语义三种模式，权限范围内召回。"""
    user = auth.current_user()  # 当前用户（决定可见范围）
    query = (request.args.get("q") or "").strip()  # 查询词
    if not query:  # 空查询直接返回空
        return jsonify({"ok": True, "results": [], "stats": {}})

    # 解析过滤条件（前端筛选器传参）
    filters: dict = {}
    if request.args.get("category_l1"):
        filters["category_l1"] = request.args.get("category_l1")
    if request.args.get("category_l2"):
        filters["category_l2"] = request.args.get("category_l2")
    if request.args.get("department_id"):
        filters["department_id"] = request.args.get("department_id")
    if request.args.get("security_level"):
        filters["security_level"] = request.args.get("security_level")
    if request.args.get("tag"):  # 按标签过滤（P1 标签体系）
        filters["tag"] = request.args.get("tag")

    mode = request.args.get("mode", "hybrid")  # 默认混合检索
    try:
        top_k = int(request.args.get("top_k", config.SEARCH_TOP_K))
    except ValueError:  # 非法数字兜底默认
        top_k = config.SEARCH_TOP_K

    # 调用检索引擎。source="web" 用于区分统计来源
    results, stats = search.engine.search(
        query, user=user, filters=filters, top_k=top_k, mode=mode, source="web",
    )
    # 把结果对象转成字典列表返回前端
    return jsonify({
        "ok": True,
        "results": [r.to_dict() for r in results],
        "stats": stats,
    })


# ============================================================
# 三、AI 问答接口（Server-Sent Events 流式）
# ============================================================

@bp.route("/api/ask", methods=["POST"])
@auth.api_key_required  # 同时支持浏览器会话与 API Key 调用
def api_ask():
    """
    流式问答接口（SSE）。
    产出事件类型：citations / content / done / error。
    done 事件携带 session_id 与 message_id，供前端关联会话。
    """
    data = request.get_json(silent=True) or {}
    question = (data.get("question") or "").strip()  # 用户问题
    session_id = data.get("session_id")  # 可选，多轮对话用
    include_confidential = bool(data.get("include_confidential"))  # 是否纳入机密

    if not question:  # 空问题
        return jsonify({"ok": False, "error": "请输入问题"}), 400

    user = auth.current_user()  # 可能来自会话或 API Key（装饰器已注入 g._current_user）

    # 确定会话：传了有效 session_id 才复用，否则新建
    if session_id:
        sess = db.query_one(
            "SELECT id FROM chat_sessions WHERE id = ? AND user_id = ?",
            (session_id, user["id"]),
        )
        session_id = sess["id"] if sess else None
    if not session_id:  # 新建会话
        session_id = rag.create_session(user["id"], question)

    # 取历史对话，实现多轮追问（只取最近若干轮）
    history = rag.get_session_history(session_id, limit=config.RAG_HISTORY_TURNS * 2)

    def event_stream():
        """生成器：逐段把 RAG 事件转成 SSE 格式输出。"""
        full_text = ""  # 累积完整答案
        citations: list = []  # 引用列表
        for ev in rag.answer(
            question,
            user=user,
            include_confidential=include_confidential,
            history=history,
            stream=True,
        ):
            if ev["type"] == "citations":  # 引用事件
                citations = ev["data"]
            elif ev["type"] == "content":  # 文本增量
                full_text += ev["data"]
            elif ev["type"] == "done":  # 结束事件：落库
                # 把用户问题与助手答案写入会话，实现对话持久化
                rag.add_message(session_id, "user", question)
                mid = rag.add_message(session_id, "assistant", full_text, citations=citations)
                # 在 done 事件里附带会话信息，前端据此更新 URL
                ev["data"]["session_id"] = session_id
                ev["data"]["message_id"] = mid
            # 统一序列化成 SSE 行：data: {json}\n\n
            yield f"data: {json.dumps(ev, ensure_ascii=False)}\n\n"

    # 返回 SSE 流。stream_with_context 保证能访问请求上下文
    return Response(stream_with_context(event_stream()), mimetype="text/event-stream")


# ============================================================
# 四、文档列表与审核接口
# ============================================================

@bp.route("/api/documents")
@auth.login_required
def api_documents():
    """文档列表：支持分类/部门/状态/关键词过滤 + 分页，全程带权限过滤。"""
    user = auth.current_user()
    # 读取过滤参数
    category_l1 = request.args.get("category_l1")
    department_id = request.args.get("department_id")
    status = request.args.get("status")
    keyword = request.args.get("keyword")
    try:
        page = max(1, int(request.args.get("page", 1)))  # 页码下限 1
        per_page = min(100, max(1, int(request.args.get("per_page", 20))))  # 每页上限 100
    except ValueError:
        page, per_page = 1, 20

    # 权限过滤条件（数据层隔离，无权限文档查不出）
    where_sql, params = auth.visibility_filter(user, "d")
    conditions = [where_sql]
    if category_l1:
        conditions.append("d.category_l1 = ?")
        params.append(category_l1)
    if department_id:
        conditions.append("d.department_id = ?")
        params.append(int(department_id))
    if status:
        conditions.append("d.status = ?")
        params.append(status)
    if keyword:
        conditions.append("(d.title LIKE ? OR d.summary LIKE ?)")
        params.extend([f"%{keyword}%", f"%{keyword}%"])
    if request.args.get("tag"):  # 按标签过滤（P1 标签体系）
        # 通过 doc_tags 关联 tags，找出打了该标签的文档
        conditions.append(
            "d.id IN (SELECT dt.doc_id FROM doc_tags dt "
            "JOIN tags t ON t.id = dt.tag_id WHERE t.name = ?)"
        )
        params.append(request.args.get("tag"))
    # 按知识树节点过滤：连同其所有子孙节点一起筛选（递归展开）
    space_id = request.args.get("space_id")
    if space_id:
        all_spaces = db.query("SELECT id, parent_id FROM knowledge_spaces")  # 取出全部空间用于递归
        desc_ids = _collect_space_descendants(all_spaces, int(space_id))  # 收集子孙节点 ID
        if desc_ids:  # 正常情况
            ph = ",".join("?" for _ in desc_ids)  # 构造 IN 占位符
            conditions.append(f"d.space_id IN ({ph})")  # 限定在该子树范围内
            params.extend(desc_ids)  # 加入参数
        else:
            conditions.append("1=0")  # 节点不存在，直接返回空结果
    where = " AND ".join(conditions)  # 拼成完整 WHERE

    # 先查总数用于分页
    total = db.query_one(f"SELECT COUNT(*) AS c FROM documents d WHERE {where}", params)["c"]
    # 再查当前页数据
    rows = db.query(
        """
        SELECT d.*, COALESCE(dept.name, '') AS dept_name
        FROM documents d
        LEFT JOIN departments dept ON dept.id = d.department_id
        WHERE {where}
        ORDER BY d.id DESC
        LIMIT ? OFFSET ?
        """.replace("{where}", where),
        params + [per_page, (page - 1) * per_page],
    )
    return jsonify({
        "ok": True,
        "total": total,
        "items": db.rows_to_dicts(rows),
        "page": page,
        "per_page": per_page,
    })


@bp.route("/api/review/<int:doc_id>", methods=["POST"])
@auth.login_required
@auth.role_required("admin", "reviewer")
def api_review(doc_id: int):
    """文档审核：通过（发布）或退回（带理由）。"""
    user = auth.current_user()
    data = request.get_json(silent=True) or {}
    action = data.get("action")  # approve / reject
    reason = (data.get("reason") or "").strip()

    doc = db.query_one("SELECT * FROM documents WHERE id = ?", (doc_id,))
    if not doc:
        return jsonify({"ok": False, "error": "文档不存在"}), 404
    if doc["status"] != "pending_review":
        return jsonify({"ok": False, "error": "该文档不在待审核状态"}), 400

    now = audit.now_iso()  # 当前时间
    if action == "approve":  # 通过 → 发布
        db.execute(
            "UPDATE documents SET status='published', reviewed_by=?, reviewed_at=? WHERE id=?",
            (user["id"], now, doc_id),
        )
        audit.log("review", user_id=user["id"], username=user["username"], ip=auth.client_ip(),
                  target_type="document", target_id=doc_id, detail={"动作": "通过发布"}, result="success")
        return jsonify({"ok": True, "message": "已发布"})
    elif action == "reject":  # 退回
        db.execute(
            "UPDATE documents SET status='rejected', reject_reason=?, reviewed_by=?, reviewed_at=? WHERE id=?",
            (reason, user["id"], now, doc_id),
        )
        audit.log("reject", user_id=user["id"], username=user["username"], ip=auth.client_ip(),
                  target_type="document", target_id=doc_id, detail={"动作": "退回", "原因": reason}, result="success")
        return jsonify({"ok": True, "message": "已退回"})
    return jsonify({"ok": False, "error": "未知操作"}), 400


# ============================================================
# 五、知识入库接口
# ============================================================

@bp.route("/api/upload", methods=["POST"])
@auth.login_required
@auth.role_required("admin", "reviewer", "contributor")
def api_upload():
    """单文件入库：表单含文件 + 元数据字段。"""
    user = auth.current_user()
    if "file" not in request.files:  # 没收到文件字段
        return jsonify({"ok": False, "error": "未选择文件"}), 400
    f = request.files["file"]
    if not f.filename:  # 文件名为空
        return jsonify({"ok": False, "error": "文件名为空"}), 400

    # 先把上传内容落到一个临时文件，避免直接操作内存大文件
    suffix = Path(f.filename).suffix.lower()  # 扩展名
    tmp_fd, tmp_path = tempfile.mkstemp(suffix=suffix)  # 创建临时文件
    os.close(tmp_fd)  # 关闭文件描述符，后续用路径写
    try:
        f.save(tmp_path)  # 保存上传内容
    except Exception as exc:  # 保存失败
        os.remove(tmp_path)  # 清理临时文件
        return jsonify({"ok": False, "error": f"文件保存失败：{exc}"}), 500

    # 从表单组装元数据字典
    meta = {
        "title": (request.form.get("title") or "").strip(),
        "category_l1": request.form.get("category_l1"),
        "category_l2": request.form.get("category_l2") or None,
        "department_id": request.form.get("department_id"),
        "security_level": request.form.get("security_level") or "internal",
        "quality_level": request.form.get("quality_level") or "normal",
        "owner": (request.form.get("owner") or "").strip(),
        "effective_date": request.form.get("effective_date") or None,
        "expire_date": request.form.get("expire_date") or None,
        "source": request.form.get("source") or None,
        "summary": request.form.get("summary") or None,
    }

    # 仅管理员/审核员可勾选"跳过审核直接发布"；贡献者一律走审核
    auto_publish = (
        request.form.get("auto_publish") == "1"
        and user["role"] in ("admin", "reviewer")
    )

    try:
        result = ingest.ingest_file(
            tmp_path, meta,
            user_id=user["id"],
            original_name=f.filename,
            auto_publish=auto_publish,
        )
    except Exception as exc:  # 兜底异常
        os.remove(tmp_path)
        return jsonify({"ok": False, "error": f"入库异常：{exc}"}), 500

    os.remove(tmp_path)  # 清理临时文件

    # 记录审计：入库成功或失败都记，便于追溯
    audit.log(
        "upload", user_id=user["id"], username=user["username"], ip=auth.client_ip(),
        target_type="document", target_id=result.get("doc_id"),
        detail={"标题": meta.get("title"), "文件名": f.filename},
        result="success" if result["ok"] else "fail",
    )
    # 返回码：成功 200，元数据/格式等可预期失败用 422，其余 200（前端按 ok 字段判断）
    return jsonify(result), (200 if result["ok"] else 422)


# ============================================================
# 六、会话接口
# ============================================================

@bp.route("/api/chat/sessions")
@auth.login_required
def api_sessions():
    """当前用户的会话列表。"""
    user = auth.current_user()
    return jsonify({"ok": True, "sessions": rag.list_sessions(user["id"])})


@bp.route("/api/chat/sessions/<int:sid>")
@auth.login_required
def api_session_messages(sid: int):
    """某会话的完整消息（带越权校验）。"""
    user = auth.current_user()
    messages = rag.get_session_messages(sid, user["id"])  # 内部已校验归属
    return jsonify({"ok": True, "messages": messages})


# ============================================================
# 七、效能看板接口
# ============================================================

@bp.route("/api/departments")
@auth.login_required
def api_departments():
    """部门列表（用于各页面的下拉框）。"""
    rows = db.query("SELECT id, name FROM departments ORDER BY name")
    return jsonify({"ok": True, "departments": db.rows_to_dicts(rows)})


@bp.route("/api/dashboard/stats")
@auth.login_required
def api_dashboard():
    """效能看板数据：检索统计 + 知识库概况。"""
    # 检索维度统计（最近 30 天）
    search_stats = search.engine.search_stats(days=30)
    # 知识库文档总量与状态分布
    doc_total = db.query_one("SELECT COUNT(*) AS c FROM documents")["c"]
    status_rows = db.query(
        "SELECT status, COUNT(*) AS c FROM documents GROUP BY status"
    )
    status_dist = {r["status"]: r["c"] for r in status_rows}
    # 知识片段总量（反映检索粒度）
    chunk_total = db.query_one("SELECT COUNT(*) AS c FROM chunks")["c"]
    return jsonify({
        "ok": True,
        "search": search_stats,
        "doc_total": doc_total,
        "status_dist": status_dist,
        "chunk_total": chunk_total,
    })


@bp.route("/api/home")
@auth.login_required
def api_home():
    """
    知识库首页（专业工作台）聚合接口：一次调用返回工作台所需的全部数据。

    返回：六大一级分类及其可见文档数、最近更新文档、我的待办、
    热门检索词、统计概览。前端据此渲染类 BookStack 的企业知识库工作台，
    避免首页加载时发起多次请求（N+1）。
    """
    user = auth.current_user()
    # 统一权限过滤条件（数据层隔离，无权限文档查不出）
    where_sql, params = auth.visibility_filter(user, "d")

    # ① 六大一级分类 + 各分类可见文档数（用于分类导航入口）
    cat_rows = db.query(
        f"SELECT d.category_l1 AS c, COUNT(*) AS cn FROM documents d WHERE {where_sql} GROUP BY d.category_l1",
        params,
    )
    cat_counts = {r["c"]: r["cn"] for r in cat_rows}
    # 分类图标映射（前端卡片展示用，与 CATEGORIES_L1 顺序对齐）
    cat_icons = {
        "POLICY": "📜", "PROJECT": "🗂️", "CUSTOMER": "🤝",
        "PRODUCT": "⚙️", "PROCESS": "📋", "TRAINING": "🎓",
    }
    categories = [
        {"key": k, "name": v, "icon": cat_icons.get(k, "📁"), "count": cat_counts.get(k, 0)}
        for k, v in config.CATEGORIES_L1.items()
    ]

    # ② 最近更新：可见范围内已发布文档，按更新时间倒序取前 8
    recent_rows = db.query(
        f"""
        SELECT d.id, d.title, d.category_l1, d.security_level, d.updated_at, d.summary,
               COALESCE(u.display_name, '') AS owner_name
        FROM documents d
        LEFT JOIN users u ON u.id = d.created_by
        WHERE {where_sql} AND d.status = 'published'
        ORDER BY d.updated_at DESC
        LIMIT 8
        """,
        params,
    )

    # ③ 我的待办：按角色与创建人聚合（审核类 / 我待处理 / 知识缺口）
    todos = []
    # 待我审核：审核员与管理员可见的待审文档
    if user["role"] in ("admin", "reviewer"):
        pending = db.query_one(
            f"SELECT COUNT(*) AS c FROM documents d WHERE {where_sql} AND d.status='pending_review'",
            params,
        )["c"]
        if pending:
            todos.append({"type": "review", "title": "待我审核", "desc": "有新的知识文档等待审核发布", "count": pending, "url": "/review"})
    # 我创建的待发布 / 被退回
    my_pending = db.query_one(
        "SELECT COUNT(*) AS c FROM documents WHERE created_by=? AND status IN ('pending_review','rejected')",
        (user["id"],),
    )["c"]
    if my_pending:
        todos.append({"type": "mine", "title": "我待处理", "desc": "我提交的文档待审核或被退回", "count": my_pending, "url": "/spaces"})
    # 知识缺口：近期零结果检索词数量，提示补充知识
    search_stats = search.engine.search_stats(days=30)
    zero_cnt = len(search_stats.get("zero_queries", []) or [])
    if zero_cnt:
        todos.append({"type": "gap", "title": "知识缺口待补", "desc": "近期有检索零结果，建议补充知识", "count": zero_cnt, "url": "/search"})

    # ④ 热门检索 Top8
    hot = (search_stats.get("top_queries", []) or [])[:8]

    # ⑤ 统计概览：文档总量 + 知识片段总量
    stats = {
        "doc_total": db.query_one("SELECT COUNT(*) AS c FROM documents")["c"],
        "chunk_total": db.query_one("SELECT COUNT(*) AS c FROM chunks")["c"],
    }

    return jsonify({
        "ok": True,
        "categories": categories,
        "recent": db.rows_to_dicts(recent_rows),
        "todos": todos,
        "hot_queries": hot,
        "stats": stats,
    })


# ============================================================
# 八、管理后台接口（仅管理员）
# ============================================================

@bp.route("/api/admin/users", methods=["GET", "POST"])
@auth.login_required
@auth.admin_required
def api_admin_users():
    """用户管理：GET 列表 / POST 新建。"""
    if request.method == "GET":
        rows = db.query(
            """
            SELECT u.id, u.username, u.display_name, u.role, u.department_id,
                   u.max_security, u.cross_dept, u.active, u.must_change_pwd,
                   u.last_login_at, dept.name AS dept_name
            FROM users u
            LEFT JOIN departments dept ON dept.id = u.department_id
            ORDER BY u.id
            """
        )
        return jsonify({"ok": True, "users": db.rows_to_dicts(rows)})

    # POST：新建用户
    data = request.get_json(silent=True) or {}
    username = (data.get("username") or "").strip()
    display_name = (data.get("display_name") or "").strip()
    if not username or not display_name:
        return jsonify({"ok": False, "error": "登录名和显示名必填"}), 400
    if auth.get_user_by_username(username):
        return jsonify({"ok": False, "error": "用户名已存在"}), 400
    # 初始密码：未提供则用系统生成的安全随机密码
    password = data.get("password") or auth.generate_password()
    # 管理员显式指定密码时，先校验强度，避免弱口令被创建
    if data.get("password"):
        ok, reason = auth.validate_password(password)  # 校验密码强度
        if not ok:  # 不达标
            return jsonify({"ok": False, "error": f"密码不符合安全策略：{reason}"}), 400
    try:
        uid = auth.create_user(
            username=username,
            display_name=display_name,
            password=password,
            role=data.get("role", "user"),
            department_id=int(data["department_id"]) if data.get("department_id") else None,
            max_security=data.get("max_security", "internal"),
            cross_dept=bool(data.get("cross_dept")),
            must_change_pwd=True,
        )
    except ValueError as e:  # create_user 内部也会再校验一次，捕获并返回友好提示
        return jsonify({"ok": False, "error": str(e)}), 400
    audit.log("user_manage", user_id=auth.current_user()["id"],
              username=auth.current_user()["username"], ip=auth.client_ip(),
              target_type="user", target_id=uid, detail={"动作": "新建用户", "账号": username},
              result="success")
    # 把初始密码返回给管理员（仅此一次，需妥善转交）
    return jsonify({"ok": True, "user_id": uid, "password": password})


@bp.route("/api/admin/users/<int:uid>/reset", methods=["POST"])
@auth.login_required
@auth.admin_required
def api_admin_user_reset(uid: int):
    """重置用户密码（生成新随机密码）。"""
    if not db.query_one("SELECT id FROM users WHERE id = ?", (uid,)):
        return jsonify({"ok": False, "error": "用户不存在"}), 404
    new_pwd = auth.generate_password()  # 生成新密码
    auth.change_password(uid, new_pwd)  # 修改并清除强制改密标记
    audit.log("user_manage", user_id=auth.current_user()["id"],
              username=auth.current_user()["username"], ip=auth.client_ip(),
              target_type="user", target_id=uid, detail={"动作": "重置密码"}, result="success")
    return jsonify({"ok": True, "password": new_pwd})


@bp.route("/api/admin/users/<int:uid>/toggle", methods=["POST"])
@auth.login_required
@auth.admin_required
def api_admin_user_toggle(uid: int):
    """启用/停用用户（停用而非删除，保留审计痕迹）。"""
    user = db.query_one("SELECT * FROM users WHERE id = ?", (uid,))
    if not user:
        return jsonify({"ok": False, "error": "用户不存在"}), 404
    if user["username"] == "admin" and user["active"]:
        return jsonify({"ok": False, "error": "管理员账号不可停用"}), 400
    new_active = 0 if user["active"] else 1  # 取反
    db.execute("UPDATE users SET active = ? WHERE id = ?", (new_active, uid))
    audit.log("user_manage", user_id=auth.current_user()["id"],
              username=auth.current_user()["username"], ip=auth.client_ip(),
              target_type="user", target_id=uid, detail={"动作": "切换启用状态", "新状态": new_active},
              result="success")
    return jsonify({"ok": True, "active": new_active})


@bp.route("/api/admin/apikeys", methods=["GET", "POST"])
@auth.login_required
@auth.admin_required
def api_admin_apikeys():
    """API 密钥管理：GET 列表 / POST 新建。"""
    if request.method == "GET":
        rows = db.query(
            "SELECT id, name, key_prefix, active, call_count, last_used_at, created_at FROM api_keys ORDER BY id DESC"
        )
        return jsonify({"ok": True, "keys": db.rows_to_dicts(rows)})

    data = request.get_json(silent=True) or {}
    name = (data.get("name") or "").strip()
    if not name:
        return jsonify({"ok": False, "error": "密钥名称必填"}), 400
    admin = auth.current_user()
    raw_key, key_id = auth.create_api_key(name, admin["id"])  # 创建并返回明文密钥
    audit.log("user_manage", user_id=admin["id"], username=admin["username"], ip=auth.client_ip(),
              target_type="apikey", target_id=key_id, detail={"动作": "创建API密钥", "名称": name},
              result="success")
    # 明文密钥只在此返回一次
    return jsonify({"ok": True, "key_id": key_id, "raw_key": raw_key})


@bp.route("/api/admin/apikeys/<int:kid>/revoke", methods=["POST"])
@auth.login_required
@auth.admin_required
def api_admin_apikey_revoke(kid: int):
    """吊销 API 密钥。"""
    db.execute("UPDATE api_keys SET active = 0 WHERE id = ?", (kid,))
    audit.log("user_manage", user_id=auth.current_user()["id"],
              username=auth.current_user()["username"], ip=auth.client_ip(),
              target_type="apikey", target_id=kid, detail={"动作": "吊销API密钥"}, result="success")
    return jsonify({"ok": True})


@bp.route("/api/admin/audit")
@auth.login_required
@auth.admin_required
def api_admin_audit():
    """审计日志查询（分页 + 过滤）。"""
    rows, total = audit.search_logs(
        action=request.args.get("action"),
        user_id=int(request.args.get("user_id")) if request.args.get("user_id") else None,
        keyword=request.args.get("keyword"),
        date_from=request.args.get("date_from"),
        date_to=request.args.get("date_to"),
        sensitive_only=request.args.get("sensitive") == "1",
        limit=int(request.args.get("limit", 100)),
        offset=int(request.args.get("offset", 0)),
    )
    # 把动作编码转成中文名，前端直接显示
    for r in rows:
        r["action_name"] = audit.ACTIONS.get(r["action"], r["action"])
    return jsonify({"ok": True, "logs": rows, "total": total})


@bp.route("/api/admin/audit/export")
@auth.login_required
@auth.admin_required
def api_admin_audit_export():
    """导出审计日志为 CSV（带 BOM，Excel 可正常打开）。"""
    rows, _ = audit.search_logs(
        action=request.args.get("action"),
        keyword=request.args.get("keyword"),
        date_from=request.args.get("date_from"),
        date_to=request.args.get("date_to"),
        sensitive_only=request.args.get("sensitive") == "1",
        limit=100000,  # 导出最多 10 万条
    )
    csv_text = audit.export_csv(rows)  # 生成 CSV 文本
    audit.log("export_audit", user_id=auth.current_user()["id"],
              username=auth.current_user()["username"], ip=auth.client_ip(),
              detail={"条数": len(rows)}, result="success")
    # 返回文件下载响应
    return Response(
        csv_text,
        mimetype="text/csv; charset=utf-8",
        headers={"Content-Disposition": "attachment; filename=audit_logs.csv"},
    )


@bp.route("/api/admin/config")
@auth.login_required
@auth.admin_required
def api_admin_config():
    """查看当前运行时配置（密钥脱敏）。"""
    return jsonify({"ok": True, "config": config.get_runtime_config()})


@bp.route("/api/admin/config/health", methods=["POST"])
@auth.login_required
@auth.admin_required
def api_admin_config_health():
    """测试模型连通性（LLM + Embedding）。"""
    return jsonify({
        "ok": True,
        "llm": llm.llm_health(),
        "embedding": embedding.embedding_health(),
    })


@bp.route("/api/admin/eval/run", methods=["POST"])
@auth.login_required
@auth.admin_required
def api_admin_eval_run():
    """运行检索准确率评测。"""
    data = request.get_json(silent=True) or {}
    mode = data.get("mode", "hybrid")  # 评测模式
    top_k = int(data.get("top_k", 5))  # K 值
    admin = auth.current_user()
    result = eval_mod.run_eval(mode=mode, top_k=top_k, run_by=admin["id"])
    audit.log("eval_run", user_id=admin["id"], username=admin["username"], ip=auth.client_ip(),
              detail={"模式": mode, "K": top_k, "recall": result.get("recall_at_k")},
              result="success" if result.get("ok") else "fail")
    return jsonify(result)


@bp.route("/api/admin/eval/runs")
@auth.login_required
@auth.admin_required
def api_admin_eval_runs():
    """评测历史列表与最新明细。"""
    runs = eval_mod.list_eval_runs()
    return jsonify({"ok": True, "runs": runs})


@bp.route("/api/admin/reindex", methods=["POST"])
@auth.login_required
@auth.admin_required
def api_admin_reindex():
    """重建索引：补缺失向量（快）或全量重建（切模型后用）。"""
    data = request.get_json(silent=True) or {}
    scope = data.get("scope", "missing")  # missing / all
    result = ingest.reindex_all(only_missing_vectors=(scope == "missing"))
    audit.log("reindex", user_id=auth.current_user()["id"],
              username=auth.current_user()["username"], ip=auth.client_ip(),
              detail={"范围": scope, "成功": result.get("success"), "失败": result.get("failed")},
              result="success")
    return jsonify({"ok": True, **result})


# ============================================================
# 四-B、知识树接口（BookStack 式层级组织）
# ============================================================

def _space_visible(user, sp):
    """
    判断用户能否看到某个知识树节点。
    规则与文档可见性一致（密级 + 部门），但空间没有"状态"概念：
    - 密级高于用户许可的不可见
    - 管理员/审核员/跨部门权限用户不受部门限制
    - 无归属部门的空间全公司可见
    """
    if not user:  # 未登录一律不可见
        return False
    user_rank = config.SECURITY_RANK.get(user["max_security"], 2)  # 用户密级许可等级
    lvl_rank = config.SECURITY_RANK.get(sp["security_level"], 2)  # 节点自身密级等级
    if lvl_rank > user_rank:  # 密级超出许可
        return False
    if user["role"] in ("admin", "reviewer") or user.get("cross_dept"):  # 豁免部门限制的角色
        return True
    if sp["department_id"] is None:  # 全公司可见的空间
        return True
    return sp["department_id"] == user.get("department_id")  # 仅本部门可见


def _collect_space_descendants(rows, root_id):
    """收集某个知识树节点的所有子孙节点 ID（含自身），用于按空间递归检索文档。"""
    by_parent = {}  # 按父节点分组的子节点映射
    for r in rows:  # 遍历所有空间节点
        by_parent.setdefault(r["parent_id"], []).append(r["id"])
    result = [root_id]  # 结果从根开始
    stack = [root_id]  # 用栈做深度优先遍历
    while stack:  # 栈非空就继续
        cur = stack.pop()  # 弹出当前节点
        for child in by_parent.get(cur, []):  # 遍历其直接子节点
            result.append(child)  # 加入结果
            stack.append(child)  # 子节点入栈继续向下
    return result


def _build_space_tree(nodes):
    """把扁平节点列表组装成嵌套树结构（供前端递归渲染）。"""
    by_id = {n["id"]: n for n in nodes}  # 按 ID 建索引
    for n in nodes:  # 初始化子节点列表
        n["children"] = []
    roots = []  # 顶层节点集合
    for n in nodes:  # 遍历每个节点挂到父节点下
        if n["parent_id"] and n["parent_id"] in by_id:
            by_id[n["parent_id"]]["children"].append(n)
        else:
            roots.append(n)  # 没有有效父节点则作为根
    return roots


@bp.route("/api/spaces/tree")
@auth.login_required
def api_space_tree():
    """返回当前用户可见的知识树（嵌套结构 + 扁平列表 + 各节点文档数）。"""
    user = auth.current_user()
    rows = db.query("SELECT * FROM knowledge_spaces ORDER BY sort_order, name")
    nodes = [db.row_to_dict(r) for r in rows]
    visible = [n for n in nodes if _space_visible(user, n)]  # 按权限过滤
    # 统计每个空间下的文档数量
    counts = {
        r["space_id"]: r["c"]
        for r in db.query("SELECT space_id, COUNT(*) AS c FROM documents WHERE space_id IS NOT NULL GROUP BY space_id")
    }
    for n in visible:  # 把文档数挂到节点上
        n["doc_count"] = counts.get(n["id"], 0)
    tree = _build_space_tree(visible)  # 组装嵌套树
    return jsonify({"ok": True, "tree": tree, "flat": visible})


@bp.route("/api/spaces", methods=["POST"])
@auth.login_required
@auth.role_required("admin", "reviewer", "contributor")
def api_create_space():
    """新建知识树节点（书架/书/章）。"""
    user = auth.current_user()
    data = request.get_json(silent=True) or {}
    name = (data.get("name") or "").strip()
    if not name:  # 名称必填
        return jsonify({"ok": False, "error": "名称必填"}), 400
    parent_id = int(data["parent_id"]) if data.get("parent_id") else None
    kind = data.get("kind", "shelf")
    if kind not in config.SPACE_KINDS:  # 非法类型兜底为书架
        kind = "shelf"
    dept_id = int(data["department_id"]) if data.get("department_id") else None
    sec = data.get("security_level", "internal")
    if sec not in config.SECURITY_LEVELS:  # 非法密级兜底内部
        sec = "internal"
    sid = db.execute(
        """
        INSERT INTO knowledge_spaces
            (parent_id, name, kind, department_id, security_level, description, sort_order, created_by, created_at)
        VALUES (?,?,?,?,?,?,?,?,?)
        """,
        (parent_id, name, kind, dept_id, sec, data.get("description") or None,
         int(data.get("sort_order", 0)), user["id"], audit.now_iso()),
    )
    audit.log("space_manage", user_id=user["id"], username=user["username"], ip=auth.client_ip(),
              target_type="space", target_id=sid, detail={"动作": "新建节点", "名称": name, "类型": kind}, result="success")
    return jsonify({"ok": True, "space_id": sid})


@bp.route("/api/spaces/<int:sid>", methods=["PUT"])
@auth.login_required
@auth.role_required("admin", "reviewer", "contributor")
def api_update_space(sid: int):
    """更新知识树节点属性（改名/移动/改密级等）。"""
    user = auth.current_user()
    sp = db.query_one("SELECT * FROM knowledge_spaces WHERE id = ?", (sid,))
    if not sp:  # 节点不存在
        return jsonify({"ok": False, "error": "节点不存在"}), 404
    data = request.get_json(silent=True) or {}
    name = (data.get("name") or "").strip() or sp["name"]  # 空名则保持原值
    kind = data.get("kind", sp["kind"])
    if kind not in config.SPACE_KINDS:  # 非法类型兜底原值
        kind = sp["kind"]
    dept_id = data.get("department_id", sp["department_id"])
    if dept_id is not None:
        dept_id = int(dept_id)
    sec = data.get("security_level", sp["security_level"])
    if sec not in config.SECURITY_LEVELS:  # 非法密级兜底原值
        sec = sp["security_level"]
    parent_id = data.get("parent_id", sp["parent_id"])
    if parent_id is not None:
        parent_id = int(parent_id)
        if parent_id == sid:  # 不能把自己设为自己的父节点（成环）
            return jsonify({"ok": False, "error": "不能挂载到自身"}), 400
    db.execute(
        """
        UPDATE knowledge_spaces
        SET name=?, kind=?, department_id=?, security_level=?, description=?, sort_order=?, parent_id=?
        WHERE id=?
        """,
        (name, kind, dept_id, sec, data.get("description", sp["description"]),
         int(data.get("sort_order", sp["sort_order"])), parent_id, sid),
    )
    audit.log("space_manage", user_id=user["id"], username=user["username"], ip=auth.client_ip(),
              target_type="space", target_id=sid, detail={"动作": "更新节点", "名称": name}, result="success")
    return jsonify({"ok": True})


@bp.route("/api/spaces/<int:sid>", methods=["DELETE"])
@auth.login_required
@auth.role_required("admin", "reviewer", "contributor")
def api_delete_space(sid: int):
    """删除知识树节点。要求节点下无子节点且无文档，避免误删造成悬挂引用。"""
    user = auth.current_user()
    sp = db.query_one("SELECT * FROM knowledge_spaces WHERE id = ?", (sid,))
    if not sp:
        return jsonify({"ok": False, "error": "节点不存在"}), 404
    child = db.query_one("SELECT id FROM knowledge_spaces WHERE parent_id = ? LIMIT 1", (sid,))  # 查子节点
    if child:
        return jsonify({"ok": False, "error": "请先删除该节点下的子节点"}), 400
    doc = db.query_one("SELECT id FROM documents WHERE space_id = ? LIMIT 1", (sid,))  # 查挂载文档
    if doc:
        return jsonify({"ok": False, "error": "请先将该节点下的文档移走或删除"}), 400
    db.execute("DELETE FROM knowledge_spaces WHERE id = ?", (sid,))
    audit.log("space_manage", user_id=user["id"], username=user["username"], ip=auth.client_ip(),
              target_type="space", target_id=sid, detail={"动作": "删除节点", "名称": sp["name"]}, result="success")
    return jsonify({"ok": True})


# ============================================================
# 五-B、文档在线编辑与版本管理（P0-2 版本历史 / P0-3 在线编辑器）
# ============================================================

def _visible_doc(doc_id: int, user) -> Optional[sqlite3.Row]:
    """
    按权限取出一篇文档；无权限或不存在返回 None。

    在线编辑/版本管理都基于"已经能看到这篇文档"的前提，
    所以必须复用与文档详情页一致的权限过滤，防止越权编辑/回滚。
    """
    where_sql, params = auth.visibility_filter(user, "d")  # 复用统一的权限过滤条件
    return db.query_one(
        f"SELECT * FROM documents d WHERE d.id = ? AND {where_sql}",
        [doc_id] + params,
    )


@bp.route("/api/documents/<int:doc_id>/raw")
@auth.login_required
def api_doc_raw(doc_id: int):
    """取文档当前正文（body_text）与基础元数据，供在线编辑器初始化。"""
    user = auth.current_user()
    doc = _visible_doc(doc_id, user)  # 校验可见性
    if not doc:
        return jsonify({"ok": False, "error": "文档不存在或您无权查看"}), 404
    return jsonify({
        "ok": True,
        "doc": {
            "id": doc["id"],
            "title": doc["title"],
            "body_text": doc["body_text"] or "",  # 正文权威源（纯文本），编辑器缺省加载它
            "body_html": doc["body_html"] or "",  # 富文本 HTML（有则优先用于编辑器与展示）
            "security_level": doc["security_level"],
            "category_l1": doc["category_l1"],
            "category_l2": doc["category_l2"],
            "summary": doc["summary"],
            "version": doc["version"],
            "content_length": doc["content_length"],
        },
    })


@bp.route("/api/documents/<int:doc_id>/edit", methods=["POST"])
@auth.login_required
def api_doc_edit(doc_id: int):
    """在线保存文档编辑：用新正文替换旧正文并产生版本快照。"""
    user = auth.current_user()
    doc = _visible_doc(doc_id, user)  # 校验可见性（不能编辑看不到的文档）
    if not doc:
        return jsonify({"ok": False, "error": "文档不存在或您无权编辑"}), 404
    # 统一用 can_edit_doc 判定编辑权（覆盖角色规则 + 文档级 ACL 的 view_edit 授权）
    if not auth.can_edit_doc(user, doc):
        return jsonify({"ok": False, "error": "您没有该文档的编辑权限"}), 403
    # 并发编辑锁校验：若锁被他人有效持有，拒绝保存以防互相覆盖
    if config.EDIT_LOCK_ENABLED and config.EDIT_LOCK_CHECK_ON_SAVE:
        blocker = db.edit_lock_blocking(doc_id, user["id"], config.EDIT_LOCK_TTL_SECONDS)
        if blocker:
            return jsonify({
                "ok": False,
                "error": f"「{blocker}」正在编辑本文档，保存被拒绝（请稍后重试或联系对方）",
            }), 409
    data = request.get_json(silent=True) or {}
    text = (data.get("text") or "").strip()  # 新正文（纯文本，富文本模式可能为空）
    html = data.get("html")  # 富文本 HTML（Quill 产出，可选）
    # 富文本模式：以 html 为准；纯文本模式：以 text 为准。两者皆空则拒绝
    if not html and not text:
        return jsonify({"ok": False, "error": "正文不能为空"}), 400
    fmt = data.get("fmt", "md")  # 文本格式，默认 Markdown（富文本模式会被后端改写为 txt）
    if fmt not in ("md", "html", "txt"):  # 非法格式兜底
        fmt = "md"
    title = (data.get("title") or "").strip() or doc["title"]  # 空标题沿用原值
    summary = data.get("summary")  # 摘要可空（空则自动生成）
    change_note = (data.get("change_note") or "").strip()  # 变更说明

    try:
        result = ingest.update_document(
            doc_id, text, fmt=fmt, title=title, summary=summary,
            change_note=change_note, user_id=user["id"], html=html,
        )
    except Exception as exc:  # 兜底异常，避免 500 裸露
        return jsonify({"ok": False, "error": f"保存失败：{exc}"}), 500

    # 保存成功后主动释放编辑锁（锁的使命已完成，避免长期占用）
    if config.EDIT_LOCK_ENABLED:
        db.release_edit_lock(doc_id, user["id"], is_admin=False)

    audit.log("doc_edit", user_id=user["id"], username=user["username"], ip=auth.client_ip(),
              target_type="document", target_id=doc_id,
              detail={"标题": title, "版本": result.get("version"), "说明": change_note},
              result="success" if result["ok"] else "fail")
    return jsonify(result), (200 if result["ok"] else 422)


# ============================================================
# 七-二、富文本内嵌图片上传 + /uploads 静态服务
# ============================================================

@bp.route("/uploads/<path:filename>")
def serve_upload(filename: str):
    """
    静态服务上传目录里的文件（图片/附件）。

    编辑器内嵌图片存到 DATA_DIR/uploads，通过本路由以 /uploads/xxx 暴露，
    Quill 插入 <img src="/uploads/xxx"> 即可显示。不要求登录（图片属文档内容，
    文档可见性由页面层控制；若需严格管控可在此加可见性校验）。
    """
    # 用安全目录发送，杜绝 ../ 目录穿越
    return send_from_directory(str(config.UPLOAD_DIR), secure_filename(filename))


@bp.route("/api/documents/<int:doc_id>/images", methods=["POST"])
@auth.login_required
def api_doc_image_upload(doc_id: int):
    """
    接收富文本编辑器粘贴/拖拽/选择上传的图片，保存到 uploads 目录，返回可访问 URL。

    支持的提交方式：
        - 表单 multipart：字段名 image（文件）
        - JSON：{"image": "data:image/png;base64,...."}（极少数场景用 base64，不推荐，体积大）
    返回：{"ok": True, "url": "/uploads/xxx.png"}
    """
    user = auth.current_user()
    doc = _visible_doc(doc_id, user)  # 校验可见性（看不到的文档不能往里传图）
    if not doc:
        return jsonify({"ok": False, "error": "文档不存在或您无权操作"}), 404
    if not auth.can_edit_doc(user, doc):  # 统一编辑权判定（含文档级 ACL）
        return jsonify({"ok": False, "error": "您没有该文档的编辑权限"}), 403

    raw = None       # 原始字节
    ext = ".png"     # 扩展名（默认 png）
    # 方式一：multipart 表单上传（推荐，体积可控）
    if "image" in request.files:
        f = request.files["image"]
        raw = f.read()
        # 从原始文件名推断扩展名，并做白名单校验
        src_ext = Path(f.filename or "").suffix.lower()
        if src_ext in config.ALLOWED_IMAGE_EXTS:
            ext = src_ext
    # 方式二：base64 JSON（兜底）
    elif request.is_json:
        data = request.get_json(silent=True) or {}
        b64 = data.get("image", "")
        if b64.startswith("data:"):  # 形如 data:image/png;base64,xxxx
            # 从 MIME 推导扩展名
            mime = b64.split(";")[0].split(":")[1]
            ext = {"image/png": ".png", "image/jpeg": ".jpg", "image/gif": ".gif",
                   "image/webp": ".webp", "image/svg+xml": ".svg"}.get(mime, ".png")
            import base64  # 标准库 base64 解码
            try:
                raw = base64.b64decode(b64.split(",", 1)[1])  # 取逗号后的编码体
            except Exception:
                raw = None
    if not raw:  # 没收到图片数据
        return jsonify({"ok": False, "error": "未收到图片数据"}), 400
    # 体积校验：超过上限拒绝，避免撑爆磁盘
    if len(raw) > config.IMAGE_MAX_MB * 1024 * 1024:
        return jsonify({"ok": False, "error": f"图片超过 {config.IMAGE_MAX_MB}MB 上限"}), 413

    # 用随机文件名落盘（避免重名覆盖、避免文件名注入）
    from uuid import uuid4  # UUID 生成安全文件名
    fname = uuid4().hex + ext
    save_path = config.UPLOAD_DIR / fname
    save_path.write_bytes(raw)  # 写入上传目录

    url = f"/uploads/{fname}"  # 前端可直接用于 <img src>
    audit.log("doc_image", user_id=user["id"], username=user["username"], ip=auth.client_ip(),
              target_type="document", target_id=doc_id,
              detail={"文件名": fname, "大小": len(raw)}, result="success")
    return jsonify({"ok": True, "url": url})


@bp.route("/api/documents/<int:doc_id>/versions")
@auth.login_required
def api_doc_versions(doc_id: int):
    """列出文档的版本历史：历史快照 + 当前实时版本。"""
    user = auth.current_user()
    doc = _visible_doc(doc_id, user)  # 校验可见性
    if not doc:
        return jsonify({"ok": False, "error": "文档不存在或您无权查看"}), 404
    # 历史快照：按版本号倒序（最新在前）
    rows = db.query(
        """
        SELECT v.id, v.version, v.title, v.change_note, v.changed_by, v.created_at,
               u.display_name AS changed_by_name
        FROM doc_versions v
        LEFT JOIN users u ON u.id = v.changed_by
        WHERE v.doc_id = ?
        ORDER BY v.version DESC
        """,
        (doc_id,),
    )
    versions = db.rows_to_dicts(rows)
    # 当前实时版本（标记 is_current=True），让前端能对比"现在 vs 历史"
    current = {
        "version": doc["version"],
        "title": doc["title"],
        "updated_at": doc["updated_at"],
        "content_length": doc["content_length"],
        "is_current": True,
    }
    return jsonify({"ok": True, "current": current, "versions": versions})


@bp.route("/api/documents/<int:doc_id>/versions/<int:vid>")
@auth.login_required
def api_doc_version_detail(doc_id: int, vid: int):
    """取单条历史版本的完整正文，供查看与 diff 对比。"""
    user = auth.current_user()
    doc = _visible_doc(doc_id, user)  # 校验可见性
    if not doc:
        return jsonify({"ok": False, "error": "文档不存在或您无权查看"}), 404
    ver = db.query_one("SELECT * FROM doc_versions WHERE id = ? AND doc_id = ?", (vid, doc_id))
    if not ver:
        return jsonify({"ok": False, "error": "版本不存在"}), 404
    return jsonify({
        "ok": True,
        "version": db.row_to_dict(ver),  # 含 content 全文、title、change_note、created_at 等
    })


@bp.route("/api/documents/<int:doc_id>/rollback", methods=["POST"])
@auth.login_required
def api_doc_rollback(doc_id: int):
    """将文档回滚到某条历史版本。"""
    user = auth.current_user()
    doc = _visible_doc(doc_id, user)  # 校验可见性
    if not doc:
        return jsonify({"ok": False, "error": "文档不存在或您无权操作"}), 404
    if not auth.can_edit_doc(user, doc):  # 统一编辑权判定（含文档级 ACL）
        return jsonify({"ok": False, "error": "您没有该文档的编辑权限"}), 403
    data = request.get_json(silent=True) or {}
    vid = int(data.get("version_id") or 0)  # 目标版本记录 ID
    if not vid:
        return jsonify({"ok": False, "error": "请指定 version_id"}), 400
    try:
        result = ingest.rollback_document(doc_id, vid, user_id=user["id"])
    except Exception as exc:  # 兜底异常
        return jsonify({"ok": False, "error": f"回滚失败：{exc}"}), 500
    if result["ok"]:
        audit.log("doc_rollback", user_id=user["id"], username=user["username"], ip=auth.client_ip(),
                  target_type="document", target_id=doc_id,
                  detail={"回滚到版本": result.get("rollback_to"), "新版本": result.get("version")},
                  result="success")
        return jsonify(result)
    return jsonify(result), 422


# ============================================================
# 九、标签体系（P1 标签体系）
# ============================================================

def _get_or_create_tag(name: str) -> Optional[int]:
    """
    按名称获取或创建标签，返回标签 ID。

    名称会去空白并截断到 50 字，防止超长；空名返回 None。
    这是标签「自由输入即可用」体验的底层支撑：用户打一个新标签时自动建库。
    """
    name = (name or "").strip()[:50]  # 去空白并截断
    if not name:  # 空名直接返回 None
        return None
    row = db.query_one("SELECT id FROM tags WHERE name = ?", (name,))  # 先查是否已存在
    if row:  # 已存在则复用，避免重复标签
        return row["id"]
    return db.execute("INSERT INTO tags (name) VALUES (?)", (name,))  # 否则新建


@bp.route("/api/tags")
@auth.login_required
def api_tags():
    """列出全部标签及其使用次数，供前端下拉建议与标签云使用。"""
    rows = db.query(
        """
        SELECT t.id, t.name, COUNT(dt.doc_id) AS use_count
        FROM tags t
        LEFT JOIN doc_tags dt ON dt.tag_id = t.id
        GROUP BY t.id, t.name
        ORDER BY use_count DESC, t.name
        """
    )
    return jsonify({"ok": True, "tags": db.rows_to_dicts(rows)})


@bp.route("/api/tags", methods=["POST"])
@auth.login_required
def api_create_tag():
    """手动创建一个标签（前端也能在打标签时自动创建，这里提供显式入口）。"""
    user = auth.current_user()  # 当前用户（审计用）
    data = request.get_json(silent=True) or {}
    name = (data.get("name") or "").strip()[:50]  # 标签名清洗
    if not name:  # 空名拒绝
        return jsonify({"ok": False, "error": "标签名必填"}), 400
    existing = db.query_one("SELECT id FROM tags WHERE name = ?", (name,))  # 查重
    if existing:  # 已存在则幂等返回，不报错
        return jsonify({"ok": True, "tag_id": existing["id"], "exists": True})
    tid = db.execute("INSERT INTO tags (name) VALUES (?)", (name,))  # 新建
    audit.log("tag_manage", user_id=user["id"], username=user["username"], ip=auth.client_ip(),
              detail={"动作": "新建标签", "名称": name}, result="success")
    return jsonify({"ok": True, "tag_id": tid})


@bp.route("/api/documents/<int:doc_id>/tags")
@auth.login_required
def api_doc_tags(doc_id: int):
    """获取某文档当前挂载的标签列表。"""
    user = auth.current_user()
    doc = _visible_doc(doc_id, user)  # 复用权限校验
    if not doc:
        return jsonify({"ok": False, "error": "文档不存在或您无权查看"}), 404
    rows = db.query(
        """
        SELECT t.id, t.name FROM tags t
        JOIN doc_tags dt ON dt.tag_id = t.id
        WHERE dt.doc_id = ? ORDER BY t.name
        """,
        (doc_id,),
    )
    return jsonify({"ok": True, "tags": db.rows_to_dicts(rows)})


@bp.route("/api/documents/<int:doc_id>/tags", methods=["PUT"])
@auth.login_required
def api_set_doc_tags(doc_id: int):
    """
    设置某文档的标签（整体替换）。

    请求体：{"tags": ["标签A", "标签B", ...]}
    语义：传来的标签集合即为文档的最终标签；已不在集合里的旧标签会被解除。
    标签名不存在时自动创建（自由输入体验）。
    """
    user = auth.current_user()
    doc = _visible_doc(doc_id, user)  # 复用权限校验
    if not doc:
        return jsonify({"ok": False, "error": "文档不存在或您无权编辑"}), 404
    if not auth.can_edit_doc(user, doc):  # 统一编辑权判定（含文档级 ACL）
        return jsonify({"ok": False, "error": "您没有该文档的编辑权限"}), 403
    data = request.get_json(silent=True) or {}
    names = data.get("tags") or []  # 期望的标签名列表
    # 清洗：去空白、去空、去重、截断，保证入库数据干净
    clean: list[str] = []  # 清洗后的标签名列表
    seen: set[str] = set()  # 去重集合
    for n in names:  # 遍历原始列表
        n = (n or "").strip()[:50]  # 去空白截断
        if n and n not in seen:  # 非空且未出现过
            seen.add(n)
            clean.append(n)

    # 先解除该文档全部旧标签关联（整体替换语义）
    db.execute("DELETE FROM doc_tags WHERE doc_id = ?", (doc_id,))
    # 逐个获取/创建标签并写入关联；INSERT OR IGNORE 防止并发重复主键冲突
    for n in clean:  # 遍历清洗后的标签
        tid = _get_or_create_tag(n)  # 获取或创建标签 ID
        db.execute("INSERT OR IGNORE INTO doc_tags (doc_id, tag_id) VALUES (?, ?)", (doc_id, tid))

    audit.log("tag_manage", user_id=user["id"], username=user["username"], ip=auth.client_ip(),
              target_type="document", target_id=doc_id,
              detail={"动作": "设置标签", "标签": clean}, result="success")
    return jsonify({"ok": True, "tags": clean})


# ============================================================
# 十、页面级评论（P1 页面级评论）
# ============================================================

@bp.route("/api/documents/<int:doc_id>/comments")
@auth.login_required
def api_doc_comments(doc_id: int):
    """获取某文档的评论列表（按时间正序，含评论人姓名）。"""
    user = auth.current_user()
    doc = _visible_doc(doc_id, user)  # 复用权限校验
    if not doc:
        return jsonify({"ok": False, "error": "文档不存在或您无权查看"}), 404
    rows = db.query(
        """
        SELECT c.id, c.content, c.created_at, c.user_id, u.display_name AS author
        FROM doc_comments c
        JOIN users u ON u.id = c.user_id
        WHERE c.doc_id = ?
        ORDER BY c.created_at ASC
        """,
        (doc_id,),
    )
    return jsonify({"ok": True, "comments": db.rows_to_dicts(rows)})


@bp.route("/api/documents/<int:doc_id>/comments", methods=["POST"])
@auth.login_required
def api_add_comment(doc_id: int):
    """发表一条评论（任何可见文档的登录用户均可）。"""
    user = auth.current_user()
    doc = _visible_doc(doc_id, user)  # 复用权限校验
    if not doc:
        return jsonify({"ok": False, "error": "文档不存在或您无权评论"}), 404
    data = request.get_json(silent=True) or {}
    content = (data.get("content") or "").strip()
    if not content:
        return jsonify({"ok": False, "error": "评论内容不能为空"}), 400
    cid = db.execute(
        "INSERT INTO doc_comments (doc_id, user_id, content, created_at) VALUES (?,?,?,?)",
        (doc_id, user["id"], content[:1000], audit.now_iso()),  # 内容截断到 1000 字防滥用
    )
    audit.log("doc_comment", user_id=user["id"], username=user["username"], ip=auth.client_ip(),
              target_type="document", target_id=doc_id,
              detail={"内容": content[:50]}, result="success")
    return jsonify({"ok": True, "comment_id": cid})


@bp.route("/api/documents/<int:doc_id>/comments/<int:cid>", methods=["DELETE"])
@auth.login_required
def api_delete_comment(doc_id: int, cid: int):
    """删除评论：仅评论作者本人或管理员可删。"""
    user = auth.current_user()
    doc = _visible_doc(doc_id, user)
    if not doc:
        return jsonify({"ok": False, "error": "文档不存在或您无权操作"}), 404
    cm = db.query_one("SELECT * FROM doc_comments WHERE id = ? AND doc_id = ?", (cid, doc_id))
    if not cm:
        return jsonify({"ok": False, "error": "评论不存在"}), 404
    # 权限：作者本人或管理员
    if cm["user_id"] != user["id"] and user["role"] != "admin":
        return jsonify({"ok": False, "error": "仅可删除自己的评论"}), 403
    db.execute("DELETE FROM doc_comments WHERE id = ?", (cid,))
    audit.log("doc_comment", user_id=user["id"], username=user["username"], ip=auth.client_ip(),
              target_type="document", target_id=doc_id,
              detail={"动作": "删除评论", "评论ID": cid}, result="success")
    return jsonify({"ok": True})


# ============================================================
# 十一、文档导出（P1 导出 Markdown / PDF）
# ============================================================

@bp.route("/api/documents/<int:doc_id>/export/md")
@auth.login_required
def api_export_md(doc_id: int):
    """导出文档为 Markdown 文件（带元数据头，正文取自 body_text 权威源）。"""
    user = auth.current_user()
    doc = _visible_doc(doc_id, user)  # 复用权限校验
    if not doc:
        return jsonify({"ok": False, "error": "文档不存在或您无权查看"}), 404
    body = doc["body_text"] or ""  # 正文权威源
    # 组装带元信息的 Markdown 头
    meta_lines = [
        f"# {doc['title']}",
        "",
        f"> 分类：{config.CATEGORIES_L1.get(doc['category_l1'], '')} · "
        f"密级：{config.SECURITY_LEVELS.get(doc['security_level'], '')} · "
        f"责任人：{doc['owner'] or '—'} · 版本：v{doc['version']}",
    ]
    if doc["summary"]:
        meta_lines.append(f"> 摘要：{doc['summary']}")
    md_text = "\n".join(meta_lines) + "\n\n" + body  # 元信息 + 空行 + 正文
    # 文件名做安全处理（去掉路径分隔等），避免下载时出问题
    safe_title = "".join(c if (c.isalnum() or c in "._-一-鿿") else "_" for c in doc["title"])[:60]
    return Response(
        md_text,
        mimetype="text/markdown; charset=utf-8",
        headers={"Content-Disposition": f"attachment; filename=doc_{doc['id']}_{safe_title}.md"},
    )


# ============================================================
# 十一-B、文档导出（Docx / PDF 服务端生成，P 完整版补齐）
# ============================================================

def _doc_export_meta(doc: sqlite3.Row) -> list:
    """
    组装导出文件头部的元信息行（Markdown / Docx / PDF 共用）。
    取分类、密级、责任人、版本、质量等级，便于下载后溯源。
    """
    return [
        f"分类：{config.CATEGORIES_L1.get(doc['category_l1'], '')} · "
        f"密级：{config.SECURITY_LEVELS.get(doc['security_level'], '')} · "
        f"责任人：{doc['owner'] or '—'} · 版本：v{doc['version']}",
        f"质量：{config.QUALITY_LEVELS.get(doc['quality_level'], '')}",
    ]


def _doc_export_html(doc: sqlite3.Row) -> str:
    """
    取出用于导出的正文 HTML。

    优先用富文本 body_html；没有时把纯文本 body_text 按段落兜底包装成 HTML，
    保证 Docx/PDF 导出也能处理老库/上传文档（此时层级信息较弱，可接受）。
    """
    html = doc["body_html"] or ""  # 富文本优先
    if not html.strip():  # 无富文本则兜底
        html = "<p>" + (doc["body_text"] or "").replace("\n\n", "</p><p>").replace("\n", "<br/>") + "</p>"
    return html  # 返回可用 HTML


@bp.route("/api/documents/<int:doc_id>/export/docx")
@auth.login_required
def api_export_docx(doc_id: int):
    """导出文档为 Word(.docx)：服务端生成，含标题层级/列表/表格/图片。"""
    user = auth.current_user()
    doc = _visible_doc(doc_id, user)  # 复用权限校验
    if not doc:
        return jsonify({"ok": False, "error": "文档不存在或您无权查看"}), 404
    try:  # 调用导出模块生成字节
        data = export_docx.html_to_docx(doc["title"], _doc_export_meta(doc), _doc_export_html(doc))
    except Exception as exc:  # 兜底异常
        return jsonify({"ok": False, "error": f"生成 Docx 失败：{exc}"}), 500
    # 文件名做安全处理（去掉路径分隔等），避免下载异常
    safe_title = "".join(c if (c.isalnum() or c in "._-一-鿿") else "_" for c in doc["title"])[:60]
    return Response(
        data,  # Docx 字节流
        mimetype="application/vnd.openxmlformats-officedocument.wordprocessingml.document",  # Word MIME
        headers={"Content-Disposition": f"attachment; filename=doc_{doc['id']}_{safe_title}.docx"},
    )


@bp.route("/api/documents/<int:doc_id>/export/pdf")
@auth.login_required
def api_export_pdf(doc_id: int):
    """导出文档为 PDF：服务端生成（reportlab + 中文字体），无需浏览器打印。"""
    user = auth.current_user()
    doc = _visible_doc(doc_id, user)  # 复用权限校验
    if not doc:
        return jsonify({"ok": False, "error": "文档不存在或您无权查看"}), 404
    try:  # 调用导出模块生成字节
        data = export_pdf.html_to_pdf(doc["title"], _doc_export_meta(doc), _doc_export_html(doc))
    except Exception as exc:  # 兜底异常
        return jsonify({"ok": False, "error": f"生成 PDF 失败：{exc}"}), 500
    safe_title = "".join(c if (c.isalnum() or c in "._-一-鿿") else "_" for c in doc["title"])[:60]
    return Response(
        data,  # PDF 字节流
        mimetype="application/pdf",  # PDF MIME
        headers={"Content-Disposition": f"attachment; filename=doc_{doc['id']}_{safe_title}.pdf"},
    )


# ============================================================
# 十一-C、并发编辑锁（防止多人同时编辑互相覆盖，P 完整版补齐）
# ============================================================

@bp.route("/api/documents/<int:doc_id>/lock", methods=["POST"])
@auth.login_required
def api_doc_lock_acquire(doc_id: int):
    """获取/续期文档编辑锁。成功返回 ok；被他人有效占用返回 409 + 对方信息。"""
    user = auth.current_user()
    doc = _visible_doc(doc_id, user)  # 校验可见性
    if not doc:
        return jsonify({"ok": False, "error": "文档不存在或您无权查看"}), 404
    if not auth.can_edit_doc(user, doc):  # 必须有编辑权才能拿锁
        return jsonify({"ok": False, "error": "您没有编辑权限，无法获取编辑锁"}), 403
    if not config.EDIT_LOCK_ENABLED:  # 总开关关闭：直接放行（无锁语义）
        return jsonify({"ok": True, "enabled": False, "message": "编辑锁功能未启用"})
    res = db.acquire_edit_lock(doc_id, user["id"], user["display_name"], config.EDIT_LOCK_TTL_SECONDS)
    if res["ok"]:  # 抢到锁
        # 计算过期时间点返回给前端展示
        expires = (datetime.now() + timedelta(seconds=config.EDIT_LOCK_TTL_SECONDS)).strftime("%Y-%m-%d %H:%M:%S")
        return jsonify({
            "ok": True, "enabled": True, "expires_at": expires, "ttl": config.EDIT_LOCK_TTL_SECONDS,
        })
    # 被他人占用：返回 409 及持有者信息
    return jsonify({
        "ok": False, "enabled": True,
        "held_by": res["holder_user_id"], "held_by_name": res["holder_name"],
        "locked_at": res["locked_at"],
        "message": f"「{res['holder_name']}」正在编辑本文档，您暂为只读预览",
    }), 409


@bp.route("/api/documents/<int:doc_id>/lock", methods=["GET"])
@auth.login_required
def api_doc_lock_status(doc_id: int):
    """查询文档当前编辑锁状态（是否有人正在编辑、是否仍有效）。"""
    user = auth.current_user()
    doc = _visible_doc(doc_id, user)  # 校验可见性
    if not doc:
        return jsonify({"ok": False, "error": "文档不存在或您无权查看"}), 404
    if not config.EDIT_LOCK_ENABLED:  # 未启用
        return jsonify({"ok": True, "enabled": False, "active": False, "locked_by": None})
    row = db.get_edit_lock(doc_id)  # 取锁行
    holder = row["edit_lock_user"] if row else None  # 持有者 ID
    active = False  # 是否有效
    holder_name = None  # 持有者姓名
    if holder is not None:  # 有人持有
        try:
            locked_at = datetime.strptime(row["edit_lock_at"], "%Y-%m-%d %H:%M:%S")  # 解析锁时间
            # 未超过 TTL 才算有效
            active = (datetime.now() - locked_at) <= timedelta(seconds=config.EDIT_LOCK_TTL_SECONDS)
        except (ValueError, TypeError):
            active = False  # 时间异常按失效
        holder_name = row["edit_lock_name"]  # 持有者显示名
    return jsonify({
        "ok": True, "enabled": True,
        "locked_by": holder if active else None,  # 失效则视作无锁
        "locked_by_name": holder_name if active else None,
        "active": active, "ttl": config.EDIT_LOCK_TTL_SECONDS,
    })


@bp.route("/api/documents/<int:doc_id>/lock", methods=["DELETE"])
@auth.login_required
def api_doc_lock_release(doc_id: int):
    """释放文档编辑锁（保存成功后或离开编辑页时调用）。"""
    user = auth.current_user()
    doc = _visible_doc(doc_id, user)  # 校验可见性
    if not doc:
        return jsonify({"ok": False, "error": "文档不存在或您无权查看"}), 404
    # 仅持有者本人或管理员可释放（防误删他人锁）
    db.release_edit_lock(doc_id, user["id"], is_admin=(user["role"] == "admin"))
    return jsonify({"ok": True})


# ============================================================
# 十一-D、文档级细粒度可见权限（ACL）管理，P 完整版补齐
# ============================================================

def _can_manage_acl(user: Optional[dict], doc: sqlite3.Row) -> bool:
    """是否可管理该文档的 ACL：管理员 或 文档创建人本人。"""
    if not user:  # 未登录
        return False
    if user["role"] == "admin":  # 管理员
        return True
    return doc["created_by"] == user["id"]  # 创建人本人（sqlite3.Row 与 dict 均支持 [] 取值）


@bp.route("/api/documents/<int:doc_id>/acl", methods=["GET"])
@auth.login_required
def api_doc_acl_get(doc_id: int):
    """查询文档级权限设置：模式 + ACL 明细 + 可选用户/角色列表（供前端渲染面板）。"""
    user = auth.current_user()
    doc = _visible_doc(doc_id, user)  # 校验可见性
    if not doc:
        return jsonify({"ok": False, "error": "文档不存在或您无权查看"}), 404
    info = db.get_doc_acl(doc_id)  # 取模式与明细
    info["can_manage"] = _can_manage_acl(user, doc)  # 是否可编辑权限
    # 列出可选用户（仅启用）与全部角色，供前端下拉选择主体
    info["users"] = db.rows_to_dicts(db.query(
        "SELECT id, display_name, username, role FROM users WHERE active = 1 ORDER BY display_name"
    ))
    info["roles"] = [{"key": k, "name": v} for k, v in config.ROLES.items()]
    return jsonify({"ok": True, **info})


@bp.route("/api/documents/<int:doc_id>/acl", methods=["PUT"])
@auth.login_required
def api_doc_acl_set(doc_id: int):
    """整体设置文档级权限：可见模式 + ACL 明细。仅管理员或创建人可调用。"""
    user = auth.current_user()
    doc = _visible_doc(doc_id, user)  # 校验可见性
    if not doc:
        return jsonify({"ok": False, "error": "文档不存在或您无权查看"}), 404
    if not _can_manage_acl(user, doc):  # 权限不足
        return jsonify({"ok": False, "error": "仅管理员或文档创建人可设置权限"}), 403
    if not config.ACL_ENABLED:  # 总开关关闭
        return jsonify({"ok": False, "error": "文档级权限功能未启用"}), 403
    data = request.get_json(silent=True) or {}  # 解析请求体
    mode = data.get("visibility_mode", "inherit")  # 可见模式
    entries = data.get("entries", []) or []  # ACL 明细
    # 校验主体是否存在：user 必须是真实用户 ID；role 必须是合法角色
    valid_users = {str(r["id"]) for r in db.query("SELECT id FROM users")}  # 真实用户 ID 集合
    valid_roles = set(config.ROLES.keys())  # 合法角色集合
    cleaned = []  # 清洗后的 ACL 项
    for e in entries:  # 遍历前端传来的每项
        ptype = e.get("principal_type")  # 主体类型
        pid = str(e.get("principal_id", "")).strip()  # 主体标识（统一转字符串）
        perm = e.get("perm", "view")  # 权限档位
        if ptype == "user" and pid not in valid_users:  # 用户不存在跳过
            continue
        if ptype == "role" and pid not in valid_roles:  # 角色非法跳过
            continue
        if perm not in ("view", "view_edit"):  # 非法档位降级为 view
            perm = "view"
        cleaned.append({"principal_type": ptype, "principal_id": pid, "perm": perm})
    try:  # 整体替换写库
        db.replace_doc_acl(doc_id, mode, cleaned, user["id"])
    except Exception as exc:  # 兜底
        return jsonify({"ok": False, "error": f"保存权限失败：{exc}"}), 500
    audit.log("doc_acl", user_id=user["id"], username=user["username"], ip=auth.client_ip(),
              target_type="document", target_id=doc_id,
              detail={"模式": mode, "条目数": len(cleaned)}, result="success")
    return jsonify({"ok": True, **db.get_doc_acl(doc_id)})


# ============================================================
# 十二、MFA 多因子认证（P2 安全增强）
# ============================================================

@bp.route("/api/mfa/status")
@auth.login_required
def api_mfa_status():
    """查询当前用户 MFA 开启状态与备用码数量（不返回密钥本身）。"""
    user = auth.current_user()
    backup = []  # 备用码列表（仅在未开启时展示给用户抄写）
    if user["mfa_enabled"] and user["mfa_backup"]:
        try:
            backup = json.loads(user["mfa_backup"] or "[]")  # 已开启则解析备用码
        except Exception:
            backup = []
    return jsonify({
        "ok": True,
        "enabled": bool(user["mfa_enabled"]),  # 是否已开启
        "backup_count": len(backup),            # 剩余备用码数量
        "mfa_enabled_global": config.MFA_ENABLED,  # 全局开关是否开启
    })


@bp.route("/api/mfa/setup", methods=["POST"])
@auth.login_required
def api_mfa_setup():
    """
    MFA 开通第一步：生成密钥 + 二维码，但暂不启用。
    前端展示二维码让用户扫码，用户输入正确动态码后再调 /api/mfa/confirm 才真正启用。
    密钥先临时存进会话，confirm 时再落库，避免「扫了码却没验证成功」导致半开状态。
    """
    user = auth.current_user()
    if not config.MFA_ENABLED:  # 全局关闭则不可用
        return jsonify({"ok": False, "error": "MFA 未启用"}), 403
    secret = mfa_mod.generate_secret()  # 生成新密钥
    session["mfa_setup_secret"] = secret  # 暂存到会话，confirm 时用
    qr = mfa_mod.qr_svg(secret, user["username"])  # 生成二维码 SVG
    uri = mfa_mod.provisioning_uri(secret, user["username"])  # otpauth URI（手动添加用）
    return jsonify({"ok": True, "secret": secret, "qr_svg": qr, "otpauth_uri": uri})


@bp.route("/api/mfa/confirm", methods=["POST"])
@auth.login_required
def api_mfa_confirm():
    """MFA 开通第二步：校验用户输入的动态码，正确则正式启用并生成备用码。"""
    user = auth.current_user()
    secret = session.get("mfa_setup_secret")  # 取出上一步暂存的密钥
    if not secret:  # 没走 setup 直接 confirm
        return jsonify({"ok": False, "error": "请先发起 MFA 配置"}), 400
    data = request.get_json(silent=True) or {}
    code = (data.get("code") or "").strip()  # 用户输入的动态码
    if not mfa_mod.verify_code(secret, code):  # 校验失败
        return jsonify({"ok": False, "error": "动态码错误，请重试"}), 400
    backup_codes = mfa_mod.generate_backup_codes()  # 生成备用码
    mfa_mod.enable_mfa(user["id"], secret, backup_codes)  # 落库启用
    session.pop("mfa_setup_secret", None)  # 清理暂存
    return jsonify({"ok": True, "backup_codes": backup_codes})  # 把备用码交给用户抄存


@bp.route("/api/mfa/disable", methods=["POST"])
@auth.login_required
def api_mfa_disable():
    """关闭当前用户的 MFA（需已登录，且为本人操作；管理员关自己同理）。"""
    user = auth.current_user()
    # 二次确认密码，防止会话被冒用时被人悄悄关掉 MFA
    data = request.get_json(silent=True) or {}
    pwd = data.get("password") or ""
    if not auth.verify_password(pwd, user["password_hash"], user["password_salt"]):
        return jsonify({"ok": False, "error": "密码错误，无法关闭 MFA"}), 403
    mfa_mod.disable_mfa(user["id"])  # 关闭并清密钥
    return jsonify({"ok": True})


# ============================================================
# 十三、SSO / LDAP / OIDC 单点登录（P2 安全增强）
# ============================================================

@bp.route("/api/login/ldap", methods=["POST"])
def api_login_ldap():
    """
    LDAP 登录：接收 username/password，走企业目录校验后关联本地账号。
    SSO 关闭时直接拒绝，避免被误用。
    """
    if not sso_mod.sso_enabled():  # 总开关关闭
        return jsonify({"ok": False, "error": "单点登录未启用"}), 403
    if config.SSO_PROVIDER != "ldap":  # 协议不匹配
        return jsonify({"ok": False, "error": "当前未配置 LDAP 单点登录"}), 400
    data = request.get_json(silent=True) or {}
    username = (data.get("username") or "").strip()
    password = data.get("password") or ""
    user, err = sso_mod.authenticate_ldap(username, password)  # 走 LDAP 校验
    if not user:  # 失败
        audit.log("sso_login", username=username, ip=auth.client_ip(),
                  detail={"结果": "失败", "原因": err}, result="fail")
        return jsonify({"ok": False, "error": err}), 401
    auth.login_session(user)  # 关联成功则登录
    audit.log("sso_login", user_id=user["id"], username=user["username"], ip=auth.client_ip(),
               detail={"来源": "ldap"}, result="success")
    return jsonify({
        "ok": True,
        "user": {"username": user["username"], "display_name": user["display_name"], "role": user["role"]},
    })


@bp.route("/api/sso/oidc/authorize")
def api_sso_oidc_authorize():
    """返回 OIDC 授权页地址，前端把浏览器重定向过去。SSO 关闭时返回明确提示。"""
    if not sso_mod.sso_enabled():  # 总开关关闭
        return jsonify({"ok": False, "error": "单点登录未启用"}), 403
    if config.SSO_PROVIDER != "oidc":  # 协议不匹配
        return jsonify({"ok": False, "error": "当前未配置 OIDC 单点登录"}), 400
    url = sso_mod.oidc_authorize_url()  # 拼授权页 URL
    if not url:  # 配置不全或发现失败
        return jsonify({"ok": False, "error": "OIDC 配置不完整，无法发起授权"}), 400
    return jsonify({"ok": True, "authorize_url": url})


@bp.route("/api/sso/oidc/callback")
def api_sso_oidc_callback():
    """
    OIDC 回调：IdP 带着授权码跳回这里，我们用 code 换用户信息并登录。
    成功则写会话、重定向到检索页；失败则回到登录页并带错误信息。
    """
    code = request.args.get("code") or ""  # 授权码
    if not code:  # 没拿到 code（用户取消授权等）
        return redirect("/login?error=" + "OIDC 授权被取消")
    user, err = sso_mod.authenticate_oidc_code(code)  # 换 token + 拉 userinfo + 关联本地账号
    if not user:  # 失败
        from urllib.parse import quote  # 把错误信息做 URL 编码，避免破坏跳转地址
        return redirect("/login?error=" + quote(err or "OIDC 登录失败"))  # 带错误回登录页
    auth.login_session(user)  # 登录
    audit.log("sso_login", user_id=user["id"], username=user["username"], ip=auth.client_ip(),
               detail={"来源": "oidc"}, result="success")
    return redirect("/search")  # 登录成功去检索页


# ============================================================
# 十四、diagrams.net 内联画图（P2 内联画图）
# ============================================================

@bp.route("/api/documents/<int:doc_id>/diagrams", methods=["GET", "POST"])
@auth.login_required
def api_doc_diagrams(doc_id: int):
    """
    文档内嵌图列表(GET) / 新建(POST)。
    GET：返回该文档下全部图的元信息（含 XML，供编辑器回显）。
    POST：保存一张新图（diagram_xml 为 diagrams.net 导出的 mxGraphModel XML）。
    """
    user = auth.current_user()
    doc = _visible_doc(doc_id, user)  # 复用权限校验
    if not doc:
        return jsonify({"ok": False, "error": "文档不存在或您无权操作"}), 404

    if request.method == "GET":  # 列表
        rows = db.query(
            "SELECT id, doc_id, title, created_at, updated_at FROM doc_diagrams "
            "WHERE doc_id = ? ORDER BY id", (doc_id,),
        )
        return jsonify({"ok": True, "diagrams": db.rows_to_dicts(rows)})

    # POST：新建图。写权限同在线编辑（贡献者及以上）
    if user["role"] not in ("admin", "reviewer", "contributor"):
        return jsonify({"ok": False, "error": "无权限保存图示"}), 403
    data = request.get_json(silent=True) or {}
    xml = (data.get("diagram_xml") or "").strip()  # 图的 XML
    if not xml:
        return jsonify({"ok": False, "error": "图内容不能为空"}), 400
    title = (data.get("title") or "未命名图").strip()[:100]  # 图标题
    now = audit.now_iso()  # 当前时间
    did = db.execute(
        "INSERT INTO doc_diagrams (doc_id, title, diagram_xml, created_by, created_at, updated_at) "
        "VALUES (?,?,?,?,?,?)",
        (doc_id, title, xml, user["id"], now, now),
    )
    audit.log("diagram_save", user_id=user["id"], username=user["username"], ip=auth.client_ip(),
               target_type="document", target_id=doc_id,
               detail={"动作": "新建图", "图ID": did, "标题": title}, result="success")
    return jsonify({"ok": True, "diagram_id": did})


@bp.route("/api/documents/<int:doc_id>/diagrams/<int:did>", methods=["PUT", "DELETE"])
@auth.login_required
def api_doc_diagram_detail(doc_id: int, did: int):
    """单张图的更新(PUT) / 删除(DELETE)。更新写权限同编辑；删除仅作者或管理员。"""
    user = auth.current_user()
    doc = _visible_doc(doc_id, user)
    if not doc:
        return jsonify({"ok": False, "error": "文档不存在或您无权操作"}), 404
    dia = db.query_one("SELECT * FROM doc_diagrams WHERE id = ? AND doc_id = ?", (did, doc_id))
    if not dia:  # 图不存在
        return jsonify({"ok": False, "error": "图不存在"}), 404

    if request.method == "PUT":  # 更新图内容
        if user["role"] not in ("admin", "reviewer", "contributor"):
            return jsonify({"ok": False, "error": "无权限更新图示"}), 403
        data = request.get_json(silent=True) or {}
        xml = (data.get("diagram_xml") or "").strip()
        if not xml:
            return jsonify({"ok": False, "error": "图内容不能为空"}), 400
        title = (data.get("title") or dia["title"]).strip()[:100]
        db.execute(
            "UPDATE doc_diagrams SET diagram_xml=?, title=?, updated_at=? WHERE id=?",
            (xml, title, audit.now_iso(), did),
        )
        audit.log("diagram_save", user_id=user["id"], username=user["username"], ip=auth.client_ip(),
                   target_type="document", target_id=doc_id,
                   detail={"动作": "更新图", "图ID": did}, result="success")
        return jsonify({"ok": True})

    # DELETE：仅作者本人或管理员可删
    if dia["created_by"] != user["id"] and user["role"] != "admin":
        return jsonify({"ok": False, "error": "仅可删除自己创建的图"}), 403
    db.execute("DELETE FROM doc_diagrams WHERE id = ?", (did,))
    audit.log("diagram_save", user_id=user["id"], username=user["username"], ip=auth.client_ip(),
               target_type="document", target_id=doc_id,
               detail={"动作": "删除图", "图ID": did}, result="success")
    return jsonify({"ok": True})
