# -*- coding: utf-8 -*-
"""
Web 蓝图包
==========
把"页面路由"和"JSON API 路由"统一挂在一个蓝图（blueprint）上。
蓝图让路由与 app 实例解耦，未来可按需拆分或挂载到不同前缀。
"""

from flask import Blueprint  # Flask 蓝图

# 创建名为 "web" 的蓝图。url_prefix 留空，页面与 API 共用同一实例。
# 页面路由直接挂在根路径（/search、/ask...），API 路由统一挂在 /api 下，
# auth 装饰器正是据此区分"返回 JSON 还是 重定向登录页"。
bp = Blueprint("web", __name__)

# 导入子模块以注册其中的路由（pages 负责页面，apis 负责接口）。
# 注意：导入顺序无所谓，只要 bp 已创建即可。
from . import pages  # noqa: E402,F401  # 页面路由
from . import apis  # noqa: E402,F401  # JSON API 路由
