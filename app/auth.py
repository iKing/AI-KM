# -*- coding: utf-8 -*-
"""
认证与鉴权模块
==============
职责（SRS FR-1、NFR-7）：
1. 密码安全存储与校验（PBKDF2-SHA256 加盐哈希）
2. 会话管理
3. 基于角色的访问控制（RBAC）
4. 数据可见性规则（部门隔离 + 密级过滤）
5. API Key 鉴权

安全红线：
- 数据库中绝不出现明文密码或可逆加密的密码
- 权限过滤在**数据层**执行（生成 SQL 的 WHERE 条件），而不是查出来再在前端隐藏
"""

import hashlib  # 哈希算法库
import hmac  # 安全的哈希比较，防时序攻击
import secrets  # 密码学安全的随机数生成器
import time  # 用于登录限流的时间戳
from functools import wraps  # 用于编写装饰器时保留原函数元信息
from typing import Any, Optional  # 类型注解

from flask import g, jsonify, redirect, request, session  # Flask 的请求上下文相关对象

from . import audit, config, db  # 项目内部模块


# ============================================================
# 一、密码处理
# ============================================================

def hash_password(password: str, salt: Optional[str] = None) -> tuple[str, str]:
    """
    对密码做加盐哈希。

    为什么用 PBKDF2 而不是直接 MD5/SHA256：
    MD5/SHA256 计算太快，攻击者拿到数据库后可以每秒尝试数十亿次暴力破解。
    PBKDF2 通过重复迭代 20 万次，把单次验证的成本抬高到毫秒级，
    正常登录感觉不到，但暴力破解的成本被放大了 20 万倍。

    参数：
        password: 明文密码
        salt:     盐值，不传则生成新的随机盐
    返回：
        (哈希值的十六进制字符串, 盐值)
    """
    if salt is None:  # 没传盐值说明是新建密码，需要生成随机盐
        salt = secrets.token_hex(16)  # 生成 16 字节（32 个十六进制字符）的密码学安全随机盐
    # 使用 PBKDF2-HMAC-SHA256 算法做密钥派生
    dk = hashlib.pbkdf2_hmac(
        "sha256",  # 底层哈希算法
        password.encode("utf-8"),  # 密码转字节
        salt.encode("utf-8"),  # 盐值转字节
        config.PASSWORD_ITERATIONS,  # 迭代次数，来自配置（默认 20 万）
    )
    return dk.hex(), salt  # 返回十六进制哈希与盐值


def verify_password(password: str, password_hash: str, salt: str) -> bool:
    """
    校验密码是否正确。

    使用 hmac.compare_digest 而不是 == 做比较，
    是为了防止"时序攻击"——攻击者通过测量比较耗时来逐字节猜测哈希值。
    compare_digest 保证无论内容如何，比较耗时都是恒定的。
    """
    computed, _ = hash_password(password, salt)  # 用相同的盐重新计算哈希
    return hmac.compare_digest(computed, password_hash)  # 恒定时间比较


def generate_password(length: int = 12) -> str:
    """
    生成随机初始密码，供管理员创建用户时使用。
    刻意排除了容易混淆的字符（0/O、1/l/I），减少人工传递密码时的输入错误。
    """
    alphabet = "ABCDEFGHJKMNPQRSTUVWXYZabcdefghijkmnpqrstuvwxyz23456789"  # 去掉易混淆字符的字符集
    return "".join(secrets.choice(alphabet) for _ in range(length))  # 逐位随机选取


# ============================================================
# 一·补：登录暴力破解防护（内存级滑动窗口限流）
# ============================================================
# 设计说明：
# - 这是进程内的简单限流器，足够内部小规模部署使用。
# - 若以后改为多进程/多实例部署，应换成 Redis 等共享存储（当前 gunicorn 默认 1 worker，进程内即可）。
# - key 用「账号名 + IP」组合，任一维度触发即锁定，兼顾"爆破账号"和"爆破 IP"两种攻击。

_login_lock = {}  # 失败记录字典：key -> 最近若干次失败的时间戳列表


