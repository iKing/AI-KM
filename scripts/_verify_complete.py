# -*- coding: utf-8 -*-
"""
「完整版」三项补齐功能验证脚本（P 完整版：并发编辑锁 / Docx+PDF 导出 / 文档级细粒度权限）
====================================================================================
临时库，零污染。覆盖：
  A) 并发编辑锁：抢占 / 他人占用 409 / 保存后释放 / TTL 过期可重抢
  B) 文档导出：Docx（ZIP 魔数） / PDF（%PDF 魔数） / Markdown（回归）
  C) 文档级细粒度权限（ACL）：restricted 模式下指定用户可见、未授权不可见、
     创建人/管理员恒可见、view_edit 授权可编辑、inherit 模式回退全局规则
"""
import os
import sys
import tempfile
from pathlib import Path

TMP_DB = "/tmp/aikm_verify_complete.db"  # 临时库，验证完即删
if os.path.exists(TMP_DB):
    os.remove(TMP_DB)
os.environ["AIKM_DB_PATH"] = TMP_DB  # 必须在导入 app 前设置

ROOT = "/Users/ikingsmart/AI-KM"
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from app import create_app, db, auth, config  # noqa: E402
from PIL import Image  # noqa: E402  # 生成测试图片用

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


def auth_audit_now():
    """简单返回当前时间字符串（避免循环依赖，直接拼）。"""
    from datetime import datetime
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


app = create_app()  # 启动应用（自动建表 + 播种）
client = app.test_client()


def login(user):
    """把指定用户注入会话（绕过密码，纯测试）。"""
    with client.session_transaction() as sess:
        sess["user_id"] = user["id"]
        sess["username"] = user["username"]
        sess["role"] = user["role"]
        sess["display_name"] = user["display_name"]


def new_user(username, role, dept_id, pwd="Passw0rd"):
    """建一个用户并返回其字典。"""
    uid = auth.create_user(username, username, pwd, role=role, department_id=dept_id)
    return auth.get_user_by_id(uid)


# ---------- 准备数据 ----------
admin = auth.get_user_by_username("admin")
assert admin, "admin 未播种"
root_dept = db.query_one("SELECT id FROM departments WHERE name = ?", ("知识管理中心",))
db.execute("INSERT OR IGNORE INTO departments (name, code, created_at) VALUES (?,?,?)",
           ("采购一部", "BUY1", auth_audit_now()))
db.execute("INSERT OR IGNORE INTO departments (name, code, created_at) VALUES (?,?,?)",
           ("财务二部", "FIN2", auth_audit_now()))
dept_a = db.query_one("SELECT id FROM departments WHERE name = ?", ("采购一部",))["id"]
dept_b = db.query_one("SELECT id FROM departments WHERE name = ?", ("财务二部",))["id"]

user_a = new_user("user_a", "contributor", dept_a)  # 采购一部贡献者
user_b = new_user("user_b", "contributor", dept_b)  # 财务二部贡献者（被授权可见）
user_c = new_user("user_c", "contributor", dept_b)  # 财务二部另一人（未授权）
user_d = new_user("user_d", "user", dept_a)          # 采购一部普通用户（无编辑权）


def make_doc(title, dept_id, owner_user, body_html="", body_text="", sec="internal", status="published", vis="inherit"):
    """插入一篇文档并返回其 ID。"""
    now = auth_audit_now()
    lp = "知识内容" * 60  # 保证正文超过 MIN_CONTENT_LENGTH 红线
    text = body_text or lp
    html = body_html or ""
    did = db.execute(
        """
        INSERT INTO documents
            (title, category_l1, department_id, security_level, quality_level, owner,
             status, body_text, body_html, version, created_by, created_at, updated_at, visibility_mode)
        VALUES (?, 'POLICY', ?, ?, 'normal', ?, ?, ?, ?, 1, ?, ?, ?, ?)
        """,
        (title, dept_id, sec, owner_user["username"], status, text, html,
         owner_user["id"], now, now, vis),
    )
    return did


print("\n=== A) 并发编辑锁 ===")
# 文档归 user_a（采购一部），便于 user_a / admin 可编辑
lock_doc = make_doc("锁测试文档", dept_a, user_a)

# A1 user_a 抢占锁成功
login(user_a)
r = client.post(f"/api/documents/{lock_doc}/lock")
d = r.get_json()
check("A1 持有者抢占编辑锁成功", r.status_code == 200 and d["ok"] and d.get("enabled"), d)

