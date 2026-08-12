# -*- coding: utf-8 -*-
"""
初始化数据脚本
==============
用途：在已建表的基础上，写入运行所需的"基础数据"：
  1. 根部门（知识管理中心）+ 可选的示例部门
  2. 二级分类初始值（来自 config.CATEGORIES_L2，写入 categories_l2 表，支持运行时扩展）
  3. 管理员账号（create_app 也会播种，这里兜底确保存在）

用法：
    python scripts/init_data.py
或直接由 create_app() 在启动时自动调用（幂等，可重复执行）。
"""

import sys  # 模块路径
from pathlib import Path  # 路径

# 把项目根加入模块搜索路径，保证 `import app` 可用
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# 在导入 app 之前先加载 .env，避免 config 读默认值
import run as _run  # 复用 run.py 的 .env 预加载函数（顶层 import 不触发 app 加载）
_run._preload_env()

from app import config, db, auth, audit  # 内部模块


def seed_categories_l2() -> int:
    """把 config 中的二级分类初始值写入 categories_l2 表（已存在则跳过）。"""
    count = 0  # 实际写入条数
    for parent, subs in config.CATEGORIES_L2.items():  # 遍历每个一级分类
        order = 0  # 排序权重
        for code, name in subs.items():  # 遍历其下二级分类
            order += 1
            # INSERT OR IGNORE：主键冲突（code 已存在）则跳过，保证可重复执行
            existing = db.query_one("SELECT code FROM categories_l2 WHERE code = ?", (code,))
            if existing:  # 已存在跳过
                continue
            db.execute(
                "INSERT INTO categories_l2 (code, parent, name, sort_order, active) VALUES (?,?,?,?,1)",
                (code, parent, name, order),
            )
            count += 1
    return count


def seed_departments() -> int:
    """确保根部门存在；示例部门可选创建（注释掉避免污染真实环境）。"""
    count = 0
    if not db.query_one("SELECT id FROM departments WHERE name = ?", ("知识管理中心",)):
        db.execute(
            "INSERT INTO departments (name, code, contact, created_at) VALUES (?,?,?,?)",
            ("知识管理中心", "KM", "系统管理员", audit.now_iso()),
        )
        count += 1
    return count


def seed_admin() -> None:
    """兜底确保管理员账号存在。"""
    if not auth.get_user_by_username("admin"):
        auth.create_user(
            username="admin", display_name="系统管理员", password="Admin@123456",
            role="admin", department_id=1, max_security="confidential",
            cross_dept=True, must_change_pwd=True,
        )
        print("已创建默认管理员 admin / Admin@123456")


def main() -> None:
    """脚本入口：建表 + 播种。"""
    db.init_db()  # 确保表已存在
    dep = seed_departments()  # 部门
    cat = seed_categories_l2()  # 二级分类
    seed_admin()  # 管理员
    audit.log("config_change", username="system", detail={"动作": "初始化基础数据"}, result="success")
    print(f"初始化完成：新增部门 {dep} 个，新增二级分类 {cat} 个。")


if __name__ == "__main__":
    main()