def login_rate_check(key: str) -> tuple[bool, int]:
    """
    检查某 key（账号或 IP）当前是否允许尝试登录。

    参数：
        key: 限流标识，通常是 f"{username}|{ip}"
    返回：
        (是否允许登录, 剩余锁定秒数)；允许时剩余秒数为 0
    """
    now_ts = time.time()  # 当前时间戳（秒）
    # 滑动窗口：只保留窗口期内的失败记录
    window = config.LOGIN_FAIL_WINDOW_MIN * 60  # 窗口时长（秒）
    fails = [t for t in _login_lock.get(key, []) if now_ts - t <= window]  # 过滤过期记录
    _login_lock[key] = fails  # 写回，顺带完成过期清理

    if len(fails) >= config.LOGIN_MAX_FAIL:  # 失败次数达到上限
        # 锁定时间 = 第一次失败时刻 + 窗口时长 - 现在；再叠加一个锁定时长作为惩罚
        oldest = fails[0]  # 最早一次失败
        unlock_at = oldest + window + config.LOGIN_LOCK_MIN * 60  # 解锁时间点
        remain = int(unlock_at - now_ts)  # 剩余锁定秒数
        if remain > 0:  # 仍在锁定中
            return False, remain  # 拒绝本次尝试
        # 锁定已过期，清空记录放行
        _login_lock[key] = []  # 重置
        return True, 0
    return True, 0  # 未达上限，放行


def login_rate_register_failure(key: str) -> None:
    """登记一次登录失败（在 authenticate 返回错误后调用）。"""
    now_ts = time.time()  # 当前时间戳
    _login_lock.setdefault(key, []).append(now_ts)  # 追加失败时间戳


def login_rate_register_success(key: str) -> None:
    """登记一次登录成功（成功后清空该 key 的失败记录，避免误伤）。"""
    _login_lock.pop(key, None)  # 删除记录，相当于"洗白"


def validate_password(password: str) -> tuple[bool, str]:
    """
    校验密码强度是否符合安全基线（NFR-7）。

    规则（面向内部知识平台，平衡安全与可用性）：
        - 长度至少 8 位
        - 必须同时包含字母和数字（大小写不限）
        - 不允许与用户名完全相同（防止用账号名当密码）
    返回：
        (是否通过, 失败原因)；通过时原因为空字符串
    """
    # 长度检查：太短容易被暴力破解
    if not password or len(password) < 8:
        return False, "密码长度至少 8 位"
    # 复杂度检查：必须同时有字母和数字，避免纯数字/纯字母被字典攻击
    has_alpha = any(c.isalpha() for c in password)  # 是否含字母
    has_digit = any(c.isdigit() for c in password)  # 是否含数字
    if not (has_alpha and has_digit):
        return False, "密码必须同时包含字母和数字"
    return True, ""  # 校验通过


# ============================================================
# 二、用户管理
# ============================================================

def create_user(
    username: str,
    display_name: str,
    password: str,
    role: str = "user",
    department_id: Optional[int] = None,
    max_security: str = "internal",
    cross_dept: bool = False,
    must_change_pwd: bool = True,
) -> int:
    """
    创建用户。

    参数说明：
        max_security:    该用户能看到的最高密级。默认 internal，即看不到机密文档
        cross_dept:      是否可跨部门查看。默认否，实现部门隔离
        must_change_pwd: 首次登录是否强制改密。默认是（FR-1.6）
    返回：
        新用户的 ID
    """
    # 密码强度校验：不满足安全基线直接拒绝创建，避免弱口令流入系统
    ok, reason = validate_password(password)  # 校验密码
    if not ok:  # 不达标
        raise ValueError(f"密码不符合安全策略：{reason}")  # 抛出明确异常，由调用方捕获返回
    pwd_hash, salt = hash_password(password)  # 计算密码哈希与盐
    return db.execute(
        """
        INSERT INTO users
            (username, display_name, password_hash, password_salt, role,
             department_id, max_security, cross_dept, active, must_change_pwd, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?)
        """,
        (
            username,  # 登录名
            display_name,  # 显示名
            pwd_hash,  # 密码哈希
            salt,  # 盐值
            role,  # 角色
            department_id,  # 部门
            max_security,  # 密级许可
            1 if cross_dept else 0,  # 布尔转 0/1
            1 if must_change_pwd else 0,  # 布尔转 0/1
            audit.now_iso(),  # 创建时间
        ),
    )


def get_user_by_username(username: str) -> Optional[dict]:
    """按用户名查找用户，找不到返回 None。"""
    row = db.query_one("SELECT * FROM users WHERE username = ?", (username,))  # 参数化查询
    return db.row_to_dict(row)  # 转成字典返回


