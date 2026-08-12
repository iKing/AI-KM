# -*- coding: utf-8 -*-
"""
富文本编辑器 + 图片内嵌 端到端验证脚本
=======================================
覆盖：
  1. Quill 富文本 HTML 存为 body_html，同时剥标签回填 body_text（RAG/切片/diff 不变）
  2. 服务端 XSS 净化：script / onerror / javascript: 被剔除
  3. 图片上传接口 + /uploads 静态服务可用
  4. /raw 返回 body_html；/doc/<id> 页面渲染富文本
  5. 版本回滚能还原 body_html
  6. 全链路不破坏既有 body_text 检索
用法：python scripts/_verify_richtext.py
"""
import os
import sys
import io
import base64

sys.path.insert(0, "/Users/ikingsmart/AI-KM")  # 保证能 import app
DB = "/tmp/aikm_rich_test.db"
if os.path.exists(DB):
    os.remove(DB)
os.environ["AIKM_DB_PATH"] = DB

import run  # 载入 .env 与路径
run._preload_env()
from app import create_app, db  # 应用与数据库

app = create_app()  # 创建应用（临时库）
c = app.test_client()  # 测试客户端

passed = 0
failed = 0


def check(name, cond, extra=""):
    """统一的断言记账：通过 +1，失败打印明细。"""
    global passed, failed
    if cond:
        passed += 1
        print(f"  ✅ {name}")
    else:
        failed += 1
        print(f"  ❌ {name}  {extra}")


print("== 1. 登录与建文档 ==")
r = c.post("/api/login", json={"username": "admin", "password": "Admin@123456"})
check("默认管理员登录", r.get_json().get("ok"), str(r.get_json())[:120])

dept = db.query_one("SELECT id FROM departments LIMIT 1")  # 取一个部门
check("存在部门可用于建文档", bool(dept))
doc_id = db.execute(
    "INSERT INTO documents (title, category_l1, department_id, owner, status, version, body_text, content_length, created_at, updated_at) "
    "VALUES (?,?,?,?,?,?,?,?,?,?)",
    ("富文本测试", "测试", dept["id"], "admin", "uploaded", 1, "初始正文内容", 6, "2026-08-12", "2026-08-12"),
)
check("新建测试文档成功", isinstance(doc_id, int) and doc_id > 0, f"doc_id={doc_id}")

print("== 2. 富文本保存：body_html + body_text 双存 ==")
LP = "知识内容" * 60  # 凑够 200+ 字，满足最低入库线
html = f'<h2>章节标题</h2><p>正文带<strong>加粗</strong>与<img src="/uploads/demo.png" alt="示意图"></p><ul><li>项一</li></ul><p>{LP}</p>'
r = c.post(f"/api/documents/{doc_id}/edit", json={"title": "富文本测试", "html": html, "change_note": "初始化"})
check("富文本保存成功", r.get_json().get("ok"), str(r.get_json())[:160])
doc = db.query_one("SELECT body_html, body_text FROM documents WHERE id=?", (doc_id,))
check("body_html 已存且含排版标签", bool(doc["body_html"]) and "<strong>" in doc["body_html"] and "<img" in doc["body_html"])
check("body_text 由 HTML 剥出纯文本", "加粗" in (doc["body_text"] or "") and "章节标题" in (doc["body_text"] or ""))
check("body_text 不含 HTML 标签（检索不被污染）", "<strong>" not in (doc["body_text"] or ""))

print("== 3. XSS 净化 ==")
evil = f'<p>正常{LP}</p><script>alert(1)</script><img src="x" onerror="alert(2)"><a href="javascript:alert(3)">坏链</a>'
r = c.post(f"/api/documents/{doc_id}/edit", json={"html": evil})
doc = db.query_one("SELECT body_html FROM documents WHERE id=?", (doc_id,))
bh = doc["body_html"] or ""
check("剔除 <script>", "<script" not in bh)
check("剔除 onerror 事件属性", "onerror" not in bh.lower())
check("剔除 javascript: 伪协议链接", "javascript:" not in bh.lower())
check("保留正常文本与 <p>", "正常" in bh and "<p>" in bh)

