# -*- coding: utf-8 -*-
"""
生产 WSGI 入口（供 gunicorn 加载）
==================================
gunicorn 以 `wsgi:app` 形式导入本模块，因此本文件顶层就要产出一个可用的 Flask 应用实例。

关键点（与 run.py 同理）：
- 必须在「导入 app / 读取 config」之前，先把 .env 注入环境变量。
  否则 config 在模块级读取到的会是默认值，而不是 .env 里配置的真实密钥/地址。
- 本文件只负责"产出 app 对象"，不调用 app.run()（那是开发服务器做的事）。
  生产由 gunicorn 接管，提供多 worker、优雅重启、请求并发等能力。
"""

import os  # 操作系统接口，用于读写环境变量
from pathlib import Path  # 路径处理

# 项目根目录（wsgi.py 所在目录）
ROOT = Path(__file__).resolve().parent

# 复用 run.py 的 .env 预加载逻辑（run.py 顶层只定义函数，import 不会触发 app 加载，安全）
import run as _run  # 导入 run 模块
_run._preload_env()  # 先把 .env 注入环境变量

# 此时再导入并创建应用，config 才能读到 .env 里的真实配置
from app import create_app  # 应用工厂

# gunicorn 要求本模块存在一个名为 application / app 的可调用对象
app = create_app()


# ----------------------------------------------------------------------------
# 本地快速预览（可选）：直接 `python wsgi.py` 也能起服务，等价于 run.py
# 生产请改用：gunicorn -k gthread -w 1 --threads 8 -b 0.0.0.0:5200 wsgi:app
# ----------------------------------------------------------------------------
if __name__ == "__main__":
    from app import config  # 仅在直接运行时用到配置
    print(f"[AI-KM] 预览启动：http://{config.HOST}:{config.PORT}")
    app.run(host=config.HOST, port=config.PORT, debug=config.DEBUG)
