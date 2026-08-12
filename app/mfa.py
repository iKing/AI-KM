# -*- coding: utf-8 -*-
"""
MFA 多因子认证模块（P2 安全增强）
================================
实现基于 TOTP（RFC 6238）的二次验证：
- 开启时为用户生成 Base32 密钥，并输出 otpauth:// 配置 URI 与二维码（用户用 authenticator App 扫码）
- 登录时在校验密码之后，再要求输入 6 位动态码完成二次验证
- 提供一次性「备用码」，手机丢失时仍可紧急登录

依赖：pyotp（TOTP 算法）、qrcode（生成二维码图片）。两者已安装到运行环境。
密钥仅以 Base32 明文存库（落在 users.mfa_secret），因为 TOTP 本身依赖服务端持有密钥做校验，
这是标准做法；库文件权限与传输全程 HTTPS 来兜底。
"""

import base64  # Base32 标准化处理用到
import json  # 备用码以 JSON 数组存储
import secrets  # 密码学安全随机数，生成备用码

import pyotp  # TOTP 算法实现
import qrcode  # 二维码生成
import qrcode.image.svg  # SVG 格式二维码，便于前端直接内联展示，不落地文件
from io import BytesIO  # 内存字节缓冲，用于把二维码图转成字节

from . import config, db, audit  # 项目内部模块


def generate_secret() -> str:
    """
    为用户生成一个新的 TOTP 密钥（Base32 字符串）。

    返回：
        随机 Base32 密钥，如 "JBSWY3DPEHPK3PXP"
    """
    # pyotp.random_base32 内部用 secrets 生成，符合密码学安全要求
    return pyotp.random_base32()


def provisioning_uri(secret: str, username: str) -> str:
    """
    生成 otpauth:// URI，供 authenticator App 扫码识别（含发行方、账号、密钥）。

    参数：
        secret:   TOTP 密钥（Base32）
        username: 用户登录名，作为 URI 里的账号标识
    返回：
        otpauth://totp/AI-KM:username?secret=...&issuer=AI-KM
    """
    # TOTP(secret).provisioning_uri 会拼出标准化的 otpauth URI
    return pyotp.TOTP(secret).provisioning_uri(name=username, issuer_name=config.MFA_ISSUER)


def qr_svg(secret: str, username: str) -> str:
    """
    生成二维码的 SVG 字符串，前端可直接内联到页面（无需额外图片文件）。

    参数：
        secret:   TOTP 密钥
        username: 登录名
    返回：
        <svg ...>...</svg> 形式的 XML 字符串
    """
    uri = provisioning_uri(secret, username)  # 先拿到 otpauth URI
    # 用 SVG 工厂生成二维码，SVG 是矢量、无色盲问题、可直接塞进 HTML
    img = qrcode.make(uri, image_factory=qrcode.image.svg.SvgImage)
    buf = BytesIO()  # 内存缓冲
    img.save(buf)  # 写入缓冲
    return buf.getvalue().decode("utf-8")  # 转成字符串返回


def verify_code(secret: str, code: str) -> bool:
    """
    校验用户输入的 6 位动态码是否正确。

    参数：
        secret: 用户存储的 TOTP 密钥
        code:   用户输入的动态码（可能带空格，这里先清洗）
    返回：
        True/False
    """
    if not secret or not code:  # 缺任意一个直接失败
        return False
    code = code.strip().replace(" ", "")  # 去掉用户可能输入的空格
    # valid_totp 允许前后一个时间窗口的偏差（默认 ±1，即 ±30 秒），容忍时钟漂移
    return pyotp.TOTP(secret).verify(code, valid_window=1)


def generate_backup_codes(count: int = None) -> list[str]:
    """
    生成一次性备用码列表。

    备用码是普通随机串，用户抄下来保存；登录时若手机不在身边，可输入其中一个完成二次验证，
    用过的备用码会立即作废（从库里移除）。

    参数：
        count: 生成数量，不传用配置默认
    返回：
        备用码字符串列表
    """
    count = count or config.MFA_BACKUP_COUNT  # 用默认值兜底
    codes = []  # 结果列表
    alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"  # 去掉易混淆字符
    for _ in range(count):  # 循环生成指定数量
        # 8 位分两组、中间加 "-"，方便用户抄写核对，如 "AB12-CD34"
        part1 = "".join(secrets.choice(alphabet) for _ in range(4))
        part2 = "".join(secrets.choice(alphabet) for _ in range(4))
        codes.append(f"{part1}-{part2}")
    return codes


def enable_mfa(user_id: int, secret: str, backup_codes: list[str]) -> None:
    """
    开启用户的 MFA：写入密钥、备用码，并置 mfa_enabled=1。

    参数：
        user_id:     用户 ID
        secret:      已确认可用的 TOTP 密钥
        backup_codes: 生成的备用码列表
    """
    db.execute(
        "UPDATE users SET mfa_secret=?, mfa_backup=?, mfa_enabled=1 WHERE id=?",
        (secret, json.dumps(backup_codes), user_id),  # 备用码转 JSON 字符串存储
    )
    audit.log("mfa_enable", user_id=user_id, ip=_client_ip_stub(),
               detail={"动作": "开启 MFA"}, result="success")


def disable_mfa(user_id: int) -> None:
    """关闭用户的 MFA：清空密钥与备用码，置 mfa_enabled=0。"""
    db.execute(
        "UPDATE users SET mfa_secret=NULL, mfa_backup=NULL, mfa_enabled=0 WHERE id=?",
        (user_id,),
    )
    audit.log("mfa_enable", user_id=user_id, ip=_client_ip_stub(),
               detail={"动作": "关闭 MFA"}, result="success")


def is_mfa_enabled(user_id: int) -> bool:
    """查询用户是否已开启 MFA。"""
    row = db.query_one("SELECT mfa_enabled FROM users WHERE id=?", (user_id,))
    return bool(row and row["mfa_enabled"])


def consume_backup_code(user_id: int, code: str) -> bool:
    """
    尝试用备用码完成二次验证，成功则作废该码（从列表移除）。

    参数：
        user_id: 用户 ID
        code:    用户输入的备用码
    返回：
        True 表示备用码有效并已消费
    """
    row = db.query_one("SELECT mfa_backup FROM users WHERE id=?", (user_id,))
    if not row or not row["mfa_backup"]:  # 没有备用码
        return False
    codes = json.loads(row["mfa_backup"] or "[]")  # 解析备用码列表
    norm = code.strip().upper()  # 归一化（不区分大小写、去空格）
    if norm not in codes:  # 不在列表里，失败
        return False
    codes.remove(norm)  # 用过的立即移除
    db.execute("UPDATE users SET mfa_backup=? WHERE id=?", (json.dumps(codes), user_id))
    return True


def _client_ip_stub() -> str:
    """
    模块内获取客户端 IP 的兜底实现。

    说明：本模块不直接依赖 Flask 请求上下文（便于被脚本/测试调用），
    所以这里尝试从 flask request 取，取不到就返回 "system"。
    """
    try:
        from flask import request  # 延迟导入，避免非 Web 场景下的循环依赖
        return request.headers.get("X-Forwarded-For", request.remote_addr or "system").split(",")[0].strip()
    except Exception:
        return "system"  # 非请求上下文（如脚本）统一记 system
