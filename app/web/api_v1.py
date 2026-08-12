# -*- coding: utf-8 -*-
"""
标准 REST API v1 蓝图（P2 标准 REST API + 文档）
=============================================
目标：提供一套「资源式、版本化、可文档化」的对外接口，与老的「动作式 /api/」并存互补。

设计约定（与老接口区分）：
- 统一前缀 /api/v1（由 config.API_V1_PREFIX 控制）
- 统一响应信封：{"ok": true/false, "data": ..., "error": "...", "page": {...}}
- 列表接口统一支持 page / per_page 分页，并返回 page 元数据
- 鉴权：@auth.api_key_required —— 既支持浏览器会话，也支持 Bearer API Key（程序调用）
- 权限：所有文档类查询复用 auth.visibility_filter，在数据层做隔离

端点清单（详见 openapi.json 与 /api/v1/docs 文档页）：
  GET /api/v1/health                 健康检查
  GET /api/v1/documents              文档列表（分页+过滤）
  GET /api/v1/documents/<id>         文档详情（含切片/标签/引用）
  GET /api/v1/search?q=              知识检索
  GET /api/v1/spaces                 知识树（可见节点）
  GET /api/v1/tags                   标签列表（含计数）
  GET /api/v1/comments?doc_id=       评论列表
  GET /api/v1/users                  用户列表（管理员）
  GET /api/v1/audit                  审计日志（管理员）
  GET /api/v1/openapi.json           OpenAPI 3.0 规范
"""

from typing import Any, Optional

from flask import (
    Blueprint,   # 蓝图，承载一组路由
    jsonify,     # 返回 JSON
    request,     # 请求对象
)

from .. import auth, db, config  # 内部模块
from ..search import engine as search_engine  # 检索引擎
from .. import audit as audit_mod  # 审计模块（用户/审计接口用）

# api_v1_bp 是本蓝图的独立对象，url_prefix 取自配置，保证前缀统一
api_v1_bp = Blueprint("api_v1", __name__, url_prefix=config.API_V1_PREFIX)


def _page_params() -> tuple[int, int]:
    """
    解析分页参数：page（从 1 开始）与 per_page（受上下限约束）。

    返回：
        (page, per_page)
    """
    try:
        page = max(1, int(request.args.get("page", 1)))  # 页码下限 1
    except ValueError:
        page = 1
    try:
        # per_page 限制在 1 ~ REST_MAX_PAGE_SIZE 之间
        per_page = min(config.REST_MAX_PAGE_SIZE, max(1, int(request.args.get("per_page", config.REST_PAGE_SIZE))))
    except ValueError:
        per_page = config.REST_PAGE_SIZE
    return page, per_page


def _ok(data: Any = None, page: Optional[dict] = None) -> "tuple":
    """
    统一成功信封。

    参数：
        data: 业务数据
        page: 分页元数据（列表接口填）
    返回：
        (jsonify 响应, 200)
    """
    body = {"ok": True, "data": data}  # 基础信封
    if page is not None:  # 有分页信息时带上
        body["page"] = page
    return jsonify(body), 200


def _err(message: str, code: int = 400) -> "tuple":
    """统一失败信封。"""
    return jsonify({"ok": False, "error": message}), code


@api_v1_bp.route("/health")
def v1_health():
    """
    健康检查：返回服务状态与文档总数，供监控与探活。
    刻意不做鉴权（开放端点），方便负载均衡 / 容器编排 / 外部监控直接探活。
    不泄露任何业务数据，仅返回聚合计数。
    """
    total = db.query_one("SELECT COUNT(*) AS c FROM documents")["c"]  # 文档总数
    return _ok({"status": "ok", "documents": total, "version": "v1"})


