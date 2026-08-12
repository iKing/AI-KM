# -*- coding: utf-8 -*-
"""
P1 补充验证脚本：导出 / 评论 / 审计页（临时库，零污染）
===========================================================
覆盖链路（对齐「我都要」路线图的 #19 导出、#20 评论、#21 审计页）：
  1) 导出 Markdown：接口返回正文权威源 + 元数据头 + 下载头
  2) 打印/PDF 页：纯净页渲染正文并触发 window.print
  3) 页面级评论：
     - 管理员发表 / 读取 / 空内容拒绝
     - 普通登录用户亦可发表（任何可见文档可评论）
     - 非作者且非管理员删除被拒(403)
     - 作者/管理员删除成功并落库
  4) 审计页：
     - 管理员可访问 /audit 页，非管理员 403
     - /api/admin/audit 返回含 doc_comment 动作的日志
     - /api/admin/audit/export 返回 CSV
"""
import os
import sys
import tempfile

TMP_DB = "/tmp/aikm_verify_p01_extra.db"  # 临时库，验证完即删
if os.path.exists(TMP_DB):
    os.remove(TMP_DB)
os.environ["AIKM_DB_PATH"] = TMP_DB  # 必须在导入 app 前设置，隔离真实库

ROOT = "/Users/ikingsmart/AI-KM"
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from app import create_app, db, auth, ingest  # noqa: E402

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
client = app.test_client()

# 取 seeded admin 与根部门
admin = auth.get_user_by_username("admin")
assert admin, "admin 未播种"
dept = db.query_one("SELECT id FROM departments WHERE name = ?", ("知识管理中心",))
dept_id = dept["id"]


def login_as(uid, username, role, name):
    """在测试客户端会话中注入指定用户的登录态（纯测试，绕过密码）。"""
    with client.session_transaction() as sess:
        sess["user_id"] = uid
        sess["username"] = username
        sess["role"] = role
        sess["display_name"] = name


login_as(admin["id"], "admin", "admin", "系统管理员")

print("\n=== 1) 入库一篇足够长的文档（auto_publish 直接发布，便于 reader 可见）===")
body = """# 采购管理总则

本办法规范公司医疗器械采购全流程，明确各部门职责与操作要求。采购活动应当遵循公开、公平、公正与效益原则，确保采购质量与资金安全。本办法适用于全体采购从业人员，以及参与采购评审、验收、结算等环节的相关岗位人员。

## 1.1 适用范围

凡涉及公司资金支出的器械采购行为，均须遵照本办法执行。任何部门不得以化整为零方式规避集中采购程序。

## 2.1 采购流程

采购需求提出后，应按制度完成资格性审查、综合评审与下单。评审过程应留痕，结果报分管领导审批后方可执行。

## 2.2 监督

采购活动应接受监督，违规按规处理。建立供应商黑名单与履约评价机制，持续优化采购生态。
"""
tmp = tempfile.NamedTemporaryFile(suffix=".md", delete=False)
tmp.write(body.encode("utf-8"))
tmp.close()
res = ingest.ingest_file(
    tmp.name,
    {"title": "采购管理总则", "category_l1": "POLICY", "department_id": dept_id,
     "security_level": "internal", "owner": "张三", "effective_date": "2025-01-01", "source": "测试局"},
    user_id=admin["id"],
    auto_publish=True,  # 直接发布，便于普通用户可见并评论
)
doc_id = res["doc_id"]
check("文档入库成功", res["ok"], str(res))
check("文档状态=published", db.query_one("SELECT status FROM documents WHERE id=?", (doc_id,))["status"] == "published")
check("正文已存 body_text（权威源）", bool(db.query_one("SELECT body_text FROM documents WHERE id=?", (doc_id,))["body_text"]))

print("\n=== 2) 导出 Markdown ===")
r = client.get(f"/api/documents/{doc_id}/export/md")
check("导出接口 200", r.status_code == 200, f"status={r.status_code}")
ct = r.headers.get("Content-Type", "")
check("Content-Type 为 markdown", ct.startswith("text/markdown"), ct)
cd = r.headers.get("Content-Disposition", "")
check("带附件下载头", "attachment" in cd and ".md" in cd, cd)
md_text = r.get_data(as_text=True)
check("导出含标题", "# 采购管理总则" in md_text, "标题缺失")
check("导出含元数据(密级)", "密级" in md_text, "元数据缺失")
check("导出含正文片段", "全流程" in md_text and "采购流程" in md_text, "正文缺失")

