# -*- coding: utf-8 -*-
"""
评测用例导入脚本
================
把 evalset/ 下的 CSV 评测集导入 eval_cases 表，供评测运行使用。

CSV 字段（首行为表头）：
    question        测试问题（必填）
    expect_doc_ids  期望命中文档 ID，逗号分隔（必填，金标准）
    category_l1     所属知识域（可选，用于分域统计）
    case_type       normal / paraphrase / outside（可选，默认 normal）
    note            备注（可选）
    active          1/0 是否启用（可选，默认 1）

用法：
    python scripts/import_eval.py                 # 导入 evalset/sample_cases.csv
    python scripts/import_eval.py evalset/my.csv  # 导入指定文件
"""

import csv  # CSV 解析
import sys  # 模块路径 / 退出
from pathlib import Path  # 路径

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# 在导入 app 之前先加载 .env，避免 config 读默认值
import run as _run  # 复用 run.py 的 .env 预加载函数
_run._preload_env()

from app import create_app, db  # 内部模块


def import_csv(path: Path) -> tuple[int, int]:
    """
    导入单个 CSV 文件。

    返回：(成功数, 跳过数)
    """
    ok = 0  # 成功计数
    skip = 0  # 跳过计数（缺必填项）
    with path.open(encoding="utf-8-sig") as f:  # utf-8-sig 兼容带 BOM 的 Excel 导出文件
        reader = csv.DictReader(f)  # 按表头读取为字典
        for row in reader:  # 逐行
            question = (row.get("question") or "").strip()
            expect = (row.get("expect_doc_ids") or "").strip()
            if not question or not expect:  # 必填项缺失则跳过该行
                skip += 1
                continue
            # 查重：同一问题已存在则跳过
            if db.query_one("SELECT id FROM eval_cases WHERE question = ?", (question,)):
                skip += 1
                continue
            db.execute(
                """
                INSERT INTO eval_cases
                    (question, expect_doc_ids, category_l1, case_type, note, active, created_at)
                VALUES (?,?,?,?,?,?,?)
                """,
                (
                    question,
                    expect,
                    (row.get("category_l1") or "").strip(),
                    (row.get("case_type") or "normal").strip(),
                    (row.get("note") or "").strip(),
                    1 if (row.get("active") or "1").strip() != "0" else 0,
                    # created_at 用数据库 now 等价：直接写当前时间字符串
                    __import__("datetime").datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                ),
            )
            ok += 1
    return ok, skip


def main() -> None:
    """导入默认或指定的评测 CSV。"""
    create_app()  # 初始化数据库
    # 默认文件路径
    default = ROOT / "evalset" / "sample_cases.csv"
    target = Path(sys.argv[1]) if len(sys.argv) > 1 else default
    if not target.exists():  # 文件不存在
        print(f"评测文件不存在：{target}")
        sys.exit(1)
    ok, skip = import_csv(target)
    print(f"导入完成：新增 {ok} 条，跳过 {skip} 条（来自 {target.name}）")


if __name__ == "__main__":
    main()
