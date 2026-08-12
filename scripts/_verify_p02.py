# -*- coding: utf-8 -*-
"""
P2 验证脚本：MFA / SSO / 标准 REST API / diagrams.net（临时库，零污染）
===========================================================
覆盖链路：
  1) MFA(TOTP)：setup 生成密钥+二维码 → confirm 用动态码开启 → 登录需二次验证 → 备用码可用 → 关闭
  2) REST API v1：API Key 鉴权 + 健康检查/文档列表/详情/检索/知识树/标签/评论/openapi 规范
  3) diagrams.net：新建图 → 列表 → 更新 → 删除；文档页展示图链接
  4) SSO 门控：默认关闭时 /api/login/ldap 与 /api/sso/oidc/authorize 返回明确拒绝；ldap 模块缺依赖不崩
"""
import os
import sys
import json
import tempfile

TMP_DB = "/tmp/aikm_verify_p02.db"  # 临时库，验证完即删
if os.path.exists(TMP_DB):
    os.remove(TMP_DB)
os.environ["AIKM_DB_PATH"] = TMP_DB  # 必须在导入 app 前设置

ROOT = "/Users/ikingsmart/AI-KM"
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import pyotp  # TOTP 算法，用于生成正确的动态码做端到端校验
from app import create_app, db, auth, ingest, sso as sso_mod  # noqa: E402

PASS = []  # 通过的断言
FAIL = []  # 失败的断言


def check(name, cond, extra=""):
    """统一的断言记录：通过记 PASS，失败记 FAIL 并打印。"""
    if cond:
        PASS.append(name)
        print(f"  ✅ {name}")
    else:
        FAIL.append(name)
        print(f"  ❌ {name}  {extra}")


app = create_app()  # 启动应用（自动建表 + 播种）
client = app.test_client()        # 会话客户端（带 admin 登录态）
client2 = app.test_client()       # 无会话客户端（纯 API Key 测试用）

# 取 seeded admin 与根部门
admin = auth.get_user_by_username("admin")
assert admin, "admin 未播种"
dept = db.query_one("SELECT id FROM departments WHERE name = ?", ("知识管理中心",))
dept_id = dept["id"]

# 用 session 直接注入登录态（绕过密码，纯测试）
with client.session_transaction() as sess:
    sess["user_id"] = admin["id"]
    sess["username"] = "admin"
    sess["role"] = "admin"
    sess["display_name"] = "系统管理员"


def fresh_login_session():
    """清空会话，模拟「未登录」状态（用于测试登录二次验证流程）。"""
    with client.session_transaction() as sess:
        sess.clear()


print("\n=== 1) MFA(TOTP) 多因子认证 ===")
# 1.1 状态初始为未开启
r = client.get("/api/mfa/status")
check("MFA 初始未开启", r.get_json().get("enabled") is False, str(r.get_json()))

# 1.2 开通第一步：拿到密钥 + 二维码 + otpauth URI
r = client.post("/api/mfa/setup")
j = r.get_json()
check("setup 返回 ok", j.get("ok"), str(j)[:200])
check("返回 TOTP 密钥", bool(j.get("secret")), "无 secret")
check("返回二维码 SVG", "<svg" in (j.get("qr_svg") or ""), "无二维码")
check("返回 otpauth URI", (j.get("otpauth_uri") or "").startswith("otpauth://totp/"), "无 URI")
secret = j["secret"]

# 1.3 开通第二步：用正确的动态码确认
code = pyotp.TOTP(secret).now()  # 生成当前时刻正确的动态码
r = client.post("/api/mfa/confirm", json={"code": code})
j = r.get_json()
check("confirm 用正确动态码开启成功", j.get("ok"), str(j)[:200])
check("返回备用码", len(j.get("backup_codes") or []) > 0, "无备用码")
backup_codes = j.get("backup_codes")

# 1.4 状态已开启，且密钥已落库
r = client.get("/api/mfa/status")
check("MFA 状态变为已开启", r.get_json().get("enabled") is True, str(r.get_json()))
row = db.query_one("SELECT mfa_enabled, mfa_secret FROM users WHERE id=?", (admin["id"],))
check("mfa_enabled 已落库", bool(row["mfa_enabled"]), "未置位")
check("mfa_secret 已落库", bool(row["mfa_secret"]), "无密钥")

# 1.5 登录需二次验证：密码正确后返回 mfa_required，且不发会话
fresh_login_session()
r = client.post("/api/login", json={"username": "admin", "password": "Admin@123456"})
j = r.get_json()
check("登录返回 mfa_required", j.get("mfa_required") is True, str(j))
check("MFA 下不直发会话", "user" not in j or j.get("user") is None, str(j))

# 1.6 用正确动态码完成二次验证 → 登录成功
r = client.post("/api/login/mfa", json={"code": pyotp.TOTP(secret).now()})
check("二次验证通过并登录", r.get_json().get("ok"), str(r.get_json())[:200])