print("\n=== 3) 打印 / PDF 页 ===")
r = client.get(f"/doc/{doc_id}/print")
check("打印页 200", r.status_code == 200, f"status={r.status_code}")
pp = r.get_data(as_text=True)
check("打印页触发 window.print", "window.print" in pp, "无 print 调用")
check("打印页渲染正文", "采购流程" in pp, "正文未渲染")

print("\n=== 4) 页面级评论 ===")
# 4.1 管理员发表评论
r = client.post(f"/api/documents/{doc_id}/comments", json={"content": "这是管理员的第一条评论"})
j = r.get_json()
check("管理员发表评论 ok", j.get("ok"), str(j)[:200])
check("返回 comment_id", j.get("comment_id"), "无 comment_id")
admin_cid = j["comment_id"]
check("评论已落库", bool(db.query_one("SELECT id FROM doc_comments WHERE id=?", (admin_cid,))), "未落库")

# 4.2 读取评论列表
r = client.get(f"/api/documents/{doc_id}/comments")
comments = r.get_json().get("comments", [])
check("评论列表含 1 条", len(comments) == 1, f"实际 {len(comments)}")
check("评论含作者名", comments and comments[0]["author"] == "系统管理员", str(comments))
check("评论含正文", comments and comments[0]["content"] == "这是管理员的第一条评论", str(comments))

# 4.3 空内容拒绝
r = client.post(f"/api/documents/{doc_id}/comments", json={"content": "   "})
check("空评论被拒(400)", r.status_code == 400, f"status={r.status_code}")

# 4.4 普通登录用户亦可评论（任何可见文档可评论）
uid = auth.create_user(username="reader3", display_name="只读3", password="X@123456",
                       role="user", department_id=dept_id, must_change_pwd=False)
login_as(uid, "reader3", "user", "只读3")
r = client.post(f"/api/documents/{doc_id}/comments", json={"content": "普通用户也能评论"})
j = r.get_json()
check("普通用户发表评论 ok", j.get("ok"), str(j)[:200])
reader_cid = j.get("comment_id")
check("普通用户评论已落库", bool(db.query_one("SELECT id FROM doc_comments WHERE id=?", (reader_cid,))), "未落库")

# 4.5 非作者且非管理员删除被拒(403)
r = client.delete(f"/api/documents/{doc_id}/comments/{admin_cid}")
check("非作者删他人评论被拒(403)", r.status_code == 403, f"status={r.status_code}")
check("被拒后评论仍在", bool(db.query_one("SELECT id FROM doc_comments WHERE id=?", (admin_cid,))), "误删")

# 4.6 管理员删除成功
login_as(admin["id"], "admin", "admin", "系统管理员")
r = client.delete(f"/api/documents/{doc_id}/comments/{reader_cid}")
check("管理员删除评论 ok", r.get_json().get("ok"), str(r.get_json())[:200])
check("被删评论已移除", not db.query_one("SELECT id FROM doc_comments WHERE id=?", (reader_cid,)), "未移除")

# 4.7 评论区在文档页渲染
r = client.get(f"/doc/{doc_id}")
dd = r.get_data(as_text=True)
check("文档页含评论区", "评论" in dd and "commentList" in dd, "评论区未渲染")
check("文档页含发表按钮绑定", "postComment()" in dd, "postComment 未绑定")

print("\n=== 5) 审计页 ===")
# 5.1 管理员可访问审计页
r = client.get("/audit")
check("管理员访问 /audit 200", r.status_code == 200, f"status={r.status_code}")
check("审计页含『审计日志』标题", "审计日志" in r.get_data(as_text=True), "标题缺失")

# 5.2 非管理员访问被拒(403)
login_as(uid, "reader3", "user", "只读3")
r = client.get("/audit")
check("非管理员访问 /audit 被拒(403)", r.status_code == 403, f"status={r.status_code}")

# 5.3 审计接口返回含 doc_comment 动作
login_as(admin["id"], "admin", "admin", "系统管理员")
r = client.get("/api/admin/audit", query_string={"limit": 200})
aj = r.get_json()
check("审计接口 ok", aj.get("ok"), str(aj)[:200])
logs = aj.get("logs", [])
check("审计日志非空", len(logs) > 0, "无日志")
actions = {log.get("action") for log in logs}
check("含 doc_comment 动作", "doc_comment" in actions, f"动作集={actions}")
check("动作名已中文化", any(log.get("action_name") == "文档评论" for log in logs), "未中文化")
# 审计导出 CSV
r = client.get("/api/admin/audit/export")
check("审计导出 200", r.status_code == 200, f"status={r.status_code}")
csv_text = r.get_data(as_text=True)
check("导出含 CSV 表头", "操作" in csv_text or "动作" in csv_text, "表头缺失")

print("\n=== 6) 清理临时库 ===")
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