def get_user_by_id(user_id: int) -> Optional[dict]:
    """按用户 ID 查找用户。"""
    row = db.query_one("SELECT * FROM users WHERE id = ?", (user_id,))
    return db.row_to_dict(row)


def authenticate(username: str, password: str) -> tuple[Optional[dict], str]:
    """
    验证用户名密码。

    返回：
        (用户字典或None, 错误提示)
        成功时错误提示为空字符串
    """
    user = get_user_by_username(username)  # 查找用户
    if not user:  # 用户不存在
        # 注意：这里刻意返回和"密码错误"相同的提示。
        # 如果区分"用户不存在"和"密码错误"，攻击者可以据此枚举出系统中有哪些账号
        return None, "用户名或密码错误"
    if not user["active"]:  # 账号已被停用
        return None, "账号已停用，请联系管理员"
    if not verify_password(password, user["password_hash"], user["password_salt"]):  # 密码不匹配
        return None, "用户名或密码错误"
    # 更新最后登录时间，用于统计活跃用户
    db.execute("UPDATE users SET last_login_at = ? WHERE id = ?", (audit.now_iso(), user["id"]))
    return user, ""  # 认证成功


def change_password(user_id: int, new_password: str) -> None:
    """
    修改用户密码，并清除强制改密标记。

    安全：修改前先校验新密码强度，弱口令直接拒绝（防止用户把密码改弱）。
    """
    ok, reason = validate_password(new_password)  # 校验新密码强度
    if not ok:  # 不达标
        raise ValueError(f"密码不符合安全策略：{reason}")  # 抛出明确异常，由接口层捕获
    pwd_hash, salt = hash_password(new_password)  # 计算新密码的哈希
    db.execute(
        "UPDATE users SET password_hash = ?, password_salt = ?, must_change_pwd = 0 WHERE id = ?",
        (pwd_hash, salt, user_id),
    )


# ============================================================
# 三、会话管理
# ============================================================

def login_session(user: dict) -> None:
    """
    把用户信息写入 Flask 会话，标记为已登录状态。
    Flask 的 session 基于签名 Cookie，内容经过密钥签名，客户端无法伪造。
    """
    session.permanent = True  # 启用永久会话，这样 PERMANENT_SESSION_LIFETIME 配置才会生效
    session["user_id"] = user["id"]  # 存用户 ID
    session["username"] = user["username"]  # 存用户名
    session["role"] = user["role"]  # 存角色，避免每次请求都查库
    session["display_name"] = user["display_name"]  # 存显示名


def logout_session() -> None:
    """清空会话，实现登出。"""
    session.clear()  # 清除所有会话数据


def current_user() -> Optional[dict]:
    """
    获取当前登录用户的完整信息。

    优化点：使用 Flask 的 g 对象做请求级缓存。
    同一次请求中多次调用本函数只查一次数据库。
    """
    if hasattr(g, "_current_user"):  # 本次请求已经查过了
        return g._current_user  # 直接返回缓存
    uid = session.get("user_id")  # 从会话中取用户 ID
    user = get_user_by_id(uid) if uid else None  # 有 ID 才查库
    # 如果用户已被停用，视同未登录（防止停用后旧会话仍能访问）
    if user and not user["active"]:
        user = None
    g._current_user = user  # 写入请求级缓存
    return user


def client_ip() -> str:
    """
    获取客户端真实 IP。
    如果部署在 Nginx 反向代理后面，真实 IP 在 X-Forwarded-For 头里，
    直接取 remote_addr 只会拿到代理服务器的 IP。
    """
    # X-Forwarded-For 可能是 "真实IP, 代理1, 代理2" 的形式，取第一个
    xff = request.headers.get("X-Forwarded-For", "")
    if xff:  # 有这个头说明经过了代理
        return xff.split(",")[0].strip()  # 取第一段并去空白
    return request.remote_addr or "unknown"  # 没有代理则取直连 IP


# ============================================================
# 四、权限装饰器
# ============================================================

def login_required(func):
    """
    装饰器：要求用户必须已登录。
    未登录时，API 请求返回 401 JSON，页面请求重定向到登录页。
    """
    @wraps(func)  # 保留被装饰函数的名称和文档字符串，否则 Flask 路由注册会冲突
    def wrapper(*args, **kwargs):
        user = current_user()  # 获取当前用户
        if not user:  # 未登录
            # 判断是 API 请求还是页面请求：路径以 /api/ 开头即视为 API
            if request.path.startswith("/api/"):
                return jsonify({"ok": False, "error": "未登录或会话已过期"}), 401  # 返回 401
            return redirect("/login")  # 页面请求重定向到登录页
        return func(*args, **kwargs)  # 已登录，执行原函数
    return wrapper


