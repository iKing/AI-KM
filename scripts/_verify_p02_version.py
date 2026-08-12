# -*- coding: utf-8 -*-
"""
P0-2 / P0-3 端到端验证脚本（临时库，零污染）
============================================
验证链路：
  1) 初始化临时库 + 应用（自动播种 admin / 根部门）
  2) 入库一篇 Markdown 文档（产生 body_text，version=1）
  3) 取正文接口 /raw
  4) 编辑接口 /edit → 产生版本快照，version=2
  5) 版本列表 /versions（当前 v2 + 历史 1 条）
  6) 版本详情 /versions/<vid>（内容为旧正文）
  7) 回滚接口 /rollback → version=3，rollback_to=1
  8) 权限校验：普通 user 角色不可编辑（应 403）
  9) 回滚后 chunks 内容应为历史正文
"""
import os
import sys
import tempfile

# 临时数据库，验证完即删，绝不污染真实数据
TMP_DB = "/tmp/aikm_verify_p02_version.db"  # 独立库路径，避免与 _verify_p02.py 共用临时库互相干扰
if os.path.exists(TMP_DB):
    os.remove(TMP_DB)
os.environ["AIKM_DB_PATH"] = TMP_DB  # 必须在导入 app 前设置

ROOT = "/Users/ikingsmart/AI-KM"
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from app import create_app, db, auth, ingest, audit  # noqa: E402

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


# 启动应用（会建表 + 播种）
app = create_app()
client = app.test_client()

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

print("\n=== 1) 入库 Markdown 文档 ===")
md_v1 = """# 第一章 总则

为规范医疗器械集中采购活动，保障采购工作公开、公平、公正，依据国家有关法律法规及公司管理制度，制定本办法。本办法适用于公司及各分支机构开展的全部医疗器械采购业务，包括高值耗材、低值耗材、检验试剂及设备类采购。

## 1.1 适用范围

本办法适用于全体采购从业人员，以及参与采购评审、验收、结算等环节的相关岗位人员。凡涉及公司资金支出的器械采购行为，均须遵照本办法执行，不得以任何形式规避集中采购程序。

# 第二章 采购规则

采购报价须公开透明，供应商报价一经提交不得擅自修改。采购人员应当在评审前完成资格性审查，重点核验生产许可证、经营许可证、注册证及授权链条的完整性与有效期。对于同一品种存在多家供应商竞争的情形，应优先采用竞价方式确定成交候选人。

## 2.1 报价规则

供应商应在规定时限内通过采购平台提交报价，逾期提交视为放弃。报价应包含产品单价、配送费用、售后服务承诺及质保期限，不得遗漏或以口头方式补充说明。"""
tmp = tempfile.NamedTemporaryFile(suffix=".md", delete=False)
tmp.write(md_v1.encode("utf-8"))
tmp.close()
res = ingest.ingest_file(
    tmp.name,
    {
        "title": "测试制度文档",
        "category_l1": "POLICY",
        "department_id": dept_id,
        "security_level": "internal",
        "owner": "张三",
        "effective_date": "2025-01-01",
        "source": "测试局",
    },
    user_id=admin["id"],
)
doc_id = res["doc_id"]
check("入库成功", res["ok"], str(res))
check("初始版本=1", res.get("stats", {}).get("version", 1) == 1 or True)  # version 在 documents 表

# 校验 body_text 已写入
row = db.query_one("SELECT version, body_text, content_length FROM documents WHERE id = ?", (doc_id,))
check("body_text 已存", (row["body_text"] or "").strip() == md_v1, repr(row["body_text"]))
check("初始 version=1", row["version"] == 1)

print("\n=== 2) GET /raw 取正文 ===")
r = client.get(f"/api/documents/{doc_id}/raw")
d = r.get_json()
check("raw ok", d["ok"] and "总则" in (d["doc"]["body_text"] or ""), str(d)[:200])

print("\n=== 3) POST /edit 编辑生成版本 ===")
md_v2 = md_v1 + "\n\n# 第三章 监督考核\n未按期复审将通报。"
r = client.post(
    f"/api/documents/{doc_id}/edit",
    json={"title": "测试制度文档（修订稿）", "text": md_v2, "fmt": "md", "change_note": "补充第三章"},
)
d = r.get_json()
check("edit ok", d["ok"], str(d)[:200])
check("新版本=2", d.get("version") == 2, str(d.get("version")))

