# -*- coding: utf-8 -*-
"""
SSO / LDAP / OIDC 单点登录模块（P2 安全增强）
==========================================
把企业已有的身份系统接入 AI-KM，实现「一次登录、处处通行」。

设计要点（重要）：
- 本模块所有外部依赖（ldap3 / requests）一律「用到才导入」（懒加载），
  因此即使运行环境没装 ldap3，模块导入与应用启动也绝不报错，只是相关功能不可用。
- 所有行为由 config 里的 SSO_ENABLED / SSO_PROVIDER 等开关控制；关闭时路由直接返回明确提示。
- 账号关联策略：SSO 登录成功后，按 username（优先）或 email 关联本地账号；
  本地无账号且开启 SSO_AUTO_CREATE_USER 时自动建号（最小权限角色），实现平滑迁移。

当前支持两种协议：
- ldap：对接企业 AD / OpenLDAP，走「服务账号搜索 + 用户 DN 绑定校验密码」
- oidc：对接标准 OIDC 提供方（Keycloak / 企业微信网页授权 / 飞书 / Okta 等），走授权码流程
"""

import json  # 解析 OIDC 的 discovery 与 userinfo JSON
from typing import Optional

from . import config, db, auth, audit  # 项目内部模块


def sso_enabled() -> bool:
    """SSO 总开关是否开启。"""
    return config.SSO_ENABLED


def _match_local_user(username: str, email: str = "") -> Optional[dict]:
    """
    按 username 或 email 在本地用户表里找已存在的账号。

    优先用 username 精确匹配（最稳）；找不到再用 email 匹配（部分 IdP 只给邮箱）。
    """
    if username:  # 先看 username
        u = auth.get_user_by_username(username)
        if u:
            return u
    if email:  # 再用 email 兜底
        row = db.query_one("SELECT * FROM users WHERE username = ?", (email,))
        if row:
            return db.row_to_dict(row)
    return None


def link_or_create_user(profile: dict) -> tuple[Optional[dict], str]:
    """
    把 SSO 返回的身份档案关联（或创建）到本地账号。

    参数：
        profile: OIDC/LDAP 归一化后的档案，至少含 username；可选含 display_name、email
    返回：
        (本地用户字典, 错误信息) —— 成功时错误为空
    """
    username = (profile.get("username") or "").strip()  # 登录名
    email = (profile.get("email") or "").strip()        # 邮箱
    display_name = (profile.get("display_name") or username or "SSO用户").strip()  # 显示名

    # 1) 先尝试关联到已存在的本地账号
    existing = _match_local_user(username, email)
    if existing:  # 关联成功，直接返回（不覆盖密码，本地密码仍可用于兜底登录）
        return existing, ""

    # 2) 不存在且未开启自动建号 → 拒绝
    if not config.SSO_AUTO_CREATE_USER:
        return None, "本地无此账号且未开启自动建号，请联系管理员"

    # 3) 自动建号：给最小权限角色 + 默认部门
    dept = db.query_one("SELECT id FROM departments WHERE name = ?", (config.SSO_DEFAULT_DEPT,))
    dept_id = dept["id"] if dept else None
    # 初始密码用随机强密码，用户走 SSO 一般不用它，但保留本地兜底登录能力
    from .auth import generate_password  # 延迟导入，避免循环
    uid = auth.create_user(
        username=username,
        display_name=display_name,
        password=generate_password(),
        role=config.SSO_DEFAULT_ROLE,
        department_id=dept_id,
        must_change_pwd=False,  # SSO 账号不需要强制改本地密码
    )
    audit.log("sso_login", user_id=uid, ip=_stub_ip(),
               detail={"动作": "SSO 自动建号", "来源": config.SSO_PROVIDER}, result="success")
    return auth.get_user_by_id(uid), ""


# ============================================================
# 一、LDAP 认证（对接 AD / OpenLDAP）
# ============================================================

def ldap_authorize_url(state: str = "") -> str:
    """
    占位：LDAP 是「用户名+密码」直接提交式协议，没有浏览器重定向环节，
    这里仅保持与 OIDC 一致的接口形态，实际返回空串（前端直接显示账号密码表单）。
    """
    return ""


def authenticate_ldap(username: str, password: str) -> tuple[Optional[dict], str]:
    """
    走 LDAP 完成「搜索用户 + 绑定校验密码」，成功后关联本地账号。

    参数：
        username: 用户输入的登录名
        password: 用户输入的密码
    返回：
        (本地用户字典, 错误提示)
    """
    # 懒加载 ldap3：没装也能让模块正常导入（只是此函数不可用）
    try:
        import ldap3  # 标准 LDAP 客户端库
    except ImportError:
        return None, "服务端未安装 ldap3 依赖，无法使用 LDAP 登录"

    if not config.LDAP_SERVER:  # 没配服务器地址
        return None, "未配置 LDAP_SERVER，请联系管理员"
    if not username or not password:
        return None, "用户名与密码不能为空"

    try:
        # 1) 用服务账号连接目录，用于按过滤器搜索用户 DN
        server = ldap3.Server(config.LDAP_SERVER, use_ssl=config.LDAP_TLS)
        bind_conn = ldap3.Connection(
            server, user=config.LDAP_BIND_DN or None, password=config.LDAP_BIND_PASSWORD or None,
            auto_bind=True,  # 建立即绑定
        )
        # 2) 按过滤器模板替换用户名，搜索出该用户的条目
        search_filter = config.LDAP_USER_FILTER.format(username=username)
        bind_conn.search(
            config.LDAP_USER_BASE,
            search_filter,
            attributes=[config.LDAP_USER_RDNN_ATTR, config.LDAP_USER_NAME_ATTR, config.LDAP_USER_MAIL_ATTR],
        )
        if not bind_conn.entries:  # 目录里没这个人
            return None, "LDAP 目录中未找到该用户"
        entry = bind_conn.entries[0]  # 取第一个命中
        user_dn = entry.entry_dn       # 用户的完整 DN，用于下一步绑定校验密码
        # 3) 用「用户 DN + 输入密码」再绑定一次，真正校验密码是否正确
        user_conn = ldap3.Connection(server, user=user_dn, password=password, auto_bind=True)
        if not user_conn.bound:  # 绑定失败 = 密码错误
            return None, "LDAP 密码错误"
        # 4) 取出要落库的属性
        attrs = entry.entry_attributes_as_dict
        display_name = _first(attrs.get(config.LDAP_USER_NAME_ATTR)) or username
        email = _first(attrs.get(config.LDAP_USER_MAIL_ATTR)) or ""
        # 5) 关联/创建本地账号
        return link_or_create_user({
            "username": username, "display_name": display_name, "email": email,
        })
    except Exception as exc:  # 网络/目录异常统一兜底
        return None, f"LDAP 登录失败：{exc}"