# 1.7 错误动态码被拒
fresh_login_session()
client.post("/api/login", json={"username": "admin", "password": "Admin@123456"})  # 先过密码
r = client.post("/api/login/mfa", json={"code": "000000"})
check("错误动态码被拒(401)", r.status_code == 401, f"status={r.status_code}")

# 1.8 备用码可紧急登录：用其中一个备用码
fresh_login_session()
client.post("/api/login", json={"username": "admin", "password": "Admin@123456"})
bc = backup_codes[0]  # 取一个备用码
r = client.post("/api/login/mfa", json={"code": bc})
check("备用码登录成功", r.get_json().get("ok"), str(r.get_json())[:200])
# 用过的备用码失效（数量减少）
r = client.get("/api/mfa/status")
check("备用码用后减少", r.get_json().get("backup_count") == len(backup_codes) - 1, str(r.get_json()))

# 1.9 关闭 MFA（需密码）
r = client.post("/api/mfa/disable", json={"password": "Admin@123456"})
check("关闭 MFA 成功", r.get_json().get("ok"), str(r.get_json())[:200])
check("关闭后 mfa_enabled=0", db.query_one("SELECT mfa_enabled FROM users WHERE id=?", (admin["id"],))["mfa_enabled"] == 0)
fresh_login_session()

print("\n=== 2) 标准 REST API v1（API Key 鉴权）===")
# MFA 段落结束时清掉了会话，这里重新以 admin 登录（MFA 已关闭，密码直登即可）
with client.session_transaction() as sess:
    sess["user_id"] = admin["id"]
    sess["username"] = "admin"
    sess["role"] = "admin"
    sess["display_name"] = "系统管理员"

# 2.0 创建一个 API Key（admin）
r = client.post("/api/admin/apikeys", json={"name": "verify-key"})
api_key = r.get_json().get("raw_key")
check("创建 API Key", bool(api_key), str(r.get_json())[:200])
H = {"Authorization": f"Bearer {api_key}"}  # Bearer 头

# 2.1 健康检查端点刻意开放（供负载均衡/容器编排/外部监控直接探活，不泄露业务数据）
r = client2.get("/api/v1/health")
check("无 Key 访问 /health 开放(200)", r.status_code == 200 and r.get_json().get("ok"), f"status={r.status_code}")
# 2.1b 其余 v1 资源接口无 Key 仍被拒（鉴权未放松）
r = client2.get("/api/v1/documents")
check("无 Key 访问 /documents 被拒(401)", r.status_code == 401, f"status={r.status_code}")

# 2.2 健康检查（带 Key 同样可用）
r = client2.get("/api/v1/health", headers=H)
check("v1 健康检查 ok", r.get_json().get("ok") and r.get_json()["data"]["status"] == "ok", str(r.get_json())[:200])

# 先入库一篇文档，保证列表/详情/检索/评论有数据
body = """# REST API 测试文档

本文用于验证标准 REST API v1 的文档列表与详情接口。接口应采用资源式设计，统一响应信封，并支持 API Key 鉴权，便于第三方系统稳定对接。

## 1.1 设计原则

统一前缀 /api/v1，列表接口支持分页，所有文档查询复用权限过滤逻辑，保证无权限数据在数据层就被排除，不会出现越权泄露。

## 2.1 鉴权方式

同时支持浏览器会话与 Bearer API Key 两种方式。程序调用场景使用 API Key，密钥在管理后台创建，明文只在创建时返回一次，库里只存哈希。

## 3.1 响应信封

所有接口返回统一信封，列表接口附带 page 分页元数据，便于前端做翻页与总量展示，错误时携带 error 字段说明原因。
"""
tmp = tempfile.NamedTemporaryFile(suffix=".md", delete=False)
tmp.write(body.encode("utf-8")); tmp.close()
res = ingest.ingest_file(tmp.name, {"title": "REST API 测试文档", "category_l1": "PRODUCT",
    "department_id": dept_id, "security_level": "internal", "owner": "测试"}, user_id=admin["id"], auto_publish=True)
check("入库测试文档成功", res["ok"], str(res)[:200])  # 标题合格、正文超 200 字，应当成功
doc_id = res["doc_id"]

# 2.3 文档列表（含分页信封）
r = client2.get("/api/v1/documents?per_page=5", headers=H)
j = r.get_json()
check("v1 文档列表 ok", j.get("ok") and "page" in j, str(j)[:150])
check("列表含分页元数据", j["page"]["total"] >= 1 and j["page"]["page"] == 1, str(j.get("page")))

# 2.4 文档详情
r = client2.get(f"/api/v1/documents/{doc_id}", headers=H)
j = r.get_json()
check("v1 文档详情 ok", j.get("ok") and j["data"]["document"]["id"] == doc_id, str(j)[:150])
check("详情含标签/引用/切片", "tags" in j["data"] and "chunks" in j["data"], str(list(j["data"].keys())))

