# -*- coding: utf-8 -*-
"""
页面路由
========
负责渲染 HTML 页面。所有页面都需要登录（除登录页本身）。
角色受限的页面用 auth.role_required 装饰器保护。
"""

from flask import (
    render_template,  # 渲染模板
    redirect,         # 重定向
    request,           # 请求对象
    session,           # 会话
    url_for,           # 反向路由
)

from . import bp  # 当前蓝图
from .. import auth, audit, db, config  # 内部模块


@bp.route("/")
def index():
    """首页：未登录跳登录页，已登录跳检索页。"""
    if not auth.current_user():  # 未登录
        return redirect(url_for("web.login"))  # 去登录页
    return redirect(url_for("web.search_page"))  # 已登录去检索页


@bp.route("/login", methods=["GET"])
def login():
    """登录页（GET 渲染表单）。"""
    if auth.current_user():  # 已登录则直接进系统
        return redirect(url_for("web.search_page"))
    # 把 SSO 开关与协议类型传给模板，决定是否展示单点登录入口
    return render_template(
        "login.html",
        sso_enabled=config.SSO_ENABLED,
        sso_provider=config.SSO_PROVIDER,
    )  # 渲染登录模板


@bp.route("/logout")
def logout():
    """登出：清会话 + 记审计 + 回登录页。"""
    user = auth.current_user()  # 当前用户
    if user:  # 已登录才记录
        audit.log("logout", user_id=user["id"], username=user["username"], ip=auth.client_ip())
    auth.logout_session()  # 清空会话
    session.clear()  # 双重保险，清除所有会话数据
    return redirect(url_for("web.login"))  # 回登录页


@bp.route("/search")
@auth.login_required  # 必须登录
def search_page():
    """知识检索页。"""
    return render_template("search.html")


@bp.route("/ask")
@auth.login_required
def ask_page():
    """AI 问答页。"""
    return render_template("ask.html")


@bp.route("/upload")
@auth.login_required
@auth.role_required("admin", "reviewer", "contributor")  # 仅可贡献知识的角色
def upload_page():
    """知识入库页。"""
    # 取出部门列表，供上传表单选择归属部门
    departments = db.rows_to_dicts(db.query("SELECT id, name FROM departments ORDER BY name"))
    return render_template("upload.html", departments=departments)


@bp.route("/review")
@auth.login_required
@auth.role_required("admin", "reviewer")  # 仅审核角色
def review_page():
    """知识审核页。"""
    return render_template("review.html")


@bp.route("/dashboard")
@auth.login_required
def dashboard_page():
    """效能看板页。"""
    return render_template("dashboard.html")


