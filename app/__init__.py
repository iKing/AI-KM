# -*- coding: utf-8 -*-
"""
应用包初始化
============
本文件把零散的模块组装成一个可运行的 Flask 应用（SRS NFR-4 可拓展）。
核心思想：用"应用工厂函数" create_app() 统一装配，
避免在模块顶层直接创建 app 实例，方便测试和未来扩展。
"""

import os  # 操作系统接口，用于读取环境变量（判断密钥是否显式配置）
import sys  # 用于把项目根目录加入模块搜索路径
from pathlib import Path  # 跨平台路径处理
from datetime import timedelta  # 会话有效期配置
from flask import Flask  # Flask 应用类

# ROOT 是项目根目录（app 的上级目录）。把根目录加入 sys.path，
# 保证从任意工作目录启动都能正确 `import app`
ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:  # 避免重复加入
    sys.path.insert(0, str(ROOT))

# 集中导入内部模块（这些模块之间用相对导入，因此 app 必须是包，本文件就是包的标识）
from . import config, db, auth, audit  # noqa: E402  # 配置/数据库/认证/审计
from .web import bp as web_bp  # noqa: E402  # Web 蓝图（页面 + API 路由）
from .web.api_v1 import api_v1_bp  # noqa: E402  # 标准 REST API v1 蓝图（P2）


def _seed_initial_data() -> None:
    """
    首次启动时写入最小可用数据（SRS FR-1.5 初始化管理员）。
    设计为"幂等"：重复调用不会重复创建，可直接在每次启动时执行。
    """
    # --- 根部门：所有文档至少要归属一个部门 ---
    dept = db.query_one("SELECT id FROM departments WHERE name = ?", ("知识管理中心",))
    if dept:  # 已存在则复用其 ID
        dept_id = dept["id"]
    else:  # 不存在则创建
        dept_id = db.execute(
            "INSERT INTO departments (name, code, contact, created_at) VALUES (?,?,?,?)",
            ("知识管理中心", "KM", "系统管理员", audit.now_iso()),
        )

    # --- 初始化管理员账号 ---
    # 默认密码 Admin@123456，首次登录强制修改（FR-1.6）。
    # 生产环境上线后请立即修改密码并改用环境变量注入的密钥。
    if not auth.get_user_by_username("admin"):
        auth.create_user(
            username="admin",                       # 登录名
            display_name="系统管理员",               # 显示名
            password="Admin@123456",                # 初始密码
            role="admin",                            # 系统管理员角色
            department_id=dept_id,                   # 归属根部门
            max_security="confidential",             # 可见最高密级：机密
            cross_dept=True,                        # 可跨部门查看
            must_change_pwd=True,                   # 首次登录强制改密
        )
        audit.log(  # 记录初始化动作，留痕
            "user_manage",
            username="system",
            detail={"动作": "初始化管理员账号", "账号": "admin", "说明": "首次启动自动创建"},
            result="success",
        )