# 2.5 检索
r = client2.get("/api/v1/search?q=REST API", headers=H)
check("v1 检索 ok", r.get_json().get("ok"), str(r.get_json())[:150])
check("检索返回结果", isinstance(r.get_json().get("data"), list), "结果非列表")

# 2.6 知识树 / 标签
check("v1 知识树 ok", client2.get("/api/v1/spaces", headers=H).get_json().get("ok"))
check("v1 标签 ok", client2.get("/api/v1/tags", headers=H).get_json().get("ok"))

# 2.7 评论（先发一条）
client.post(f"/api/documents/{doc_id}/comments", json={"content": "v1 评论测试"})
r = client2.get(f"/api/v1/comments?doc_id={doc_id}", headers=H)
check("v1 评论列表含刚发评论", r.get_json().get("ok") and any(c["content"] == "v1 评论测试" for c in r.get_json()["data"]), str(r.get_json())[:200])

# 2.8 OpenAPI 规范
r = client2.get("/api/v1/openapi.json", headers=H)
spec = r.get_json()
check("openapi 规范 ok", spec.get("openapi", "").startswith("3."), str(spec)[:80])
check("规范含 paths", isinstance(spec.get("paths"), dict) and len(spec["paths"]) >= 8, "paths 过少")
# 文档页可渲染（在线 Swagger UI）
r = client.get("/api/v1/docs")
check("API 文档页 200", r.status_code == 200, f"status={r.status_code}")

print("\n=== 3) diagrams.net 内联画图 ===")
# 3.1 新建图（XML 为人造 diagrams.net mxGraphModel）
xml = '<mxGraphModel><root><mxCell id="0"/><mxCell id="1" parent="0"/><mxCell id="2" value="采购流程" style="rounded" vertex="1" parent="1"><mxGeometry x="40" y="40" width="120" height="40" as="geometry"/></mxCell></root></mxGraphModel>'
r = client.post(f"/api/documents/{doc_id}/diagrams", json={"title": "流程图A", "diagram_xml": xml})
j = r.get_json()
check("新建图 ok", j.get("ok"), str(j)[:200])
did = j.get("diagram_id")
check("图已落库", bool(db.query_one("SELECT id FROM doc_diagrams WHERE id=?", (did,))), "未落库")

# 3.2 列表
r = client.get(f"/api/documents/{doc_id}/diagrams")
check("图列表含刚建的图", r.get_json().get("ok") and any(g["id"] == did for g in r.get_json()["diagrams"]), str(r.get_json())[:150])

# 3.3 更新图内容
xml2 = xml.replace("采购流程", "修订后流程")
r = client.put(f"/api/documents/{doc_id}/diagrams/{did}", json={"title": "流程图A", "diagram_xml": xml2})
check("更新图 ok", r.get_json().get("ok"), str(r.get_json())[:200])
check("图内容已更新", "修订后流程" in db.query_one("SELECT diagram_xml FROM doc_diagrams WHERE id=?", (did,))["diagram_xml"], "未更新")

# 3.4 文档页展示图链接
r = client.get(f"/doc/{doc_id}")
check("文档页含内嵌图区", "内嵌图" in r.get_data(as_text=True), "未渲染图区")

# 3.5 删除图
r = client.delete(f"/api/documents/{doc_id}/diagrams/{did}")
check("删除图 ok", r.get_json().get("ok"), str(r.get_json())[:200])
check("图已移除", not db.query_one("SELECT id FROM doc_diagrams WHERE id=?", (did,)), "未移除")

# 3.6 画图页可渲染
r = client.get(f"/doc/{doc_id}/draw")
check("画图页 200", r.status_code == 200, f"status={r.status_code}")

print("\n=== 4) SSO 门控与容错 ===")
# 4.1 默认 SSO 关闭：ldap/oidc 入口明确拒绝（不崩）
r = client.post("/api/login/ldap", json={"username": "x", "password": "y"})
check("SSO 关闭时 LDAP 登录被拒(403)", r.status_code == 403, f"status={r.status_code}")
r = client.get("/api/sso/oidc/authorize")
check("SSO 关闭时 OIDC 授权被拒(403)", r.status_code == 403, f"status={r.status_code}")

# 4.2 ldap 模块在缺失 ldap3 时优雅报错（不抛异常、不崩应用）
user, err = sso_mod.authenticate_ldap("someone", "pw")
check("ldap 缺依赖返回错误而非崩溃", user is None and bool(err), f"user={user}, err={err}")

# 4.3 配置门控：关闭时 sso_enabled() 为 False
check("sso_enabled() 反映开关", sso_mod.sso_enabled() is False, "应为 False")

print("\n=== 5) 清理临时库 ===")
try:
    os.remove(TMP_DB)
    print("  🧹 临时库已删除")
except Exception as e:
    print(f"  ⚠️ 清理失败：{e}")

print(f"\n===== 结果：通过 {len(PASS)} 项，失败 {len(FAIL)} 项 =====")
if FAIL:
    print("失败项：", FAIL)
    sys.exit(1)
print("全部通过 ✅")