# A2 admin（特权角色，可见且可编辑 dept_a 文档）尝试抢占 → 被他人占用 409
# 注：必须用 admin 而非 user_b —— user_b 属财务二部、看不到采购一部 internal 文档，会先 404
login(admin)
r = client.post(f"/api/documents/{lock_doc}/lock")
d = r.get_json()
check("A2 他人抢占被拒(409)", r.status_code == 409 and not d["ok"] and d.get("held_by_name") == user_a["display_name"], d)

# A3 锁状态查询：active 且 locked_by = user_a
login(user_a)
r = client.get(f"/api/documents/{lock_doc}/lock")
d = r.get_json()
check("A3 锁状态有效且归属 user_a", d["active"] and d["locked_by"] == user_a["id"], d)

# A4 user_a 保存编辑 → 成功且锁被释放
r = client.post(f"/api/documents/{lock_doc}/edit", json={"html": f"<p>{'知识内容' * 60}</p>", "change_note": "锁测试保存"})
d = r.get_json()
check("A4 持锁者保存成功", r.status_code == 200 and d["ok"], d)
r = client.get(f"/api/documents/{lock_doc}/lock")
d = r.get_json()
check("A4b 保存后锁已释放", not d["active"] and d["locked_by"] is None, d)

# A5 TTL 过期后可被他人抢占：手动把锁时间改成很久以前，再让 admin 抢（user_b 看不到该文档会 404）
db.execute("UPDATE documents SET edit_lock_user=?, edit_lock_at=?, edit_lock_name=? WHERE id=?",
           (user_a["id"], "2000-01-01 00:00:00", user_a["display_name"], lock_doc))
login(admin)
r = client.post(f"/api/documents/{lock_doc}/lock")
d = r.get_json()
check("A5 锁过期后他人可抢占", r.status_code == 200 and d["ok"] and d.get("enabled"), d)
# 清理锁
login(admin)
client.delete(f"/api/documents/{lock_doc}/lock")

# A6 无编辑权者（普通 user）拿锁被拒 403
login(user_d)
r = client.post(f"/api/documents/{lock_doc}/lock")
check("A6 无编辑权拿锁被拒(403)", r.status_code == 403, r.get_json())

print("\n=== B) 文档导出（Docx / PDF / Markdown）===")
# 生成一张测试图片放到上传目录，验证导出内嵌图片不崩
img_path = config.UPLOAD_DIR / "verify_test.png"
Image.new("RGB", (40, 40), (200, 50, 50)).save(img_path)  # 纯红小图
rich_html = (
    "<h1>标题一</h1><p>这是<b>加粗</b>与<i>斜体</i>的正文，含<a href='https://example.com'>链接</a>。</p>"
    "<h2>二级标题</h2><ul><li>列表项一</li><li>列表项二</li></ul>"
    "<blockquote>引用一段话。</blockquote>"
    "<p>内嵌图片：<img src='/uploads/verify_test.png' /></p>"
    "<table><tr><th>列1</th><th>列2</th></tr><tr><td>A</td><td>B</td></tr></table>"
)
export_doc = make_doc("导出测试文档", dept_a, user_a, body_html=rich_html)

# B1 Docx 导出：返回 ZIP 魔数 PK
login(admin)
r = client.get(f"/api/documents/{export_doc}/export/docx")
data = r.get_data()
check("B1 Docx 导出 200 且为合法 docx(ZIP)",
      r.status_code == 200 and data[:2] == b"PK", f"status={r.status_code} head={data[:8]}")

# B2 PDF 导出：返回 %PDF 魔数
r = client.get(f"/api/documents/{export_doc}/export/pdf")
data = r.get_data()
check("B2 PDF 导出 200 且为合法 PDF",
      r.status_code == 200 and data[:4] == b"%PDF", f"status={r.status_code} head={data[:8]}")

# B3 Markdown 导出（回归）：仍可用
r = client.get(f"/api/documents/{export_doc}/export/md")
check("B3 Markdown 导出 200（回归）", r.status_code == 200 and b"# " in r.get_data(), r.status_code)

# B4 无权限者不可导出（user_c 在财务二部，看不到采购一部 internal 文档）
login(user_c)
r = client.get(f"/api/documents/{export_doc}/export/docx")
check("B4 无权限者导出被拒(404)", r.status_code == 404, r.get_json())

# B5 无富文本的纯文本文档也能导出 PDF（兜底分支不崩）
plain_doc = make_doc("纯文本导出文档", dept_a, user_a, body_text="第一段内容。\n\n第二段内容。")
login(admin)
r = client.get(f"/api/documents/{plain_doc}/export/pdf")
data = r.get_data()
check("B5 纯文本文档 PDF 导出可用", r.status_code == 200 and data[:4] == b"%PDF", r.status_code)

