# -*- coding: utf-8 -*-
"""
根据 scripts/e2e_visual_test.mjs 的运行结果（test_report_assets/results.json + 7 张截图），
生成一份带真实截图的 HTML 测试报告（图片 base64 内嵌，单文件可直接打开/分享）。

运行：/Users/ikingsmart/.workbuddy/binaries/python/envs/aikm/bin/python scripts/gen_test_report.py
产出：test_report_assets/测试报告.html
"""
import json
import base64
import datetime
from pathlib import Path

ROOT = Path("/Users/ikingsmart/AI-KM")
OUT = ROOT / "test_report_assets"
results = json.loads((OUT / "results.json").read_text(encoding="utf-8"))

# 把截图转成 base64 data-uri，内嵌进 HTML（单文件可分享）
def img_b64(name):
    p = OUT / name
    if not p.exists():
        return ""
    b = base64.b64encode(p.read_bytes()).decode("ascii")
    return f"data:image/png;base64,{b}"

# 每个用例对应的截图文件名（与 e2e 脚本一致）
shot_map = {
    "TC-01": "01_login.png",
    "TC-02": "02_dashboard.png",
    "TC-03": "03_browse_policy.png",
    "TC-04": "04_search_coronary.png",
    "TC-05": "05_search_drg.png",
    "TC-06": "06_ask_coronary.png",
    "TC-07": "07_ask_outofscope.png",
}

passed = sum(1 for r in results if r["passed"])
total = len(results)
today = datetime.date.today().isoformat()

# 用例中文说明（测试过程/预期）
desc = {
    "TC-01": "打开 /login，确认登录表单（账号/密码输入框）渲染正常。",
    "TC-02": "用 admin/Admin@123456 登录，确认跳转知识库工作台，分类导航卡片正常渲染。",
    "TC-03": "在工作台点击「政策法规库」分类卡片，确认跳转 /search?category_l1=POLICY 并以浏览模式列出该分类全部文档（带「📁 文档」标记）。",
    "TC-04": "在检索框输入「冠脉支架」混合检索，确认 TOP1 命中《2025年国家高值医用耗材集中带量采购政策解读》（真语义向量召回，非字面撞词）。",
    "TC-05": "在检索框输入「DRG」关键词检索，确认召回 DRG 付费改革相关文档。",
    "TC-06": "在问答页提问「冠脉支架集中带量采购何时开始、覆盖哪些品种？」，确认答案基于真实文档并标注 [1] 引用。",
    "TC-07": "提问知识库外问题（山东知识库建设预算），确认诚实标注「未找到/需补充」，不编造。",
}

rows = []
for r in results:
    tc = r["tc"]
    badge = "✅ 通过" if r["passed"] else "❌ 失败"
    shot = shot_map.get(tc, r.get("shot"))
    img = img_b64(shot) if shot else ""
    rows.append(f"""
    <div class="case">
      <div class="case-head">
        <span class="tag">{tc}</span>
        <span class="name">{r['name']}</span>
        <span class="{'ok' if r['passed'] else 'bad'}">{badge}</span>
      </div>
      <div class="case-desc">测试过程：{desc.get(tc,'')}</div>
      <div class="case-detail">结果：{r['detail']}</div>
      {f'<img src="{img}" alt="{tc} 截图"/>' if img else '<div class="nocap">（无截图）</div>'}
    </div>""")

# 本次修复记录（对应本次「问答差+知识库难用」根因修复）
fixes = [
    ("语义检索从「假」到「真」", "向量化 provider 由 hash 降级（占位符密钥）切换为本地 Ollama qwen3-embedding:0.6b；新增 scripts/reindex_vectors.py 全量重建 14 篇文档向量，语义召回生效。"),
    ("分类浏览 500", "/api/search 空查询+过滤时进入浏览模式；修复 SQL 行内「#」注释（SQLite 不认 #，改用 --）导致的 500。"),
    ("浏览模式前端断点", "search.html 的 doSearch() 原在空查询时直接 return 不调接口，使首页分类卡片跳转后停在空白页；改为「带过滤则允许空查询进入浏览模式」。"),
    ("前端自动检索", "search.html 新增 initFromUrl()，从首页分类卡片带参跳入时自动回填过滤并检索。"),
]

fix_html = "".join(f"<li><b>{t}</b>：{d}</li>" for t, d in fixes)

