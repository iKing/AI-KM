# -*- coding: utf-8 -*-
"""
AI-KM 应用启动入口
==================
直接运行本文件即可启动知识管理平台：
    python run.py
或用环境变量覆盖配置后启动：
    AIKM_PORT=8080 AIKM_DEBUG=true python run.py

【关于 .env 加载顺序的重要说明】
config.py 在模块级别就会读取环境变量（如 AIKM_DATA_DIR、AIKM_LLM_API_KEY 等）。
因此"加载 .env"必须在 `import app.config` 之前完成，
否则那些配置项读到的是默认值而不是 .env 里的值。
本文件先手动把 .env 注入 os.environ，再创建 app，顺序不可颠倒。
"""

import os  # 操作系统接口，用于读写环境变量
import sys  # 系统相关，用于修改模块搜索路径
from pathlib import Path  # 路径处理

# 项目根目录（run.py 所在目录）
ROOT = Path(__file__).resolve().parent
# 把根目录加入模块搜索路径，保证 `import app` 一定能找到
sys.path.insert(0, str(ROOT))


def _preload_env() -> None:
    """
    在导入任何 app 模块之前，把项目根目录下的 .env 文件注入环境变量。
    仅对"尚未设置"的变量 setdefault，保证真实环境变量（如 Docker 注入的）优先级更高。
    """
    env_file = ROOT / ".env"  # .env 完整路径
    if not env_file.exists():  # 没有 .env（如生产环境用真实环境变量）则直接返回
        return
    # 逐行读取，跳过注释和空行，按第一个等号拆分键值
    for line in env_file.read_text(encoding="utf-8").splitlines():
        line = line.strip()  # 去掉首尾空白
        if not line or line.startswith("#"):  # 空行或注释行跳过
            continue
        if "=" not in line:  # 非法行跳过
            continue
        key, value = line.split("=", 1)  # 以第一个等号为界
        # setdefault：只有在环境变量尚未设置时才写入，真实环境变量优先
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


if __name__ == "__main__":
    # 第一步：必须先加载 .env（详见文件顶部说明）
    _preload_env()

    # 第二步：此时再导入 config 才能读到 .env 里的配置
    from app import config  # 配置模块
    from app import create_app  # 应用工厂

    # 第三步：创建应用并启动
    app = create_app()
    print(f"[AI-KM] 启动中：http://{config.HOST}:{config.PORT}")
    print(f"[AI-KM] 默认管理员账号 admin / Admin@123456（首次登录需改密）")
    # 监听 0.0.0.0 接受内网访问；debug 模式由 AIKM_DEBUG 控制
    app.run(host=config.HOST, port=config.PORT, debug=config.DEBUG)