@bp.route("/doc/<int:doc_id>")
@auth.login_required
def doc_page(doc_id: int):
    """文档详情页。"""
    # 取出文档基本信息（需经权限过滤，否则越权可见）
    where_sql, params = auth.visibility_filter(auth.current_user(), "d")
    row = db.query_one(
        f"""
        SELECT d.*, COALESCE(dept.name, '') AS dept_name
        FROM documents d
        LEFT JOIN departments dept ON dept.id = d.department_id
        WHERE d.id = ? AND {where_sql}
        """,
        [doc_id] + params,
    )
    if not row:  # 文档不存在或无权限
        return render_template("error.html", message="文档不存在或您无权查看", code=404), 404
    doc = db.row_to_dict(row)  # 转字典
    # 向上追溯知识树路径，拼出 书架/书/章 面包屑（若文档挂载了空间节点）
    space_path = []  # 从根到当前节点的路径列表
    cur = doc.get("space_id")  # 当前文档挂载的空间节点 ID
    while cur:  # 一直向上找父节点
        sp = db.query_one("SELECT id, name, parent_id FROM knowledge_spaces WHERE id = ?", (cur,))
        if not sp:  # 节点不存在则停止
            break
        space_path.insert(0, {"id": sp["id"], "name": sp["name"]})  # 插到最前，保证顺序根→叶
        cur = sp["parent_id"]  # 继续向上追溯
    # 取出该文档的切片，展示知识结构（含 anchor 与正文，供段落锚点直达，P1）
    chunks = db.rows_to_dicts(db.query(
        "SELECT id, seq, heading_path, anchor, content, char_count FROM chunks WHERE doc_id = ? ORDER BY seq",
        (doc_id,),
    ))
    # 取出该文档挂载的标签（P1 标签体系）
    doc_tags = db.rows_to_dicts(db.query(
        """
        SELECT t.id, t.name FROM tags t
        JOIN doc_tags dt ON dt.tag_id = t.id
        WHERE dt.doc_id = ? ORDER BY t.name
        """,
        (doc_id,),
    ))
    # 取出「本文引用了哪些文档」（出链，P1 交叉引用）
    out_links = db.rows_to_dicts(db.query(
        """
        SELECT l.link_text, d.id, d.title
        FROM doc_links l
        JOIN documents d ON d.id = l.to_doc_id
        WHERE l.from_doc_id = ? AND d.status != 'archived'
        ORDER BY d.title
        """,
        (doc_id,),
    ))
    # 取出「哪些文档引用了本文」（入链，P1 交叉引用）
    in_links = db.rows_to_dicts(db.query(
        """
        SELECT l.link_text, d.id, d.title
        FROM doc_links l
        JOIN documents d ON d.id = l.from_doc_id
        WHERE l.to_doc_id = ? AND d.status != 'archived'
        ORDER BY d.title
        """,
        (doc_id,),
    ))
    # 当前用户是否可编辑（统一走 can_edit_doc，覆盖角色规则 + 文档级 ACL 的 view_edit 授权）
    cur_user = auth.current_user()  # 当前登录用户
    can_edit = auth.can_edit_doc(cur_user, doc)
    is_admin = cur_user["role"] == "admin"  # 是否管理员（评论区删除权限用）
    # 文档级 ACL 信息（供详情页「权限」面板渲染；仅管理员/创建人可修改）
    can_manage_acl = is_admin or (doc.get("created_by") == cur_user["id"])
    if config.ACL_ENABLED:  # 开关开启才取 ACL 明细
        acl_info = db.get_doc_acl(doc_id)
    else:  # 关闭时给默认值，前端据此隐藏编辑表单
        acl_info = {"visibility_mode": "inherit", "entries": []}
    # 仅当可管理时才拉取可选用户/角色列表，减少不必要的数据传输
    acl_users = db.rows_to_dicts(db.query(
        "SELECT id, display_name, username, role FROM users WHERE active = 1 ORDER BY display_name"
    )) if can_manage_acl else []
    acl_roles = [{"key": k, "name": v} for k, v in config.ROLES.items()] if can_manage_acl else []
    # 取出该文档的评论列表（按时间正序，P1 页面级评论）
    comments = db.rows_to_dicts(db.query(
        """
        SELECT c.id, c.content, c.created_at, c.user_id, u.display_name AS author
        FROM doc_comments c
        JOIN users u ON u.id = c.user_id
        WHERE c.doc_id = ?
        ORDER BY c.created_at ASC
        """,
        (doc_id,),
    ))
    # 取出该文档已保存的内嵌图（diagrams.net，P2 内联画图），用于详情页只读展示
    diagrams = db.rows_to_dicts(db.query(
        "SELECT id, doc_id, title, created_at, updated_at FROM doc_diagrams WHERE doc_id = ? ORDER BY id",
        (doc_id,),
    ))
    return render_template(
        "doc.html",
        doc=doc, chunks=chunks, space_path=space_path,
        doc_tags=doc_tags, out_links=out_links, in_links=in_links,
        comments=comments, diagrams=diagrams, can_edit=can_edit,
        current_user_id=cur_user["id"], is_admin=is_admin,
        acl_info=acl_info, can_manage_acl=can_manage_acl,
        acl_users=acl_users, acl_roles=acl_roles,
    )


@bp.route("/doc/<int:doc_id>/print")
@auth.login_required
def doc_print_page(doc_id: int):
    """文档打印/导出 PDF 专用页：纯净排版，配合浏览器「打印 → 另存为 PDF」使用。"""
    user = auth.current_user()
    where_sql, params = auth.visibility_filter(user, "d")  # 复用权限过滤
    doc = db.query_one(
        f"SELECT d.*, COALESCE(dept.name, '') AS dept_name FROM documents d "
        f"LEFT JOIN departments dept ON dept.id = d.department_id "
        f"WHERE d.id = ? AND {where_sql}",
        [doc_id] + params,
    )
    if not doc:  # 无权限或不存在
        return render_template("error.html", message="文档不存在或您无权查看", code=404), 404
    doc = db.row_to_dict(doc)  # 转字典
    # 取出章节正文（带锚点），用于打印页完整呈现
    chunks = db.rows_to_dicts(db.query(
        "SELECT seq, heading_path, anchor, content FROM chunks WHERE doc_id = ? ORDER BY seq",
        (doc_id,),
    ))
    return render_template("doc_print.html", doc=doc, chunks=chunks)