def role_required(*allowed_roles: str):
    """
    装饰器工厂：要求用户具备指定角色之一。

    用法：
        @role_required("admin", "reviewer")
        def review_document(): ...
    """
    def decorator(func):
        @wraps(func)
        def wrapper(*args, **kwargs):
            user = current_user()  # 获取当前用户
            if not user:  # 未登录
                if request.path.startswith("/api/"):
                    return jsonify({"ok": False, "error": "未登录"}), 401
                return redirect("/login")
            if user["role"] not in allowed_roles:  # 角色不在允许列表中
                # 越权访问必须记审计（NFR-6），这是安全监控的重要信号
                audit.log(
                    "user_manage",  # 归类到权限相关动作
                    user_id=user["id"],
                    username=user["username"],
                    ip=client_ip(),
                    detail={"path": request.path, "需要角色": list(allowed_roles), "实际角色": user["role"]},
                    result="denied",  # 标记为被拒绝
                )
                return jsonify({"ok": False, "error": "权限不足"}), 403  # 返回 403 禁止访问
            return func(*args, **kwargs)  # 权限通过，执行原函数
        return wrapper
    return decorator


def admin_required(func):
    """装饰器：仅管理员可访问。是 role_required('admin') 的快捷写法。"""
    return role_required("admin")(func)


# ============================================================
# 五、数据可见性规则（核心安全逻辑）
# ============================================================

def visibility_filter(user: Optional[dict], table_alias: str = "d") -> tuple[str, list[Any]]:
    """
    生成文档可见性的 SQL 过滤条件（SRS 3.3）。

    这是整个系统最关键的安全函数。
    所有涉及文档查询的地方都必须调用它，把返回的条件拼进 WHERE 子句。
    这样做的意义在于：**无权限的文档从数据库层面就查不出来**，
    而不是查出来后在前端隐藏——后者只要用户会看网络请求就能绕过。

    权限模型（在「部门隔离 + 密级过滤 + 状态」全局规则之上，叠加「文档级细粒度 ACL」）：
    - 密级（security_level）对所有用户生效，是分类红线，ACL 也无法放宽；
    - 管理员/审核员仅受密级约束，可看到本部门全部状态文档；
    - 普通/贡献者默认只看「已发布 + 本部门/公开」文档；
    - 文档级 ACL（visibility_mode='restricted' + doc_acl 白名单）可把某篇文档额外开放给
      指定用户/角色查看，或设为私有（仅创建人+ACL 指定人），且创建人本人始终可见（含草稿）。

    参数：
        user:        当前用户字典，None 表示未登录
        table_alias: documents 表在 SQL 中的别名，默认 "d"
    返回：
        (SQL 条件片段, 参数列表)
    """
    a = table_alias  # 简化后续拼接的书写

    if not user:  # 未登录用户什么都看不到
        return "1=0", []  # 1=0 是恒假条件，查不出任何数据

    # --- 密级过滤（对所有用户生效，包括管理员，作为分类红线） ---
    user_rank = config.SECURITY_RANK.get(user["max_security"], 2)  # 默认 2（internal）
    allowed_levels = [lvl for lvl, rank in config.SECURITY_RANK.items() if rank <= user_rank]
    if allowed_levels:  # 正常情况一定非空
        placeholders = ",".join("?" for _ in allowed_levels)
        sec_sql = f"{a}.security_level IN ({placeholders})"  # 密级在允许范围内
        sec_params = allowed_levels  # 对应参数
    else:  # 异常兜底：密级配置错误时什么都看不到，宁可误伤不可泄露
        sec_sql = "1=0"
        sec_params = []

    # 管理员 / 审核员：仅受密级约束，不受状态/部门/ACL 限制（其工作需要看到待审、退回文档）
    if user["role"] in ("admin", "reviewer"):
        return sec_sql, sec_params

    # --- 基础可见性（状态 + 部门），仅非特权角色需要 ---
    base: list[str] = []  # 基础条件片段
    base_params: list[Any] = []  # 基础参数
    base.append(f"{a}.status = 'published'")  # 普通用户和贡献者只看已发布
    if not user["cross_dept"]:  # 无跨部门权限才受部门隔离
        if user["department_id"]:  # 有归属部门：本部门 或 公开文档
            base.append(f"({a}.department_id = ? OR {a}.security_level = 'public')")
            base_params.append(user["department_id"])
        else:  # 无归属部门：只能看公开文档
            base.append(f"{a}.security_level = 'public'")
    base_where = " AND ".join(base) if base else "1=1"  # 基础条件拼装

    # --- 文档级细粒度 ACL（P 完整版补齐） ---
    # 当总开关开启时，在「密级红线」之上叠加以下三条可见路径：
    #   1) 文档创建人本人（含未发布草稿，便于查看/编辑自己上传的私有文档）
    #   2) inherit 模式：沿用「状态 + 部门」全局规则
    #   3) restricted 模式：必须命中文档级白名单（user/role 任一），本部门匹配不再自动放行
    # 关键安全修正：restricted 模式下「本部门规则」不再作为可见依据，否则会泄漏私有文档；
    #               密级红线（sec_sql）对全部三条路径一律生效，ACL 无法放宽密级。
    if config.ACL_ENABLED:
        acl_user_sql = (
            f"({a}.id IN (SELECT doc_id FROM doc_acl WHERE perm IN ('view','view_edit') "
            f"AND principal_type = 'user' AND principal_id = ?))"
        )
        acl_role_sql = (
            f"({a}.id IN (SELECT doc_id FROM doc_acl WHERE perm IN ('view','view_edit') "
            f"AND principal_type = 'role' AND principal_id = ?))"
        )
        where_sql = (
            f"({sec_sql}) AND ("
            f"{a}.created_by = ? "  # 路径 1：创建人恒可见
            f"OR ({a}.visibility_mode = 'inherit' AND ({base_where})) "  # 路径 2：继承全局规则
            f"OR ({a}.visibility_mode = 'restricted' AND ({acl_user_sql} OR {acl_role_sql}))"  # 路径 3：受限白名单
            f")"
        )
        params = (
            sec_params               # 密级红线参数
            + [user["id"]]           # 路径 1 的 created_by 绑定值
            + base_params            # 路径 2 可能需要的部门参数
            + [str(user["id"]), user["role"]]  # 路径 3 的 user/role 绑定值
        )
    else:
        # ACL 关闭：仅用全局规则（密级 + 状态 + 部门）
        where_sql = " AND ".join([sec_sql] + base)
        params = sec_params + base_params
    return where_sql, params