@api_v1_bp.route("/documents", methods=["GET"])
@auth.api_key_required
def v1_documents():
    """文档列表：支持分类/密级/空间/标签/关键词/状态过滤 + 分页，全程权限隔离。"""
    user = auth.current_user()
    page, per_page = _page_params()  # 取分页
    # 组装过滤条件（复用统一权限过滤）
    where_sql, params = auth.visibility_filter(user, "d")
    conditions = [where_sql]  # 已含权限条件
    if request.args.get("category_l1"):
        conditions.append("d.category_l1 = ?")
        params.append(request.args.get("category_l1"))
    if request.args.get("security_level"):
        conditions.append("d.security_level = ?")
        params.append(request.args.get("security_level"))
    if request.args.get("status"):
        conditions.append("d.status = ?")
        params.append(request.args.get("status"))
    if request.args.get("keyword"):
        conditions.append("(d.title LIKE ? OR d.summary LIKE ?)")
        params.extend([f"%{request.args.get('keyword')}%", f"%{request.args.get('keyword')}%"])
    if request.args.get("tag"):
        # 按标签过滤：关联 doc_tags + tags
        conditions.append(
            "d.id IN (SELECT dt.doc_id FROM doc_tags dt JOIN tags t ON t.id = dt.tag_id WHERE t.name = ?)"
        )
        params.append(request.args.get("tag"))
    if request.args.get("space_id"):
        # 按知识树节点过滤（含子孙）
        all_spaces = db.query("SELECT id, parent_id FROM knowledge_spaces")
        from .apis import _collect_space_descendants  # 复用主蓝图的递归展开函数
        desc = _collect_space_descendants(all_spaces, int(request.args.get("space_id")))
        if desc:
            conditions.append(f"d.space_id IN ({','.join('?' for _ in desc)})")
            params.extend(desc)
        else:
            conditions.append("1=0")
    where = " AND ".join(conditions)  # 拼完整 WHERE

    total = db.query_one(f"SELECT COUNT(*) AS c FROM documents d WHERE {where}", params)["c"]  # 总数
    rows = db.query(
        """
        SELECT d.id, d.title, d.category_l1, d.category_l2, d.security_level, d.status,
               d.quality_level, d.owner, d.version, d.content_length, d.chunk_count, d.updated_at
        FROM documents d WHERE {where} ORDER BY d.id DESC LIMIT ? OFFSET ?
        """.replace("{where}", where),
        params + [per_page, (page - 1) * per_page],
    )
    return _ok(
        db.rows_to_dicts(rows),
        {"page": page, "per_page": per_page, "total": total, "pages": (total + per_page - 1) // per_page},
    )


@api_v1_bp.route("/documents/<int:doc_id>", methods=["GET"])
@auth.api_key_required
def v1_document_detail(doc_id: int):
    """文档详情：含基础信息、标签、出/入链、章节切片。"""
    user = auth.current_user()
    where_sql, params = auth.visibility_filter(user, "d")
    doc = db.query_one(
        f"SELECT * FROM documents d WHERE d.id = ? AND {where_sql}", [doc_id] + params
    )
    if not doc:  # 无权限或不存在
        return _err("文档不存在或您无权查看", 404)
    doc = db.row_to_dict(doc)  # 转字典
    # 读取标签
    tags = db.rows_to_dicts(db.query(
        "SELECT t.id, t.name FROM tags t JOIN doc_tags dt ON dt.tag_id=t.id WHERE dt.doc_id=? ORDER BY t.name",
        (doc_id,),
    ))
    # 读取出链（本文引用了谁）
    out_links = db.rows_to_dicts(db.query(
        "SELECT l.link_text, d.id, d.title FROM doc_links l JOIN documents d ON d.id=l.to_doc_id "
        "WHERE l.from_doc_id=? AND d.status!='archived' ORDER BY d.title", (doc_id,),
    ))
    # 读取入链（谁引用了本文）
    in_links = db.rows_to_dicts(db.query(
        "SELECT l.link_text, d.id, d.title FROM doc_links l JOIN documents d ON d.id=l.from_doc_id "
        "WHERE l.to_doc_id=? AND d.status!='archived' ORDER BY d.title", (doc_id,),
    ))
    # 读取章节切片（含锚点）
    chunks = db.rows_to_dicts(db.query(
        "SELECT seq, heading_path, anchor, content, char_count FROM chunks WHERE doc_id=? ORDER BY seq",
        (doc_id,),
    ))
    # 组装成完整详情返回
    return _ok({
        "document": {k: doc[k] for k in doc if k != "body_text"},  # 正文较大，列表/详情不默认返回全文
        "tags": tags,
        "out_links": out_links,
        "in_links": in_links,
        "chunks": chunks,
    })


@api_v1_bp.route("/search", methods=["GET"])
@auth.api_key_required
def v1_search():
    """知识检索：混合/关键词/语义三种模式，返回标准化结果。"""
    user = auth.current_user()
    q = (request.args.get("q") or "").strip()
    if not q:  # 空查询
        return _err("缺少查询参数 q", 400)
    mode = request.args.get("mode", "hybrid")  # 默认混合
    try:
        top_k = min(config.REST_MAX_PAGE_SIZE, max(1, int(request.args.get("top_k", config.SEARCH_TOP_K))))
    except ValueError:
        top_k = config.SEARCH_TOP_K
    results, stats = search_engine.search(query_text=q, user=user, top_k=top_k, mode=mode, source="api_v1")
    return _ok([r.to_dict() for r in results], {"mode": mode, "stats": stats})


@api_v1_bp.route("/spaces", methods=["GET"])
@auth.api_key_required
def v1_spaces():
    """知识树：返回当前用户可见节点的扁平列表（含文档数）。"""
    user = auth.current_user()
    rows = db.query("SELECT * FROM knowledge_spaces ORDER BY sort_order, name")
    visible = [db.row_to_dict(r) for r in rows if _space_visible(user, r)]  # 按权限过滤
    # 统计各节点文档数
    counts = {
        r["space_id"]: r["c"]
        for r in db.query("SELECT space_id, COUNT(*) AS c FROM documents WHERE space_id IS NOT NULL GROUP BY space_id")
    }
    for n in visible:
        n["doc_count"] = counts.get(n["id"], 0)
    return _ok(visible)


def _space_visible(user, sp) -> bool:
    """判断用户能否看到某知识树节点（与 apis 中逻辑一致，权限隔离核心）。"""
    if not user:
        return False
    user_rank = config.SECURITY_RANK.get(user["max_security"], 2)
    lvl_rank = config.SECURITY_RANK.get(sp["security_level"], 2)
    if lvl_rank > user_rank:
        return False
    if user["role"] in ("admin", "reviewer") or user.get("cross_dept"):
        return True
    if sp["department_id"] is None:
        return True
    return sp["department_id"] == user.get("department_id")


@api_v1_bp.route("/tags", methods=["GET"])
@auth.api_key_required
def v1_tags():
    """标签列表：返回全部标签及其被使用次数。"""
    rows = db.query(
        """
        SELECT t.id, t.name, COUNT(dt.doc_id) AS use_count
        FROM tags t LEFT JOIN doc_tags dt ON dt.tag_id = t.id
        GROUP BY t.id, t.name ORDER BY use_count DESC, t.name
        """
    )
    return _ok(db.rows_to_dicts(rows))


@api_v1_bp.route("/comments", methods=["GET"])
@auth.api_key_required
def v1_comments():
    """评论列表：按 doc_id 过滤（必填），仅可见文档的评论。"""
    user = auth.current_user()
    doc_id = request.args.get("doc_id")
    if not doc_id:  # 缺文档 ID
        return _err("缺少 doc_id 参数", 400)
    # 校验可见性
    where_sql, params = auth.visibility_filter(user, "d")
    doc = db.query_one(f"SELECT id FROM documents d WHERE d.id = ? AND {where_sql}", [int(doc_id)] + params)
    if not doc:
        return _err("文档不存在或您无权查看", 404)
    rows = db.query(
        """
        SELECT c.id, c.doc_id, c.content, c.created_at, c.user_id, u.display_name AS author
        FROM doc_comments c JOIN users u ON u.id = c.user_id
        WHERE c.doc_id = ? ORDER BY c.created_at ASC
        """,
        (int(doc_id),),
    )
    return _ok(db.rows_to_dicts(rows))


@api_v1_bp.route("/users", methods=["GET"])
@auth.login_required
@auth.admin_required
def v1_users():
    """用户列表（仅管理员）：脱敏返回账号与角色信息。"""
    rows = db.query(
        "SELECT id, username, display_name, role, department_id, max_security, "
        "cross_dept, active, must_change_pwd, last_login_at FROM users ORDER BY id"
    )
    return _ok(db.rows_to_dicts(rows))


@api_v1_bp.route("/audit", methods=["GET"])
@auth.login_required
@auth.admin_required
def v1_audit():
    """审计日志（仅管理员）：支持动作/关键词/日期过滤 + 分页。"""
    rows, total = audit_mod.search_logs(
        action=request.args.get("action"),
        keyword=request.args.get("keyword"),
        date_from=request.args.get("date_from"),
        date_to=request.args.get("date_to"),
        limit=int(request.args.get("limit", 100)),
        offset=int(request.args.get("offset", 0)),
    )
    for r in rows:
        r["action_name"] = audit_mod.ACTIONS.get(r["action"], r["action"])  # 动作转中文
    return _ok(rows, {"total": total})


@api_v1_bp.route("/openapi.json", methods=["GET"])
@auth.api_key_required
def v1_openapi():
    """返回 OpenAPI 3.0 规范文档（供 Swagger UI / 代码生成器消费）。"""
    return jsonify(build_openapi_spec())  # 直接返回规范字典


def build_openapi_spec() -> dict:
    """
    手工维护的 OpenAPI 3.0 规范字典。

    说明：本项目接口相对固定，手写规范比引入 flasgger 等重型依赖更轻、更可控；
    新加端点时请同步在此登记，保证 /api/v1/docs 文档页与规范一致。
    """
    base = config.API_V1_PREFIX  # 统一前缀
    return {
        "openapi": "3.0.3",
        "info": {
            "title": "AI-KM 标准 REST API",
            "version": "1.0.0",
            "description": "带 AI 问答的医药采购知识引擎 · 资源式 REST 接口（与 /api/ 动作式接口并存）",
        },
        "servers": [{"url": base, "description": "本服务"}],  # 服务器地址
        "security": [{"bearerAuth": []}, {"sessionCookie": []}],  # 两种鉴权方式
        "components": {
            "securitySchemes": {
                "bearerAuth": {"type": "http", "scheme": "bearer", "description": "Authorization: Bearer <API Key>"},
                "sessionCookie": {"type": "apiKey", "in": "cookie", "name": "session", "description": "已登录的浏览器会话"},
            }
        },
        "paths": {
            f"{base}/health": {
                "get": {"summary": "健康检查", "responses": {"200": {"description": "服务状态"}}}
            },
            f"{base}/documents": {
                "get": {
                    "summary": "文档列表",
                    "parameters": [
                        {"name": "page", "in": "query", "schema": {"type": "integer"}},
                        {"name": "per_page", "in": "query", "schema": {"type": "integer"}},
                        {"name": "category_l1", "in": "query", "schema": {"type": "string"}},
                        {"name": "security_level", "in": "query", "schema": {"type": "string"}},
                        {"name": "tag", "in": "query", "schema": {"type": "string"}},
                        {"name": "keyword", "in": "query", "schema": {"type": "string"}},
                        {"name": "space_id", "in": "query", "schema": {"type": "integer"}},
                    ],
                    "responses": {"200": {"description": "文档列表（带分页）"}},
                }
            },
            f"{base}/documents/{{doc_id}}": {
                "get": {"summary": "文档详情", "parameters": [{"name": "doc_id", "in": "path", "required": True, "schema": {"type": "integer"}}],
                        "responses": {"200": {"description": "文档详情（含标签/引用/切片）"}, "404": {"description": "无权限或不存在"}}}
            },
            f"{base}/search": {
                "get": {
                    "summary": "知识检索",
                    "parameters": [
                        {"name": "q", "in": "query", "required": True, "schema": {"type": "string"}},
                        {"name": "mode", "in": "query", "schema": {"type": "string", "enum": ["hybrid", "bm25", "vector"]}},
                        {"name": "top_k", "in": "query", "schema": {"type": "integer"}},
                    ],
                    "responses": {"200": {"description": "检索结果"}},
                }
            },
            f"{base}/spaces": {"get": {"summary": "知识树", "responses": {"200": {"description": "可见节点列表"}}}},
            f"{base}/tags": {"get": {"summary": "标签列表", "responses": {"200": {"description": "标签+计数"}}}},
            f"{base}/comments": {
                "get": {"summary": "评论列表", "parameters": [{"name": "doc_id", "in": "query", "required": True, "schema": {"type": "integer"}}],
                        "responses": {"200": {"description": "评论列表"}}}
            },
            f"{base}/users": {"get": {"summary": "用户列表（管理员）", "responses": {"200": {"description": "用户列表"}}}},
            f"{base}/audit": {"get": {"summary": "审计日志（管理员）", "responses": {"200": {"description": "审计日志"}}}},
        },
    }
