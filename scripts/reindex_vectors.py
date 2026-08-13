# -*- coding: utf-8 -*-
"""
全量重建向量索引脚本
====================
用途：切换 embedding 模型（例如从 hash 本地降级切换到本地 Ollama 的 qwen3-embedding）之后，
      库里所有文档的切片向量还是旧模型生成的，必须全部重新向量化，否则语义检索会完全错乱。

本脚本做的事情：
    1. 复用 run._preload_env() 在 import app 之前把 .env 注入环境变量（与 run.py / wsgi.py 顺序一致）
    2. create_app() 初始化数据库（建表 + 种子数据，幂等，可重复执行）
    3. 调用 ingest.reindex_all() 对所有非下架文档重新切片 + 重新向量化

直接运行：
    /Users/ikingsmart/.workbuddy/binaries/python/envs/aikm/bin/python scripts/reindex_vectors.py
"""

import sys  # 系统模块，用于把项目根加入模块搜索路径
from pathlib import Path  # 路径处理，用于定位项目根目录

# ROOT 是项目根目录（scripts 的上一级）。把根目录加入 sys.path，保证 import app 一定能找到
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# 关键顺序：必须在 import 任何 app 模块之前注入 .env，否则 config 顶层读到的是默认值
import run as _run  # 复用 run 模块的 _preload_env 函数（只定义函数，import 不触发 app 加载）
_run._preload_env()  # 把 .env 里的键值写入 os.environ（仅对未设置的变量 setdefault）

from app import create_app, config  # 应用工厂 + 配置
from app.ingest import reindex_all  # 全量重建索引函数


def main() -> None:
    """脚本主流程：打印当前 embedding 配置 → 初始化应用 → 全量重建索引 → 打印结果。"""
    # 先打印当前生效的向量配置，便于确认确实切到了 Ollama 而不是 hash 降级
    print(f"[reindex] embedding 服务商={config.EMBED_PROVIDER} 模型={config.EMBED_MODEL} 维度={config.EMBED_DIM}")

    # create_app() 内部会 db.init_db()（建表）+ 种子初始数据，保证数据库已就绪
    app = create_app()

    # 在应用上下文里执行重建（部分 db 操作依赖 Flask 上下文；create_app 已确保连接可用）
    with app.app_context():
        result = reindex_all()  # 对所有非 archived 文档重新切片 + 重新向量化

    # 打印重建结果，便于肉眼确认成功数与失败数
    print("[reindex] 重建结果：", result)


if __name__ == "__main__":
    main()