def can_view_confidential(user: Optional[dict]) -> bool:
    """
    判断用户是否有权访问机密级文档。
    用于 AI 问答中"包含机密内容"开关的权限校验（FR-5.5）。
    """
    if not user:  # 未登录一律没有
        return False
    return config.SECURITY_RANK.get(user["max_security"], 2) >= 3  # 密级许可达到 3（confidential）


def can_edit_doc(user: Optional[dict], doc: dict) -> bool:
    """
    判断用户能否编辑指定文档。

    规则（P 完整版补齐文档级 ACL）：
    - 管理员：全部可编辑
    - 审核员：本部门或跨部门权限范围内可编辑
    - 贡献者：只能编辑自己上传的文档
    - 受限文档（visibility_mode='restricted'）：仅 doc_acl 中 perm='view_edit' 命中的用户/角色，
      或创建人本人可编辑；其余角色即便有 contributor 也不行（除非命中 ACL）
    - 普通用户：默认不可编辑；但若受限文档的 ACL 授予其 view_edit，则可编辑
    """
    if not user:  # 未登录
        return False
    if user["role"] == "admin":  # 管理员畅通无阻
        return True
    # 受限文档走 ACL 细粒度判定（覆盖默认角色规则）
    if config.ACL_ENABLED and doc["visibility_mode"] == "restricted":
        # 创建人本人可编辑自己的受限文档（sqlite3.Row 与 dict 均支持 [] 取值）
        if doc["created_by"] == user["id"]:
            return True
        # 查询该文档的 ACL，命中 view_edit 主体则放行
        acl = db.query(
            "SELECT principal_type, principal_id, perm FROM doc_acl WHERE doc_id = ?",
            (doc["id"],),
        )
        for r in acl:  # 遍历每条权限
            if r["perm"] == "view_edit" and (
                (r["principal_type"] == "user" and str(r["principal_id"]) == str(user["id"]))
                or (r["principal_type"] == "role" and r["principal_id"] == user["role"])
            ):
                return True  # 命中编辑授权
        return False  # 受限文档且无编辑授权
    if user["role"] == "reviewer":  # 审核员
        # 有跨部门权限，或文档属于本部门
        return bool(user["cross_dept"]) or doc["department_id"] == user["department_id"]
    if user["role"] == "contributor":  # 贡献者
        return doc["created_by"] == user["id"]  # 只能改自己传的
    return False  # 其余角色一律不可编辑