html = f"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>AI-KM 端到端测试报告</title>
<style>
  * {{ box-sizing: border-box; }}
  body {{ font-family: -apple-system, "PingFang SC", "Microsoft YaHei", Segoe UI, sans-serif;
         margin: 0; background: #f5f6f8; color: #1f2329; line-height: 1.6; }}
  .wrap {{ max-width: 980px; margin: 0 auto; padding: 32px 24px 64px; }}
  h1 {{ font-size: 26px; margin: 0 0 6px; }}
  .sub {{ color: #6b7280; font-size: 14px; margin-bottom: 20px; }}
  .summary {{ display: flex; gap: 16px; margin: 18px 0 28px; }}
  .card {{ flex: 1; background: #fff; border: 1px solid #e5e7eb; border-radius: 10px; padding: 16px 18px; }}
  .card .n {{ font-size: 30px; font-weight: 700; }}
  .card .l {{ color: #6b7280; font-size: 13px; }}
  .pass {{ color: #13a10e; }} .fail {{ color: #d13438; }}
  .sec {{ margin: 28px 0 12px; font-size: 18px; border-left: 4px solid #2563eb; padding-left: 10px; }}
  .env {{ background: #fff; border: 1px solid #e5e7eb; border-radius: 10px; padding: 14px 18px; font-size: 14px; }}
  .env code {{ background: #f0f1f3; padding: 1px 6px; border-radius: 4px; }}
  .case {{ background: #fff; border: 1px solid #e5e7eb; border-radius: 10px; padding: 16px 18px; margin: 14px 0; }}
  .case-head {{ display: flex; align-items: center; gap: 10px; }}
  .case-head .tag {{ background: #2563eb; color: #fff; border-radius: 6px; padding: 2px 8px; font-size: 13px; }}
  .case-head .name {{ font-weight: 600; font-size: 16px; }}
  .case-head .ok {{ color: #13a10e; font-weight: 700; margin-left: auto; }}
  .case-head .bad {{ color: #d13438; font-weight: 700; margin-left: auto; }}
  .case-desc {{ color: #374151; font-size: 14px; margin: 8px 0 4px; }}
  .case-detail {{ color: #111827; font-size: 14px; background: #f8fafc; border-radius: 6px; padding: 8px 10px; }}
  .case img {{ width: 100%; border: 1px solid #e5e7eb; border-radius: 8px; margin-top: 12px; display: block; }}
  .nocap {{ color: #9ca3af; padding: 12px; }}
  ul.fixes {{ background: #fff; border: 1px solid #e5e7eb; border-radius: 10px; padding: 14px 30px; font-size: 14px; }}
  .note {{ background: #fff7ed; border: 1px solid #fed7aa; border-radius: 10px; padding: 12px 16px; font-size: 14px; color: #9a3412; }}
  footer {{ color: #9ca3af; font-size: 12px; margin-top: 30px; text-align: center; }}
</style></head>
<body><div class="wrap">
  <h1>AI-KM 知识管理平台 · 端到端测试报告</h1>
  <div class="sub">生成时间：{today} ｜ 类型：真实浏览器端到端（Playwright/puppeteer-core 驱动本机 Chrome headless）｜ 服务：http://localhost:5200</div>

  <div class="summary">
    <div class="card"><div class="n pass">{passed}/{total}</div><div class="l">用例通过</div></div>
    <div class="card"><div class="n">7</div><div class="l">测试用例数</div></div>
    <div class="card"><div class="n">本地</div><div class="l">环境：macOS + Chrome</div></div>
  </div>

  <div class="sec">一、测试环境</div>
  <div class="env">
    • 被测系统：AI-KM 知识管理平台（Flask + SQLite + 本地 Ollama 语义向量）<br>
    • 服务地址：<code>http://localhost:5200</code>（gunicorn -k gthread -w 1 --threads 8）<br>
    • 测试账号：<code>admin / Admin@123456</code><br>
    • 浏览器：本机 Google Chrome（headless），视口 1366×900<br>
    • 驱动：puppeteer-core（Node 22），脚本 <code>scripts/e2e_visual_test.mjs</code><br>
    • 语义向量：Ollama <code>qwen3-embedding:0.6b</code>（维度 1024，本地零 token）
  </div>

  <div class="sec">二、测试用例与结果（含真实截图）</div>
  {''.join(rows)}

  <div class="sec">三、本次修复记录（对应「问答差 + 知识库难用」根因）</div>
  <ul class="fixes">{fix_html}</ul>

  <div class="sec">四、遗留项（独立问题，未纳入本次修复）</div>
  <div class="note">权限隔离回归：测试报告 docs/测试报告.md 记录 <code>reviewer_demo</code> / <code>contributor_demo</code> 比预期多看到 d7/d8/d9 三篇文档，疑似 ACL/部门隔离逻辑问题，与本次检索/问答修复无关，待单独排查。</div>

  <footer>本报告由 scripts/e2e_visual_test.mjs 真实运行生成，截图均为浏览器实际渲染结果。</footer>
</div></body></html>"""

(OUT / "测试报告.html").write_text(html, encoding="utf-8")
print("报告已生成：", OUT / "测试报告.html", "大小", len(html), "字节")
print(f"通过 {passed}/{total}")
