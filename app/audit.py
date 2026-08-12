# -*- coding: utf-8 -*-
"""
审计日志模块
============
职责：记录系统中所有关键操作，形成"只增不改"的可信底账（SRS FR-11、NFR-6）。

设计红线：
本模块**不提供任何删除或修改审计记录的函数**。
这是刻意为之——审计日志一旦可改，就失去了审计价值。
如果将来确有归档需求，也只能由 DBA 在数据库层面操作，并留下操作痕迹。
"""

import json  # 用于把详情字典序列化成 JSON 字符串存库
import time  # 用于计算操作耗时
from datetime import datetime  # 用于生成时间戳
from typing import Any, Optional  # 类型注解

from . import db  # 数据库访问层


# ACTIONS 枚举了系统中所有被审计的动作类型（对应 SRS FR-11.1 的 11 类事件）
# 集中定义的好处：避免各处随手写字符串导致同一动作出现多种拼写，无法统计
ACTIONS = {
    "login": "登录",
    "logout": "登出",
    "login_fail": "登录失败",
    "upload": "上传文档",
    "batch_import": "批量导入",
    "review": "审核文档",
    "publish": "发布文档",
    "reject": "退回文档",
    "archive": "下架文档",
    "delete": "删除文档",
    "update_doc": "修改文档",
    "doc_edit": "在线编辑文档",
    "doc_rollback": "回滚文档版本",
    "space_manage": "知识树管理",
    "tag_manage": "标签管理",
    "doc_comment": "文档评论",
    "search": "检索知识",
    "chat": "AI问答",
    "download": "下载原文",
    "view_doc": "查看文档",
    "config_change": "修改系统配置",
    "user_manage": "用户管理",
    "api_call": "API调用",
    "eval_run": "运行评测",
    "export_audit": "导出审计日志",
    "reindex": "重建索引",
    "confidential_access": "访问机密内容",
    # ---- P2 新增动作 ----
    "mfa_enable": "MFA管理",       # 开启/关闭多因子认证
    "mfa_verify": "MFA验证",       # 登录时二次验证（成功/失败）
    "sso_login": "SSO登录",        # 通过 LDAP/OIDC 单点登录
    "diagram_save": "保存内嵌图",   # diagrams.net 画图保存
}


def now_iso() -> str:
    """
    返回当前时间的 ISO8601 格式字符串（精确到秒）。
    统一时间格式，避免各处用不同格式导致排序和筛选出错。
    """
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")  # 例如 "2026-08-11 10:30:45"