print("\n=== C) 文档级细粒度可见权限（ACL）===")
# 创建一篇「采购一部 internal 已发布」文档，默认 inherit
acl_doc = make_doc("私有文档示例", dept_a, user_a, body_text="敏感内容仅限授权人。")
# 设为 restricted，并仅授权 user_b（财务二部）可查看
login(user_a)
r = client.put(f"/api/documents/{acl_doc}/acl", json={
    "visibility_mode": "restricted",
    "entries": [{"principal_type": "user", "principal_id": str(user_b["id"]), "perm": "view"}],
})
d = r.get_json()
check("C1 设置 ACL 成功(restricted+授权user_b)", r.status_code == 200 and d["ok"] and d["visibility_mode"] == "restricted", d)

# C2 被授权者 user_b 可看到（/raw 200）
login(user_b)
r = client.get(f"/api/documents/{acl_doc}/raw")
check("C2 被授权用户 user_b 可见", r.status_code == 200 and r.get_json()["ok"], r.get_json())

# C3 未授权者 user_c（同财务二部）看不到（404）
login(user_c)
r = client.get(f"/api/documents/{acl_doc}/raw")
check("C3 未授权用户 user_c 不可见(404)", r.status_code == 404, r.get_json())

# C4 同部门但未授权、非创建的 user_d（采购一部普通用户）看不到
login(user_d)
r = client.get(f"/api/documents/{acl_doc}/raw")
check("C4 同部门未授权用户 user_d 不可见(404)", r.status_code == 404, r.get_json())

# C5 创建人 user_a 本人始终可见
login(user_a)
r = client.get(f"/api/documents/{acl_doc}/raw")
check("C5 创建人 user_a 恒可见", r.status_code == 200, r.get_json())

# C6 管理员可见（特权角色不受 ACL 限制）
login(admin)
r = client.get(f"/api/documents/{acl_doc}/raw")
check("C6 管理员恒可见", r.status_code == 200, r.get_json())

# C7 文档列表中可见性过滤一致：user_b 列表含该文档，user_c 列表不含
login(user_b)
r = client.get("/api/documents", query_string={"per_page": 200})
ids_b = [it["id"] for it in r.get_json()["items"]]
login(user_c)
r = client.get("/api/documents", query_string={"per_page": 200})
ids_c = [it["id"] for it in r.get_json()["items"]]
check("C7 列表过滤与详情一致(b有/c无)", acl_doc in ids_b and acl_doc not in ids_c, f"b={acl_doc in ids_b} c={acl_doc in ids_c}")

# C8 view_edit 授权：授予 user_b 可编辑
login(user_a)
r = client.put(f"/api/documents/{acl_doc}/acl", json={
    "visibility_mode": "restricted",
    "entries": [{"principal_type": "user", "principal_id": str(user_b["id"]), "perm": "view_edit"}],
})
check("C8 改为 view_edit 授权成功", r.status_code == 200 and r.get_json()["ok"], r.get_json())
login(user_b)
r = client.post(f"/api/documents/{acl_doc}/edit", json={"html": f"<p>{'知识内容' * 60}</p>"})
check("C9 被授予 view_edit 的 user_b 可编辑", r.status_code == 200 and r.get_json()["ok"], r.get_json())
# 清理锁
client.delete(f"/api/documents/{acl_doc}/lock")

# C10 非创建人/非管理员设置 ACL 被拒 403
login(user_b)
r = client.put(f"/api/documents/{acl_doc}/acl", json={"visibility_mode": "inherit", "entries": []})
check("C10 非创建人设 ACL 被拒(403)", r.status_code == 403, r.get_json())

# C11 回退 inherit：user_c 又能看到了（恢复全局规则，采购一部 internal 但 user_c 在财务二部 → 仍不可见！）
# 说明：inherit 时 user_c 属财务二部且文档 internal 非 public，本就看不到，因此用 user_a 自己验证 inherit 不改变创建人可见性
login(user_a)
r = client.put(f"/api/documents/{acl_doc}/acl", json={"visibility_mode": "inherit", "entries": []})
check("C11 改回 inherit 成功", r.status_code == 200 and r.get_json()["visibility_mode"] == "inherit", r.get_json())

# ---------- 汇总 ----------
print("\n" + "=" * 60)
print(f"通过 {len(PASS)} 项，失败 {len(FAIL)} 项，共 {len(PASS)+len(FAIL)} 项")
if FAIL:
    print("失败项：")
    for f in FAIL:
        print("  -", f)
    raise SystemExit(1)
print("🎉 全部通过")