print("== 4. 图片上传 + 静态服务 ==")
png = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+M8AAAMBAQDJ/pLvAAAAAElFTkSuQmCC"
)
r = c.post(f"/api/documents/{doc_id}/images", data={"image": (io.BytesIO(png), "shot.png")},
           content_type="multipart/form-data")
j = r.get_json()
check("图片上传返回 ok", j.get("ok"), str(j)[:160])
check("返回 /uploads/ 开头的 URL", j.get("ok") and j["url"].startswith("/uploads/"), str(j)[:160])
url = j["url"]
r = c.get(url)
check("/uploads 静态服务返回图片(200)", r.status_code == 200 and r.mimetype.startswith("image"), f"status={r.status_code} mime={r.mimetype}")
# 超大队片应被拒
big = b"\x00" * (11 * 1024 * 1024)
r = c.post(f"/api/documents/{doc_id}/images", data={"image": (io.BytesIO(big), "big.png")},
           content_type="multipart/form-data")
check("超大图片被拒(413)", r.status_code == 413, f"status={r.status_code}")

print("== 5. /raw 与 /doc 页面渲染 ==")
r = c.get(f"/api/documents/{doc_id}/raw")
check("/raw 返回 body_html", r.get_json().get("ok") and bool(r.get_json()["doc"]["body_html"]))
r = c.get(f"/doc/{doc_id}")
check("/doc 页面 200", r.status_code == 200, f"status={r.status_code}")
page_text = r.get_data(as_text=True)  # 页面 HTML 文本
check("/doc 页面渲染了富文本正文", "doc-rich" in page_text and "正常" in page_text)

print("== 6. 版本回滚还原 body_html ==")
# 先编辑到 html_A（标记 AAA），再编辑到 html_B（标记 BBB）；
# 编辑到 html_B 时会把 html_A 的状态快照进 doc_versions（含 body_html=AAA）
html_a = f'<h2>AAA标题</h2><p>AAA内容{LP}</p>'
r = c.post(f"/api/documents/{doc_id}/edit", json={"html": html_a})
check("保存 AAA 版本成功", r.get_json().get("ok"))
html_b = f'<h2>BBB标题</h2><p>BBB内容{LP}</p>'
r = c.post(f"/api/documents/{doc_id}/edit", json={"html": html_b})
check("保存 BBB 版本成功（AAA 被快照）", r.get_json().get("ok"))
# 从数据库找到含 AAA 的版本快照（即 html_A 的状态）
vrow = db.query_one("SELECT id FROM doc_versions WHERE doc_id=? AND body_html LIKE '%AAA%' ORDER BY id DESC", (doc_id,))
check("能定位到含富文本的版本快照", bool(vrow), "未找到 AAA 快照")
if vrow:
    r = c.post(f"/api/documents/{doc_id}/rollback", json={"version_id": vrow["id"]})
    check("回滚成功", r.get_json().get("ok"), str(r.get_json())[:160])
    doc = db.query_one("SELECT body_html, body_text FROM documents WHERE id=?", (doc_id,))
    check("回滚后 body_html 还原为 AAA 版内容", "AAA" in (doc["body_html"] or ""))
    check("回滚后 body_text 同步还原", "AAA" in (doc["body_text"] or ""))

print("== 7. 检索仍可用（body_text 驱动）==")
r = c.get("/api/search?q=" + "加粗")
res = r.get_json()
check("关键词检索命中富文本正文", res.get("ok") and any(d.get("doc_id") == doc_id for d in res.get("results", [])),
      str(res)[:160])

print(f"\n结果：通过 {passed} / 失败 {failed}")
sys.exit(1 if failed else 0)