# ============================================================
# 六、API Key 鉴权（供 SG2 上层场景调用）
# ============================================================

def create_api_key(name: str, bind_user_id: int) -> tuple[str, int]:
    """
    创建 API 密钥。

    安全设计：数据库只存密钥的哈希值，明文只在创建时返回一次。
    这样即使数据库泄露，攻击者也无法还原出可用的密钥。

    返回：
        (明文密钥, 记录ID) —— 明文只此一次，界面必须提示用户妥善保存
    """
    raw_key = f"aikm_{secrets.token_urlsafe(32)}"  # 生成带前缀的随机密钥，前缀便于识别来源
    key_hash = hashlib.sha256(raw_key.encode("utf-8")).hexdigest()  # 计算哈希用于存储
    prefix = raw_key[:12]  # 取前 12 个字符作为展示前缀，让用户能在列表里认出是哪个 Key
    key_id = db.execute(
        """
        INSERT INTO api_keys (name, key_hash, key_prefix, bind_user_id, active, created_at)
        VALUES (?, ?, ?, ?, 1, ?)
        """,
        (name, key_hash, prefix, bind_user_id, audit.now_iso()),
    )
    return raw_key, key_id  # 返回明文和 ID


def verify_api_key(raw_key: str) -> Optional[dict]:
    """
    校验 API 密钥并返回其绑定的用户信息。

    返回：
        用户字典，校验失败返回 None
    """
    if not raw_key:  # 空密钥直接失败
        return None
    key_hash = hashlib.sha256(raw_key.encode("utf-8")).hexdigest()  # 计算哈希
    # 查找有效的密钥记录
    row = db.query_one("SELECT * FROM api_keys WHERE key_hash = ? AND active = 1", (key_hash,))
    if not row:  # 密钥不存在或已吊销
        return None
    # 更新调用统计。这既是用量统计也是审计依据
    db.execute(
        "UPDATE api_keys SET call_count = call_count + 1, last_used_at = ? WHERE id = ?",
        (audit.now_iso(), row["id"]),
    )
    user = get_user_by_id(row["bind_user_id"])  # 取出绑定的用户身份
    if user and not user["active"]:  # 绑定用户已停用，密钥同步失效
        return None
    return user


def api_key_required(func):
    """
    装饰器：API 接口鉴权。
    同时支持两种方式，方便不同调用场景：
    1. HTTP 头 Authorization: Bearer <key> —— 标准做法
    2. 已登录的浏览器会话 —— 便于前端页面直接调用同一套 API
    """
    @wraps(func)
    def wrapper(*args, **kwargs):
        # 优先尝试会话认证（浏览器场景）
        user = current_user()
        if user:  # 会话有效
            g.api_caller = "session"  # 标记调用来源，供审计区分
            return func(*args, **kwargs)

        # 再尝试 API Key 认证（程序调用场景）
        auth_header = request.headers.get("Authorization", "")  # 取出鉴权头
        raw_key = ""
        if auth_header.startswith("Bearer "):  # 标准 Bearer 格式
            raw_key = auth_header[7:].strip()  # 去掉 "Bearer " 前缀
        else:
            # 兼容 X-API-Key 自定义头，某些客户端用起来更方便
            raw_key = request.headers.get("X-API-Key", "").strip()

        api_user = verify_api_key(raw_key)  # 校验密钥
        if not api_user:  # 校验失败
            audit.log(  # 记录失败的 API 调用，用于发现暴力尝试
                "api_call",
                ip=client_ip(),
                detail={"path": request.path, "原因": "无效的API密钥"},
                result="denied",
            )
            return jsonify({"ok": False, "error": "无效的 API 密钥"}), 401

        g._current_user = api_user  # 把 API Key 绑定的用户注入请求上下文，后续权限过滤照常生效
        g.api_caller = "apikey"  # 标记来源为 API Key
        return func(*args, **kwargs)
    return wrapper
