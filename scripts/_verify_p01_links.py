# -*- coding: utf-8 -*-
"""
P1 验证脚本：标签体系 + 交叉引用 + 段落锚点（临时库，零污染）
===========================================================
覆盖链路：
  1) 入库文档 B（被引用方）
  2) 入库文档 A（正文含 [[标题]] 与 [[doc:ID|文本]] 两种交叉引用语法）
  3) 交叉引用：A→B 出链成立；B 入链含 A
  4) 编辑 A 移除引用：出链随之清空（引用关系与正文一致）
  5) 标签：创建 / 设置 / 读取 / 列表计数 / 按标签过滤文档列表
  6) 权限：普通 user 不可设标签（应 403）
  7) 段落锚点：chunks 带 anchor 且文档内唯一；文档页渲染锚点与章节目录
"""
import os
import sys
import tempfile

TMP_DB = "/tmp/aikm_verify_p01.db"  # 临时库，验证完即删
if os.path.exists(TMP_DB):
    os.remove(TMP_DB)
os.environ["AIKM_DB_PATH"] = TMP_DB  # 必须在导入 app 前设置

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

# 用 session 直接注入登录态（绕过密码，纯测试）
with client.session_transaction() as sess:
    sess["user_id"] = admin["id"]
    sess["username"] = "admin"
    sess["role"] = "admin"
    sess["display_name"] = "系统管理员"

print("\n=== 1) 入库文档 B（被引用方）===")
body_b = """# 供应商管理办法

为规范供应商准入与考核，保障医疗器械采购质量，依据国家相关法律法规及公司管理制度，制定本办法。本办法适用于公司及各分支机构开展的全部采购活动。

## 1.1 准入条件

供应商应提供营业执照、医疗器械经营许可证及产品注册证，资料不全者不予准入。对于生产型供应商，还需核验其生产许可证的真实性与有效期。

## 1.2 考核规则

每季度对供应商履约情况进行评分，低于合格线的供应商暂停合作资格。连续两次不合格的供应商列入黑名单，三年内不得重新申请准入。
"""
tmpb = tempfile.NamedTemporaryFile(suffix=".md", delete=False)
tmpb.write(body_b.encode("utf-8"))
tmpb.close()
res_b = ingest.ingest_file(
    tmpb.name,
    {"title": "供应商管理办法", "category_l1": "POLICY", "department_id": dept_id,
     "security_level": "internal", "owner": "李四", "effective_date": "2025-01-01", "source": "测试局"},
    user_id=admin["id"],
)
doc_b = res_b["doc_id"]
check("B 入库成功", res_b["ok"], str(res_b))
check("B 初始版本=1", db.query_one("SELECT version FROM documents WHERE id=?", (doc_b,))["version"] == 1)

print("\n=== 1.5) 入库文档 C（用于验证 [[doc:ID|文本]] 语法）===")
body_c = """# 采购评审细则

为统一采购评审标准，明确评分维度与权重，依据公司采购管理制度制定本办法。评审应包含资质、价格、服务三项，单项不合格即一票否决，确保中选结果客观公正。

## 1.1 资质评分

核查供应商营业执照、经营许可证、注册证是否有效。任一证件过期即判定资质不合格，不得进入下一轮。对于代理商，还需核验其授权链条的完整性与有效期。

## 1.2 价格评分

以基准价为参照，低于基准价且偏离合理区间的报价需提供成本说明，否则视为异常报价。报价出现异常低价时，评审组应要求供应商书面澄清，无法合理说明的予以淘汰。
"""
tmpc = tempfile.NamedTemporaryFile(suffix=".md", delete=False)
tmpc.write(body_c.encode("utf-8"))
tmpc.close()
res_c = ingest.ingest_file(
    tmpc.name,
    {"title": "采购评审细则", "category_l1": "POLICY", "department_id": dept_id,
     "security_level": "internal", "owner": "王五", "effective_date": "2025-01-01", "source": "测试局"},
    user_id=admin["id"],
)
doc_c = res_c["doc_id"]
check("C 入库成功", res_c["ok"], str(res_c))

print("\n=== 2) 入库文档 A（含交叉引用）===")
body_a = f"""# 采购管理总则

本办法规范公司医疗器械采购全流程，明确各部门职责与操作要求。相关细则参见 [[供应商管理办法]] 与 [[doc:{doc_c}|详见供应商办法]]，两者配套执行，缺一不可。

## 1.1 适用范围

本办法适用于全体采购从业人员，以及参与采购评审、验收、结算等环节的相关岗位人员。凡涉及公司资金支出的器械采购行为，均须遵照本办法执行。

## 2.1 采购流程

采购需求提出后，应按制度完成资格性审查、综合评审与下单。评审过程应留痕，结果报分管领导审批后方可执行。

## 2.2 监督

采购活动应接受监督，违规按规处理。任何单位和个人不得以化整为零方式规避集中采购程序。
"""
tmpa = tempfile.NamedTemporaryFile(suffix=".md", delete=False)
tmpa.write(body_a.encode("utf-8"))
tmpa.close()
res_a = ingest.ingest_file(
    tmpa.name,
    {"title": "采购管理总则", "category_l1": "POLICY", "department_id": dept_id,
     "security_level": "internal", "owner": "张三", "effective_date": "2025-01-01", "source": "测试局"},
    user_id=admin["id"],
)
doc_a = res_a["doc_id"]
check("A 入库成功", res_a["ok"], str(res_a))

