# -*- coding: utf-8 -*-
"""
评测运行脚本（CLI）
==================
在命令行运行检索准确率评测，验证是否达成 SRS 核心 KPI：
    **AI 检索准确率 ≥ 90%（Recall@5 ≥ 0.90）**

前置：先把评测用例导入 eval_cases 表（见 scripts/import_eval.py 与 evalset/ 目录）。

用法：
    python scripts/run_eval.py                 # 默认 混合检索 / K=5
    python scripts/run_eval.py --mode vector --k 5
    python scripts/run_eval.py --mode bm25
"""

import argparse  # 命令行参数解析
import sys  # 模块路径
from pathlib import Path  # 路径

# 项目根加入搜索路径
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# 在导入 app 之前先加载 .env，避免 config 读默认值
import run as _run  # 复用 run.py 的 .env 预加载函数
_run._preload_env()

from app import create_app, db, eval as eval_mod  # 内部模块


def main() -> None:
    """解析参数、建应用、跑评测、打印结论。"""
    # 命令行参数定义
    parser = argparse.ArgumentParser(description="AI-KM 检索准确率评测")
    parser.add_argument("--mode", default="hybrid", choices=["hybrid", "bm25", "vector"], help="检索模式")
    parser.add_argument("--k", type=int, default=5, help="K 值（Recall@K）")
    args = parser.parse_args()

    # 必须先创建 app 以初始化数据库与配置
    create_app()

    print(f"▶ 运行评测：模式={args.mode}  K={args.k}")
    result = eval_mod.run_eval(mode=args.mode, top_k=args.k, run_by=None)

    if not result.get("ok"):  # 评测集为空等失败
        print("✗ 评测未执行：", result.get("message", "未知原因"))
        print("  请先导入评测用例：python scripts/import_eval.py")
        sys.exit(1)

    recall = result["recall_at_k"]
    target = "✅ 达标 (≥90%)" if result["meet_target"] else "⚠️ 未达标 (<90%)"
    print(f"  总用例 : {result['total']}")
    print(f"  通过   : {result['passed']}")
    print(f"  Recall@{args.k} : {recall}   {target}")
    print(f"  MRR    : {result['mrr']}")
    print(f"  运行ID : {result['run_id']}")
    # 未达标时以非 0 退出码提示 CI / 人工关注
    sys.exit(0 if result["meet_target"] else 2)


if __name__ == "__main__":
    main()