# ============================================================
# 二、OIDC 认证（授权码流程）
# ============================================================

def oidc_discover() -> dict:
    """
    拉取 OIDC 提供方的发现文档（.well-known/openid-configuration）。
    返回包含 authorization_endpoint / token_endpoint / userinfo_endpoint 等字段的字典。
    """
    import requests  # 懒加载 HTTP 库
    url = config.OIDC_ISSUER.rstrip("/") + "/.well-known/openid-configuration"
    resp = requests.get(url, timeout=10)  # 请求发现文档
    resp.raise_for_status()  # 非 2xx 抛异常
    return resp.json()  # 返回 JSON


def oidc_authorize_url(state: str = "aikm") -> str:
    """
    拼出 OIDC 授权页地址，把浏览器重定向过去让用户登录并授权。

    参数：
        state: 防 CSRF 的随机串（生产应由会话持有，这里允许传入）
    返回：
        完整的授权页 URL；配置缺失时返回空串
    """
    if not config.OIDC_ISSUER or not config.OIDC_CLIENT_ID:
        return ""  # 配置不全，前端会提示
    try:
        disc = oidc_discover()  # 发现端点
        auth_ep = disc["authorization_endpoint"]
    except Exception:
        return ""  # 发现失败也返回空，由前端降级提示
    # 用标准查询参数拼 URL（response_type=code 授权码模式）
    from urllib.parse import urlencode  # 拼查询串
    params = {
        "client_id": config.OIDC_CLIENT_ID,
        "response_type": "code",
        "scope": config.OIDC_SCOPES,
        "redirect_uri": config.OIDC_REDIRECT_URI,
        "state": state,
    }
    return f"{auth_ep}?{urlencode(params)}"


def authenticate_oidc_code(code: str) -> tuple[Optional[dict], str]:
    """
    用授权码换取用户信息（token 交换 + 拉 userinfo），成功后关联本地账号。

    参数：
        code: OIDC 回调带回的授权码
    返回：
        (本地用户字典, 错误提示)
    """
    import requests  # 懒加载
    try:
        disc = oidc_discover()  # 发现端点
        token_ep = disc["token_endpoint"]
        userinfo_ep = disc["userinfo_endpoint"]
    except Exception as exc:
        return None, f"OIDC 发现失败：{exc}"

    # 1) 用授权码换 token（client_secret_post 或 Basic 都支持，这里用 Basic）
    from base64 import b64encode  # 客户端鉴权头编码
    basic = b64encode(f"{config.OIDC_CLIENT_ID}:{config.OIDC_CLIENT_SECRET}".encode()).decode()
    token_resp = requests.post(
        token_ep,
        headers={"Authorization": f"Basic {basic}"},
        data={
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": config.OIDC_REDIRECT_URI,
        },
        timeout=10,
    )
    if token_resp.status_code != 200:  # 换 token 失败
        return None, f"OIDC 换取令牌失败：{token_resp.status_code}"
    access_token = token_resp.json().get("access_token")  # 取出访问令牌
    if not access_token:
        return None, "OIDC 未返回访问令牌"

    # 2) 带着令牌拉用户信息
    ui_resp = requests.get(
        userinfo_ep,
        headers={"Authorization": f"Bearer {access_token}"},
        timeout=10,
    )
    if ui_resp.status_code != 200:
        return None, f"OIDC 拉取用户信息失败：{ui_resp.status_code}"
    info = ui_resp.json()  # 用户档案

    # 3) 归一化成统一档案：优先 preferred_username，否则用 email 前缀当 username
    username = info.get("preferred_username") or (info.get("email") or "").split("@")[0]
    profile = {
        "username": username,
        "display_name": info.get("name") or username,
        "email": info.get("email") or "",
    }
    return link_or_create_user(profile)  # 关联/创建本地账号


# ============================================================
# 辅助函数
# ============================================================

def _first(val) -> str:
    """取 LDAP 属性值列表里的第一个（LDAP 属性通常是列表），空则返回空串。"""
    if isinstance(val, list):  # 是列表取首项
        return val[0] if val else ""
    return val or ""  # 非列表直接返回


def _stub_ip() -> str:
    """SSO 模块内取客户端 IP 的兜底（非请求上下文返回 system）。"""
    try:
        from flask import request
        return request.headers.get("X-Forwarded-For", request.remote_addr or "system").split(",")[0].strip()
    except Exception:
        return "system"