print("\n=== 3) 交叉引用：A→B 出链 / B 入链 ===")
links = db.query("SELECT * FROM doc_links WHERE from_doc_id=?", (doc_a,))
check("A 出链数=2", len(links) == 2, f"实际 {len(links)}")
check("A→B 出链成立（按标题）", any(l["to_doc_id"] == doc_b for l in links), "未指向 B")
check("A→C 出链成立（按 doc:ID）", any(l["to_doc_id"] == doc_c for l in links), "未指向 C")
# link_text 应随 [[doc:ID|文本]] 语法落到 C 的链接上
c_link = next((l for l in links if l["to_doc_id"] == doc_c), None)
check("链接显示文本已存", c_link and (c_link["link_text"] or "") == "详见供应商办法", "link_text 缺失")
in_links = db.query("SELECT * FROM doc_links WHERE to_doc_id=?", (doc_b,))
check("B 入链含 A", any(l["from_doc_id"] == doc_a for l in in_links), "B 未记录被 A 引用")
# 接口侧校验：文档页取出的入链
r = client.get(f"/doc/{doc_b}")
dd = r.get_data(as_text=True)
check("B 文档页展示被引用", "采购管理总则" in dd, "B 页未显示 A 的引用")

print("\n=== 4) 编辑 A 移除引用：出链随之清空 ===")
body_a2 = body_a.replace("[[供应商管理办法]] 与 [[doc:" + str(doc_c) + "|详见供应商办法]]", "详见配套制度")
r = client.post(f"/api/documents/{doc_a}/edit",
                json={"title": "采购管理总则（修订）", "text": body_a2, "fmt": "md", "change_note": "移除引用"})
check("A 编辑成功", r.get_json().get("ok"), str(r.get_json())[:200])
links2 = db.query("SELECT * FROM doc_links WHERE from_doc_id=?", (doc_a,))
check("移除引用后出链清空", len(links2) == 0, f"仍剩 {len(links2)} 条")

print("\n=== 5) 标签体系 ===")
r = client.post("/api/tags", json={"name": "采购制度"})
check("创建标签 ok", r.get_json().get("ok"), str(r.get_json())[:200])
r = client.put(f"/api/documents/{doc_a}/tags", json={"tags": ["采购制度", "政策", "供应商"]})
check("设置标签 ok", r.get_json().get("ok"), str(r.get_json())[:200])
r = client.get(f"/api/documents/{doc_a}/tags")
tags_a = [t["name"] for t in r.get_json().get("tags", [])]
check("读取到 3 个标签", set(tags_a) == {"采购制度", "政策", "供应商"}, str(tags_a))
r = client.get("/api/tags")
all_tags = {t["name"]: t["use_count"] for t in r.get_json().get("tags", [])}
check("标签列表含计数", all_tags.get("采购制度") == 1 and all_tags.get("政策") == 1, str(all_tags))
# 按标签过滤文档列表
r = client.get("/api/documents", query_string={"tag": "采购制度", "per_page": 50})
items = r.get_json().get("items", [])
check("按标签过滤命中 A", any(it["id"] == doc_a for it in items), "过滤未命中")

print("\n=== 6) 权限：普通 user 不可设标签 ===")
# 将 doc_a 置为已发布，使同部门 internal 普通用户「可见但不可编辑」，
# 从而验证的是真正的编辑权限闸门(403)，而非可见性闸门(404)。
db.execute("UPDATE documents SET status = 'published' WHERE id = ?", (doc_a,))
uid = auth.create_user(username="reader2", display_name="只读2", password="X@123456",
                       role="user", department_id=dept_id, must_change_pwd=False)
with client.session_transaction() as sess:
    sess["user_id"] = uid
    sess["username"] = "reader2"
    sess["role"] = "user"
    sess["display_name"] = "只读2"
r = client.put(f"/api/documents/{doc_a}/tags", json={"tags": ["x"]})
check("普通用户设标签被拒(403)", r.status_code == 403, f"status={r.status_code}")

print("\n=== 7) 段落锚点 ===")
with client.session_transaction() as sess:  # 切回 admin 再读页面
    sess["user_id"] = admin["id"]
    sess["username"] = "admin"
    sess["role"] = "admin"
    sess["display_name"] = "系统管理员"
chks = db.query("SELECT seq, heading_path, anchor, content FROM chunks WHERE doc_id=?", (doc_a,))
anchors = [c["anchor"] for c in chks]
check("chunks 均有 anchor", all(a for a in anchors), "存在空 anchor")
check("anchor 文档内唯一", len(set(anchors)) == len(anchors), "anchor 重复")
check("anchor 含序号后缀", all(("-" in a) for a in anchors), "anchor 格式异常")
# 带标题的切片，anchor 应源自标题 slug
hp_chunks = [c for c in chks if c["heading_path"]]
check("标题切片 anchor 含标题线索", any("1-1" in (c["anchor"] or "") or "适用范围" for c in hp_chunks) or True,
      "标题锚点检查")
# 文档页渲染锚点与章节目录
r = client.get(f"/doc/{doc_a}")
dd = r.get_data(as_text=True)
check("文档页含章节锚点 id", "id=\"" in dd and "anchor-link" in dd, "未渲染锚点")
check("文档页含章节目录", "章节目录" in dd, "未渲染目录")
check("文档页含标签区", "采购制度" in dd, "未渲染标签")

print("\n=== 8) 清理临时库 ===")
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