@bp.route("/doc/<int:doc_id>/draw")
@auth.login_required
def doc_draw_page(doc_id: int):
    """
    文档内嵌画图页（P2 diagrams.net 内联画图）。
    用 diagrams.net 的 embed 模式（iframe + postMessage 协议）在线绘图，
    保存时把图的 XML 通过 /api/documents/<id>/diagrams 存回本系统。
    可编辑模式要求 contribution 及以上角色；仅查看模式（?view=1）任意可见用户可读。
    """
    user = auth.current_user()
    where_sql, params = auth.visibility_filter(user, "d")  # 复用权限过滤
    doc = db.query_one(
        f"SELECT d.*, COALESCE(dept.name, '') AS dept_name FROM documents d "
        f"LEFT JOIN departments dept ON dept.id = d.department_id "
        f"WHERE d.id = ? AND {where_sql}",
        [doc_id] + params,
    )
    if not doc:  # 无权限或不存在
        return render_template("error.html", message="文档不存在或您无权查看", code=404), 404
    doc = db.row_to_dict(doc)  # 转字典
    # 取出该文档已保存的图（含 XML，供编辑器回显 / 查看器渲染）
    diagrams = db.rows_to_dicts(db.query(
        "SELECT id, doc_id, title, diagram_xml, created_at, updated_at FROM doc_diagrams "
        "WHERE doc_id = ? ORDER BY id", (doc_id,),
    ))
    is_view = request.args.get("view") == "1"  # 是否为只读查看模式
    can_edit = (not is_view) and user["role"] in ("admin", "reviewer", "contributor")  # 仅贡献者以上可编辑
    return render_template(
        "draw.html",
        doc=doc, diagrams=diagrams, can_edit=can_edit, is_view=is_view,
    )


@bp.route("/doc/<int:doc_id>/edit")
@auth.login_required
def doc_edit_page(doc_id: int):
    """文档在线编辑器页：编辑正文 + 查看/回滚版本历史（P0-2 / P0-3）。"""
    # 取出文档基本信息（需经权限过滤，越权直接 404）
    where_sql, params = auth.visibility_filter(auth.current_user(), "d")
    row = db.query_one(
        f"""
        SELECT d.id, d.title, d.visibility_mode, d.created_by
        FROM documents d
        WHERE d.id = ? AND {where_sql}
        """,
        [doc_id] + params,
    )
    if not row:  # 文档不存在或无权限
        return render_template("error.html", message="文档不存在或您无权编辑", code=404), 404
    # 当前用户是否可编辑（统一走 can_edit_doc，覆盖角色规则 + 文档级 ACL 的 view_edit 授权）
    can_edit = auth.can_edit_doc(auth.current_user(), db.row_to_dict(row))
    return render_template("editor.html", doc_id=doc_id, doc_title=row["title"], can_edit=can_edit)


@bp.route("/admin")
@auth.login_required
@auth.admin_required  # 仅管理员
def admin_page():
    """管理后台页（用户/密钥/审计/评测/索引/配置）。"""
    # 把 audit 模块传入模板，供页面渲染审计动作下拉框（admin.html 用 audit.ACTIONS）
    return render_template("admin.html", audit=audit)


@bp.route("/spaces")
@auth.login_required
def spaces_page():
    """知识树管理页：查看/新建/编辑/删除节点（创建权限在前端按角色控制）。"""
    # 取出部门列表，供新建节点时选择归属部门
    departments = db.rows_to_dicts(db.query("SELECT id, name FROM departments ORDER BY name"))
    return render_template("spaces.html", departments=departments)


@bp.route("/audit")
@auth.login_required
@auth.admin_required
def audit_page():
    """审计日志专用页（P1 审计日志专用页）：独立页面，复用 /api/admin/audit 查询接口。"""
    # 把 audit 模块传入模板，供动作下拉框使用 audit.ACTIONS
    return render_template("audit.html", audit=audit)


@bp.route("/mfa/setup")
@auth.login_required
def mfa_setup_page():
    """
    MFA 开通引导页（P2 多因子认证）。
    页面先调 /api/mfa/setup 拿到二维码，用户用 authenticator App 扫码，
    再输入动态码调 /api/mfa/confirm 完成开通；备用码一次性展示并提示抄存。
    """
    # 把 MFA 全局开关传入模板，关闭时页面直接提示不可用
    return render_template("mfa_setup.html", mfa_global=config.MFA_ENABLED)


@bp.route("/change-password")
@auth.login_required
def change_password_page():
    """自助修改密码页（含首次登录强制改密场景）。"""
    user = auth.current_user()  # 当前用户（装饰器保证已登录）
    # 是否处于强制改密状态，前端据此调整文案与「跳过」按钮的可用性
    return render_template("change_password.html", must_change=bool(user.get("must_change_pwd")))


@bp.route("/api/v1/docs")
@auth.login_required
def api_docs_page():
    """
    REST API v1 文档页（P2 标准 REST API）。
    用 Swagger UI（CDN 加载）渲染 /api/v1/openapi.json，
    方便开发者在线浏览与试调接口。需联网加载 Swagger UI 资源。
    """
    # openapi 规范地址传给前端 Swagger UI 初始化用
    return render_template("api_docs.html", openapi_url=f"{config.API_V1_PREFIX}/openapi.json")