def log(
    action: str,
    user_id: Optional[int] = None,
    username: Optional[str] = None,
    ip: Optional[str] = None,
    target_type: Optional[str] = None,
    target_id: Optional[Any] = None,
    detail: Optional[dict] = None,
    result: str = "success",
    cost_ms: int = 0,
    sensitive: bool = False,
) -> None:
    """
    写入一条审计日志。

    参数：
        action:      动作类型，应取自 ACTIONS 的键
        user_id:     操作人 ID
        username:    操作人用户名（存快照，用户改名后历史记录仍可读）
        ip:          来源 IP 地址
        target_type: 操作对象类型，如 "document" / "user"
        target_id:   操作对象 ID
        detail:      详细信息字典，会被序列化为 JSON
        result:      结果：success 成功 / fail 失败 / denied 越权被拒
        cost_ms:     操作耗时（毫秒）
        sensitive:   是否涉及机密文档，True 则可被一键筛选（FR-11.5）

    注意：
    本函数内部吞掉所有异常。原因是审计失败不应该导致业务失败——
    用户正常检索时如果因为日志表写不进去而报错，那是本末倒置。
    但审计失败会打印到控制台，运维能发现。
    """
    try:
        # 把详情字典序列化成 JSON 字符串。ensure_ascii=False 保证中文不被转义成 \uXXXX
        detail_str = json.dumps(detail, ensure_ascii=False) if detail else None
        # 插入审计表。全部使用参数化占位符，杜绝 SQL 注入
        db.execute(
            """
            INSERT INTO audit_logs
                (ts, user_id, username, ip, action, target_type, target_id,
                 detail, result, cost_ms, sensitive)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                now_iso(),  # 时间戳
                user_id,  # 用户 ID
                username,  # 用户名快照
                ip,  # 来源 IP
                action,  # 动作类型
                target_type,  # 对象类型
                str(target_id) if target_id is not None else None,  # 对象 ID 统一转字符串存储
                detail_str,  # 详情 JSON
                result,  # 结果
                cost_ms,  # 耗时
                1 if sensitive else 0,  # 布尔值转成 SQLite 的 0/1
            ),
        )
    except Exception as exc:  # 捕获一切异常，保证审计失败不影响主流程
        print(f"[审计写入失败] action={action} error={exc}")  # 打印到控制台供运维排查


class Timer:
    """
    耗时计时器，配合 with 语句使用，用于统计操作耗时。

    用法：
        with Timer() as t:
            do_something()
        audit.log("search", cost_ms=t.ms)
    """

    def __enter__(self):
        """进入 with 代码块时调用，记录开始时刻。"""
        self.start = time.time()  # 记录当前时间戳（秒，带小数）
        self.ms = 0  # 初始化耗时为 0
        return self  # 返回自身，让 as t 能拿到这个对象

    def __exit__(self, exc_type, exc_val, exc_tb):
        """离开 with 代码块时调用，计算总耗时。"""
        # 用结束时间减开始时间，乘 1000 转成毫秒，取整
        self.ms = int((time.time() - self.start) * 1000)
        return False  # 返回 False 表示不吞掉代码块中发生的异常


def search_logs(
    action: Optional[str] = None,
    user_id: Optional[int] = None,
    keyword: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    sensitive_only: bool = False,
    limit: int = 200,
    offset: int = 0,
) -> tuple[list[dict], int]:
    """
    查询审计日志（供管理员界面使用，FR-11.4）。

    返回：
        (日志字典列表, 符合条件的总条数)
    """
    where: list[str] = []  # 存放各个 WHERE 条件片段
    params: list[Any] = []  # 存放对应的参数值（与占位符一一对应）

    if action:  # 按动作类型过滤
        where.append("action = ?")  # 添加条件
        params.append(action)  # 添加参数
    if user_id:  # 按操作人过滤
        where.append("user_id = ?")
        params.append(user_id)
    if keyword:  # 按关键词模糊匹配用户名或详情内容
        where.append("(username LIKE ? OR detail LIKE ? OR target_id LIKE ?)")
        kw = f"%{keyword}%"  # 构造 LIKE 的模糊匹配串
        params.extend([kw, kw, kw])  # 三个占位符需要三个参数
    if date_from:  # 起始日期
        where.append("ts >= ?")
        params.append(date_from)
    if date_to:  # 结束日期，加 " 23:59:59" 保证包含当天全天
        where.append("ts <= ?")
        params.append(date_to + " 23:59:59")
    if sensitive_only:  # 只看涉及机密的记录
        where.append("sensitive = 1")

    # 把条件片段用 AND 拼起来；没有任何条件时用 "1=1" 占位保证 SQL 合法
    where_sql = " AND ".join(where) if where else "1=1"

    # 先查总数，用于前端分页显示
    total_row = db.query_one(f"SELECT COUNT(*) AS c FROM audit_logs WHERE {where_sql}", params)
    total = total_row["c"] if total_row else 0  # 取出计数值

    # 再查当前页数据，按时间倒序（最新的在前）
    rows = db.query(
        f"""
        SELECT * FROM audit_logs
        WHERE {where_sql}
        ORDER BY id DESC
        LIMIT ? OFFSET ?
        """,
        params + [limit, offset],  # 参数列表末尾追加分页参数
    )
    return db.rows_to_dicts(rows), total  # 转成字典列表返回


def export_csv(rows: list[dict]) -> str:
    """
    把审计日志导出为 CSV 文本（FR-11.4）。

    参数：
        rows: 日志字典列表
    返回：
        CSV 格式的字符串，可直接写文件或作为 HTTP 响应下载
    """
    # 定义 CSV 表头，用中文便于业务方直接查看
    headers = ["时间", "用户", "IP", "动作", "对象类型", "对象ID", "结果", "耗时(ms)", "涉密", "详情"]
    lines = [",".join(headers)]  # 第一行是表头

    def esc(value: Any) -> str:
        """内部函数：CSV 字段转义。含逗号、引号、换行的字段必须用双引号包裹。"""
        s = "" if value is None else str(value)  # None 转空字符串
        # 如果包含特殊字符，需要转义
        if any(c in s for c in [",", '"', "\n", "\r"]):
            s = s.replace('"', '""')  # CSV 规范：内部的双引号要变成两个双引号
            return f'"{s}"'  # 整体用双引号包裹
        return s  # 无特殊字符直接返回

    for r in rows:  # 遍历每条日志
        lines.append(",".join([  # 拼成一行 CSV
            esc(r.get("ts")),  # 时间
            esc(r.get("username")),  # 用户名
            esc(r.get("ip")),  # IP
            esc(ACTIONS.get(r.get("action", ""), r.get("action"))),  # 动作转中文，未知则原样输出
            esc(r.get("target_type")),  # 对象类型
            esc(r.get("target_id")),  # 对象 ID
            esc(r.get("result")),  # 结果
            esc(r.get("cost_ms")),  # 耗时
            esc("是" if r.get("sensitive") else "否"),  # 是否涉密转中文
            esc(r.get("detail")),  # 详情
        ]))
    # 用换行符连接所有行。开头加 BOM（\ufeff）是为了让 Excel 正确识别 UTF-8 中文，否则会乱码
    return "\ufeff" + "\n".join(lines)


def stats_summary(days: int = 30) -> dict:
    """
    审计统计概览，供管理看板展示。

    参数：
        days: 统计最近多少天
    返回：
        包含各类统计数据的字典
    """
    # 计算起始时间：当前时间戳减去 days 天的秒数，再格式化
    since = datetime.fromtimestamp(time.time() - days * 86400).strftime("%Y-%m-%d 00:00:00")

    # 按动作类型分组统计次数，取前 15 名
    by_action = db.query(
        """
        SELECT action, COUNT(*) AS c FROM audit_logs
        WHERE ts >= ? GROUP BY action ORDER BY c DESC LIMIT 15
        """,
        (since,),
    )
    # 按用户分组统计活跃度，取前 10 名
    by_user = db.query(
        """
        SELECT username, COUNT(*) AS c FROM audit_logs
        WHERE ts >= ? AND username IS NOT NULL
        GROUP BY username ORDER BY c DESC LIMIT 10
        """,
        (since,),
    )
    # 统计涉密访问次数
    sens_row = db.query_one(
        "SELECT COUNT(*) AS c FROM audit_logs WHERE ts >= ? AND sensitive = 1", (since,)
    )
    # 统计被拒绝的越权访问次数（安全监控的关键指标）
    denied_row = db.query_one(
        "SELECT COUNT(*) AS c FROM audit_logs WHERE ts >= ? AND result = 'denied'", (since,)
    )
    # 统计总记录数
    total_row = db.query_one("SELECT COUNT(*) AS c FROM audit_logs WHERE ts >= ?", (since,))

    return {
        "days": days,  # 统计天数
        "total": total_row["c"] if total_row else 0,  # 总操作数
        "sensitive_access": sens_row["c"] if sens_row else 0,  # 涉密访问数
        "denied": denied_row["c"] if denied_row else 0,  # 越权拒绝数
        # 把动作编码转成中文名再返回，前端无需再做映射
        "by_action": [
            {"action": ACTIONS.get(r["action"], r["action"]), "count": r["c"]} for r in by_action
        ],
        "by_user": [{"user": r["username"], "count": r["c"]} for r in by_user],  # 用户活跃排行
    }