def create_app() -> "object":
    """
    应用工厂函数：组装并返回一个配置好的 Flask 应用实例。

    装配顺序（重要）：
        1. 初始化数据库（建表，幂等）
        2. 播种初始数据（部门 + 管理员）
        3. 创建 Flask app，设置密钥与会话策略
        4. 注册蓝图与全局钩子
    """
    # 第一步：建表（IF NOT EXISTS，可重复执行）
    db.init_db()
    # 第二步：首次启动播种数据
    _seed_initial_data()

    # 创建 Flask 应用。模板与静态资源放在 app/web 下
    app = Flask(
        __name__,
        template_folder="web/templates",  # 相对于 app 包目录
        static_folder="web/static",        # 静态资源目录
    )

    # 设置会话签名密钥：防止客户端伪造 Cookie（NFR-7 安全）
    app.secret_key = config.SECRET_KEY
    # 会话有效期：超时自动登出，降低被盗用风险
    app.permanent_session_lifetime = timedelta(hours=config.SESSION_HOURS)
    # 上传大小上限：超过直接 413，防止超大文件撑爆服务（NFR-7）
    app.config["MAX_CONTENT_LENGTH"] = config.MAX_FILE_SIZE_MB * 1024 * 1024

    # 会话 Cookie 安全标志（NFR-7）：
    # - HTTPONLY：默认就是 True，禁止 JS 读取 Cookie，防 XSS 窃取会话
    # - SECURE：仅在 HTTPS 场景（AIKM_HTTPS=true）下要求 Cookie 走加密连接
    # - SAMESITE=Lax：降低跨站请求伪造（CSRF）风险
    app.config["SESSION_COOKIE_HTTPONLY"] = True
    app.config["SESSION_COOKIE_SECURE"] = config.HTTPS
    app.config["SESSION_COOKIE_SAMESITE"] = "Lax"

    # 统一的 HTTP 安全响应头（每次响应自动追加）
    @app.after_request
    def _security_headers(resp):  # resp 是 Flask 的响应对象
        # 防 MIME 嗅探：浏览器必须按响应头声明的类型解析，禁止"猜类型"
        resp.headers.setdefault("X-Content-Type-Options", "nosniff")
        # 点击劫持防护：禁止被其他站点用 <frame> 嵌套（Diagram 页是"我嵌入外部"，不受影响）
        resp.headers.setdefault("X-Frame-Options", "SAMEORIGIN")
        # 隐私：跳转外站时不带完整 URL 作为 Referer
        resp.headers.setdefault("Referrer-Policy", "no-referrer")
        # 内容安全策略：限定脚本/样式/框架来源，降低 XSS 与恶意嵌入风险。
        # 注意：前端模板含内联脚本，故 script-src 含 'unsafe-inline'；画图需嵌入 diagrams.net。
        csp = (
            "default-src 'self'; "
            "img-src 'self' data:; "
            "style-src 'self' 'unsafe-inline'; "
            "script-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net; "
            "frame-src 'self' https://embed.diagrams.net https://viewer.diagrams.net;"
        )
        resp.headers.setdefault("Content-Security-Policy", csp)
        # 仅在 HTTPS 场景下启用 HSTS，强制浏览器只走加密连接（避免降级攻击）
        if config.HTTPS:
            resp.headers.setdefault(
                "Strict-Transport-Security", "max-age=31536000; includeSubDomains"
            )
        return resp  # 返回（可能被后续 after_request 继续处理）的响应

    # 把常用对象注入所有模板，前端直接用 {{ user }} {{ CATEGORIES_L1 }} 等
    @app.context_processor
    def inject_globals() -> dict:
        return {
            "user": auth.current_user(),                    # 当前登录用户（可能为 None）
            "CATEGORIES_L1": config.CATEGORIES_L1,          # 一级分类（前端下拉框）
            "SPACE_KINDS": config.SPACE_KINDS,              # 知识树节点类型（书架/书/章）
            "SECURITY_LEVELS": config.SECURITY_LEVELS,      # 密级定义
            "ROLES": config.ROLES,                          # 角色定义
            "QUALITY_LEVELS": config.QUALITY_LEVELS,        # 质量等级
            "DOC_STATUS": config.DOC_STATUS,                # 文档状态机
        }

    # 注册 Web 蓝图（包含页面路由与 /api 接口）
    app.register_blueprint(web_bp)
    # 注册标准 REST API v1 蓝图（/api/v1/*，P2）
    app.register_blueprint(api_v1_bp)

    # 请求结束时关闭当前线程的数据库连接，避免连接泄漏
    @app.teardown_appcontext
    def _close_db(exc=None):  # exc 参数必须保留，Flask 会传入异常对象
        db.close_conn()

    # ---- 生产安全自检：在日志里明确提示高危配置，便于运维及时发现 ----
    if not os.getenv("AIKM_SECRET_KEY"):
        # 未显式配置固定密钥：本次启动用了临时随机密钥，重启后所有登录态失效
        app.logger.warning(
            "【安全】AIKM_SECRET_KEY 未设置，已使用临时随机密钥（服务重启后所有用户登录态失效）。"
            "生产环境务必通过环境变量固定一个随机值。"
        )
    if config.DEBUG:
        # 调试模式会暴露堆栈与交互式调试器，生产必须关闭
        app.logger.warning(
            "【安全】DEBUG 模式已开启。生产环境请设置 AIKM_DEBUG=false 以避免泄露敏感信息。"
        )
    if not config.HTTPS:
        # 纯 HTTP 传输会话 Cookie 与密码存在被窃听风险，提醒上 HTTPS
        app.logger.warning(
            "【安全】未启用 HTTPS（AIKM_HTTPS=false）。建议在前方 Nginx 等反向代理上配置 TLS，"
            "以提升账号与数据传输安全性。"
        )

    return app