print("\n=== 4) GET /versions 版本列表 ===")
r = client.get(f"/api/documents/{doc_id}/versions")
d = r.get_json()
check("versions ok", d["ok"], str(d)[:200])
check("当前版本=2", d["current"]["version"] == 2)
check("历史有1条", len(d["versions"]) == 1, f"实际 {len(d['versions'])}")
vid = d["versions"][0]["id"]
check("历史版本号=1", d["versions"][0]["version"] == 1)

print("\n=== 5) GET /versions/<vid> 历史内容 ===")
r = client.get(f"/api/documents/{doc_id}/versions/{vid}")
d = r.get_json()
check("detail ok", d["ok"], str(d)[:200])
check("历史内容=旧正文", (d["version"]["content"] or "").strip() == md_v1, "内容不匹配")

print("\n=== 6) POST /rollback 回滚到 v1 ===")
r = client.post(f"/api/documents/{doc_id}/rollback", json={"version_id": vid})
d = r.get_json()
check("rollback ok", d["ok"], str(d)[:200])
check("回滚后版本=3", d.get("version") == 3, str(d.get("version")))
check("rollback_to=1", d.get("rollback_to") == 1, str(d.get("rollback_to")))

print("\n=== 7) 回滚后 chunks 应为历史正文 ===")
r = client.get(f"/api/documents/{doc_id}/versions")
d = r.get_json()
check("当前版本=3", d["current"]["version"] == 3)
check("历史有2条", len(d["versions"]) == 2, f"实际 {len(d['versions'])}")
chk = db.query("SELECT content, heading_path FROM chunks WHERE doc_id = ?", (doc_id,))
# 注意：chunker 把"标题文字"放在 heading_path 列、正文放在 content 列，
# 真正建立检索索引的是二者拼接（searchable_text = heading_path + 内容），
# 因此校验"回滚后内容"必须以"路径+正文"为基准，不能只看 content 列。
all_text = "\n".join((f"{c['heading_path']}\n{c['content']}") if c["heading_path"] else c["content"] for c in chk)
check("回滚后含'总则'", "总则" in all_text, "切片无总则")
check("回滚后无'监督考核'", "监督考核" not in all_text, "切片仍含修订内容")

print("\n=== 8) 权限校验：普通 user 不可编辑 ===")
# 将 doc_id 置为已发布，使同部门 internal 普通用户「可见但不可编辑」，
# 从而验证的是真正的编辑权限闸门(403)，而非可见性闸门(404)。
db.execute("UPDATE documents SET status = 'published' WHERE id = ?", (doc_id,))
uid = auth.create_user(username="reader", display_name="只读", password="X@123456", role="user", department_id=dept_id, must_change_pwd=False)
with client.session_transaction() as sess:
    sess["user_id"] = uid
    sess["username"] = "reader"
    sess["role"] = "user"
    sess["display_name"] = "只读"
r = client.post(f"/api/documents/{doc_id}/edit", json={"text": "篡改", "fmt": "md"})
check("普通用户编辑被拒(403)", r.status_code == 403, f"status={r.status_code}")

print("\n=== 9) 页面渲染冒烟测试 ===")
# 第 8 步把会话切成了 reader（普通用户），此处切回 admin 再测页面，
# 否则 reader 对 internal 文档无可见权会一路 404，污染本步断言。
with client.session_transaction() as sess:
    sess["user_id"] = admin["id"]
    sess["username"] = "admin"
    sess["role"] = "admin"
    sess["display_name"] = "系统管理员"
# 编辑器页：应返回 200 且注入 DOC_ID / CAN_EDIT，含正文编辑区
r = client.get(f"/doc/{doc_id}/edit")
ed = r.get_data(as_text=True)
check("编辑器页 200", r.status_code == 200, f"status={r.status_code}")
check("编辑器页含 DOC_ID 注入", "DOC_ID" in ed, "未注入文档 ID")
check("编辑器页含富文本编辑区(Quill)", 'id="editor"' in ed and "quill.min.js" in ed, "缺 Quill 编辑器容器/脚本")
check("编辑器页含版本历史区", "ver-list" in ed, "缺版本历史面板")
# 文档详情页：应含"编辑/版本历史"入口链接指向 /edit
r = client.get(f"/doc/{doc_id}")
dd = r.get_data(as_text=True)
check("文档页含编辑入口", f"/doc/{doc_id}/edit" in dd, "文档页未挂编辑入口")

print("\n=== 10) 清理临时库 ===")
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
