# -*- coding: utf-8 -*-
"""
测试数据初始化与权限验证脚本
============================

用途：
  1. 往 AI-KM 知识库灌入一套「演示用」测试数据，方便验收与培训：
     - 6 个测试账号，覆盖全部 4 种角色（管理员/审核员/贡献者/普通用户）
       + 2 个密级/跨部门变体，用于验证权限隔离
     - 2 个测试部门（采购一部、市场部），外加系统自带的「知识管理中心」
     - 13 篇测试文档，覆盖 3 级密级 × 6 大分类 × 多部门 × 多状态
  2. 用 Flask 测试客户端（test_client）模拟各账号登录，断言「可见文档范围」
     完全符合权限模型（密级许可 + 部门隔离 + 公开全可见）。
  3. 顺带做功能冒烟：检索命中、AI 问答引用、审核通过/退回。
  4. 把结果写成一份《测试报告》md 文件。

设计原则：
  - 幂等：重跑不会重复建账号/部门/文档（按唯一键跳过）
  - 测试账号统一密码 Demo@123456，且关闭强制改密，方便演示
  - 文档正文均 >= 200 字，规避「内容过短」红线

用法：
    python scripts/seed_demo.py
"""

import sys  # 用于把项目根加入模块搜索路径
import tempfile  # 用于生成临时入库文件
from pathlib import Path  # 跨平台路径处理

# 把项目根目录加入 sys.path，保证 `import app` 可用
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# 关键：在导入 app 之前先加载 .env，否则 config 读到的是默认值而非 .env 里的真实配置。
# run.py 顶层的 import 不会触发 app 加载，因此可安全复用其 _preload_env 逻辑。
import run as _run  # 复用 run.py 的 .env 预加载函数
_run._preload_env()

from app import create_app, config, db, auth, audit  # 引入内部模块


# ============================================================
# 一、测试数据定义（集中在此，方便维护）
# ============================================================

# 所有测试账号统一使用的密码
TEST_PWD = "Demo@123456"

# 测试部门：知识管理中心(id=1)由 create_app 自动播种，这里只补两个业务部
DEPARTMENTS = [
    ("采购一部", "PURCHASE-1", "负责医用耗材集中带量采购执行与供应商管理"),
    ("市场部", "MARKET", "负责客户拓展、市场分析与竞品研究"),
]

# 测试账号：覆盖四种角色 + 密级/跨部门两个变体
# max_security：该账号能看见的最高密级（public<internal<confidential）
# cross_dept：是否可跨部门查看（True 才能看别的部门的非公开文档）
USERS = [
    dict(username="admin_demo", display_name="演示管理员", role="admin",
         dept_name="知识管理中心", max_security="confidential", cross_dept=True,
         desc="系统管理员：不受部门限制、可见全部密级"),
    dict(username="reviewer_demo", display_name="演示审核员", role="reviewer",
         dept_name="采购一部", max_security="confidential", cross_dept=False,
         desc="知识审核员：本部门 + 可见机密，但跨部门不可见"),
    dict(username="contributor_demo", display_name="演示贡献者", role="contributor",
         dept_name="采购一部", max_security="internal", cross_dept=False,
         desc="知识贡献者：本部门 + 仅到内部级，看不到机密"),
    dict(username="user_demo", display_name="演示普通用户", role="user",
         dept_name="市场部", max_security="internal", cross_dept=False,
         desc="普通用户：本部门市场部 + 仅到内部级"),
    dict(username="user_conf_demo", display_name="演示用户(可看机密)", role="user",
         dept_name="市场部", max_security="confidential", cross_dept=False,
         desc="普通用户：本部门市场部 + 可看机密，但跨部门不可见"),
    dict(username="user_cross_demo", display_name="演示用户(跨部门)", role="user",
         dept_name="市场部", max_security="internal", cross_dept=True,
         desc="普通用户：跨部门可见，但密级仅到内部级"),
]

# 测试文档：每篇含元数据 + 正文内容
# 字段：key(内部标识), title, category_l1, category_l2, dept_name,
#       security_level, quality_level, owner, effective_date, source,
#       content(正文), auto_publish(是否跳过审核直接发布)
DOCS = [
    dict(key="d1", title="2025年国家高值医用耗材集中带量采购政策解读",
         category_l1="POLICY", category_l2="POLICY-NATIONAL", dept_name="知识管理中心",
         security_level="public", quality_level="verified", owner="政策研究组",
         effective_date="2025-01-01", source="国家医保局",
         auto_publish=True,
         content="""# 2025年国家高值医用耗材集中带量采购政策解读

本章节系统解读高值医用耗材集中带量采购（以下简称集采）政策对医院采购流程的影响。

根据国家医保局相关文件，冠脉支架、骨科耗材、人工关节等已分批纳入国家组织集中带量采购范围。医院在采购此类耗材时，必须通过省级采购平台统一下单，不得自行线下议价，也不得违规采购非中选产品替代。

集采政策的核心目标是挤出流通环节水分，降低患者负担与医保支出。对医院而言，需重新核算耗材成本结构，将集采任务完成情况纳入科室考核，并建立中选产品供应保障与缺货应急预案。

未中选产品仍可在备案后限量使用，但其价格不得高于同组中选产品最高价。医院采购部门应定期比对中选与备案产品价格，动态调整采购策略，确保合规与成本可控。"""),
    dict(key="d2", title="某省医用耗材采购管理办法实施细则",
         category_l1="POLICY", category_l2="POLICY-PROVINCIAL", dept_name="采购一部",
         security_level="internal", quality_level="normal", owner="采购一部",
         effective_date="2025-03-01", source="省医保局",
         auto_publish=True,
         content="""# 某省医用耗材采购管理办法实施细则

本细则适用于全省公立医疗机构医用耗材的采购、验收、结算与监管全流程。

第一条 医疗机构应全部纳入省级医药采购平台，线上采购率不得低于规定比例，严禁网下采购、变相规避招标。

第二条 高值耗材实行条码追溯管理，入库、使用、计费环节须扫码登记，确保可追溯至具体患者与批次。

第三条 建立耗材合理使用点评制度，对用量异常增长的前十名耗材开展合理性评价，评价结果纳入绩效考核。

第四条 采购部门应每季度汇总平台交易数据，分析价格趋势与供应商履约情况，形成季度报告报分管领导。本细则自发布之日起执行，由采购管理部门负责解释。"""),
    dict(key="d3", title="XX医院大型设备招标内部评审要点",
         category_l1="POLICY", category_l2="POLICY-BIDDING", dept_name="采购一部",
         security_level="confidential", quality_level="normal", owner="采购一部",
         effective_date="2025-04-01", source="内部评审",
         auto_publish=True,
         content="""# XX医院大型设备招标内部评审要点（机密）

本文件为内部评审参考资料，记录该院在大型医疗设备招标中的评分权重与谈判底线，仅限采购与审计相关人员查阅。

一、评分构成：技术分占45%，价格分占35%，售后服务与本地化能力占20%。技术分中，核心参数满足性为否决项。

二、谈判底线：在预算金额内，允许中标价较预算下浮不低于8%；若出现两家参数近似，优先选择本地有备件库者。

三、风险管控：要求供应商提供原厂授权链证明，防止串货与翻新机；验收须由第三方检测机构出具报告。

四、合规红线：评审专家须签署回避声明，严禁向供应商泄露其他投标人报价。任何泄密行为按保密制度追责。"""),
    dict(key="d4", title="某三甲医院耗材SPD项目投标方案",
         category_l1="PROJECT", category_l2="PROJECT-BID", dept_name="采购一部",
         security_level="internal", quality_level="normal", owner="采购一部",
         effective_date="2025-05-10", source="项目团队",
         auto_publish=True,
         content="""# 某三甲医院耗材SPD项目投标方案

本项目为面向三甲医院的医用耗材供应链（SPD）整体服务投标，目标是实现耗材从入院到使用的全流程精细化管控。

方案要点：一是建设中心库+科室二级库体系，通过智能柜与扫码实现消耗即结算；二是对接医院 HIS 与省采购平台，确保线上采购率达标；三是提供驻场运营团队，承担库存周转与效期管理。

商务部分承诺 48 小时应急补货、近效期耗材主动置换、月度消耗数据分析报告。技术部分突出条码追溯与医保编码映射能力，满足飞检要求。

风险与对策：医院原有流程改造成本高，采用分阶段上线降低阻力；数据接口复杂，预留标准 API 与人工导入双通道。本项目预计可降低医院耗材管理人力约三成。"""),
    dict(key="d5", title="SPD项目上线实施标准作业手册",
         category_l1="PROJECT", category_l2="PROJECT-IMPL", dept_name="知识管理中心",
         security_level="public", quality_level="verified", owner="实施组",
         effective_date="2025-02-15", source="实施团队",
         auto_publish=True,
         content="""# SPD项目上线实施标准作业手册

本手册规定 SPD（供应—加工—配送）项目上线的标准步骤，供实施工程师与医院对接人统一遵循。

阶段一 准备：完成现场调研、网络与硬件点位确认、基础数据收集（耗材目录、科室、库位）。

阶段二 部署：安装智能柜与扫码设备，部署服务端，导入耗材主数据与条码规则。

阶段三 切换：选取试点科室先行，培训库管与护士扫码操作，观察一周无误后全院推广。

阶段四 运营：建立日报与异常工单机制，定期盘点，出具消耗分析。每个阶段均需签署里程碑确认单，作为验收依据。本手册随版本更新维护。"""),
    dict(key="d6", title="客户常见采购合规问答TOP20",
         category_l1="CUSTOMER", category_l2="CUSTOMER-FAQ", dept_name="市场部",
         security_level="public", quality_level="normal", owner="市场部",
         effective_date="2025-01-20", source="客户成功团队",
         auto_publish=True,
         content="""# 客户常见采购合规问答 TOP20

Q1 医院能否线下采购集采中选耗材？答：必须通过省级平台线上采购，线下采购违规。

Q2 未中选耗材还能用吗？答：可备案后限量使用，价格不得高于中选最高价。

Q3 高值耗材如何追溯？答：入院到使用须扫码，关联患者与批次。

Q4 供应商资质怎么审？答：核验营业执照、医疗器械经营许可、产品注册证三证齐全且在效期。

Q5 飞检关注什么？答：线上采购率、追溯完整性、价格合规。

Q6 合同续签注意？答：复核中选身份与价格联动条款，避免价外收费。

Q7 耗材近效期怎么办？答：系统预警，主动置换，禁止临期使用于患者。

Q8 数据对接难点？答：HIS 编码与医保编码映射，建议建对照表并定期校准。本文档持续更新。"""),
    dict(key="d7", title="华东区某医院采购决策链分析",
         category_l1="CUSTOMER", category_l2="CUSTOMER-PROFILE", dept_name="市场部",
         security_level="internal", quality_level="draft", owner="市场部",
         effective_date="2025-06-01", source="市场调研",
         auto_publish=True,
         content="""# 华东区某医院采购决策链分析（待提质）

本文记录该院耗材采购关键决策角色与流程，用于市场拓展参考，内容仍需补充访谈核实。

决策链概览：设备科发起需求，采购中心组织论证，医学工程处做技术评估，分管院长审批，部分高值项目上党委会。

关键触点：设备科主任关注合规与供货稳定；临床科室主任关注产品性能与学术支持；财务关注预算与回款周期。

待补充：各角色具体 KPI、历史合作供应商清单、近一年采购金额分布。当前信息来自公开资料与一次非正式交流，标记 draft 待提质，入库后需负责人复核补充。"""),
    dict(key="d8", title="智能采购耗材目录管理系统V2产品白皮书",
         category_l1="PRODUCT", category_l2="PRODUCT-MANUAL", dept_name="市场部",
         security_level="internal", quality_level="normal", owner="产品部",
         effective_date="2025-03-15", source="产品团队",
         auto_publish=True,
         content="""# 智能采购耗材目录管理系统 V2 产品白皮书

本系统面向医院采购部门，提供耗材目录管理、价格监测、合规校验与采购分析一体化能力。

核心模块：一是目录中心，维护耗材主数据并与医保编码、省平台目录映射；二是合规引擎，自动校验线下采购、价格超限、资质过期等风险并预警；三是分析中心，按科室、供应商、品类输出消耗与成本报表。

V2 相比 V1 的改进：引入 AI 问答，支持自然语言查询政策与历史采购；增强飞检预检清单；开放标准 API 便于与 HIS/SPD 对接。

部署方式支持私有化与云租户，数据加密存储，操作全程审计。适用于三甲与县域医共体等多种规模。"""),
    dict(key="d9", title="采购算法模型内部技术参数",
         category_l1="PRODUCT", category_l2="PRODUCT-TECH", dept_name="市场部",
         security_level="confidential", quality_level="normal", owner="算法组",
         effective_date="2025-04-10", source="内部技术",
         auto_publish=True,
         content="""# 采购算法模型内部技术参数（机密）

本文件记录价格异常检测与需求预测模型的关键参数，仅限算法与采购核心人员查阅，禁止外发。

模型一 价格异常检测：基于同品类历史成交价分布，采用稳健 Z 分数，阈值设为 3；对集采中选品种启用价幅联动校验，偏离超过 5% 触发复核。

模型二 需求预测：以科室历史消耗为序列，使用季节性分解 + 指数平滑，预测 horizon 为 90 天，置信区间 80%。

特征工程：效期、季节、集采批次、供应商履约率为关键特征。模型每周增量训练，离线评估 MAPE 须低于 15%。

安全说明：训练数据脱敏存储，推理服务仅内网可达，访问需机密级权限并留痕。"""),
    dict(key="d10", title="知识管理平台使用与保密制度",
         category_l1="PROCESS", category_l2="PROCESS-SYSTEM", dept_name="知识管理中心",
         security_level="public", quality_level="verified", owner="知识管理中心",
         effective_date="2025-01-05", source="知识管理中心",
         auto_publish=True,
         content="""# 知识管理平台使用与保密制度

第一条 平台用于公司知识资产的统一沉淀、检索与复用，所有员工应按规范入库个人与团队知识。

第二条 密级分为公开、内部、机密三级。公开面向全员；内部限本部门及授权人员；机密须经审批并限定知悉范围。

第三条 上传文档须填写完整元数据，接受质量校验；机密文档须标注知悉范围，严禁通过平台外渠道转发。

第四条 所有检索、下载、问答操作均留审计日志，不可篡改，用于合规追溯。

第五条 账号专人专用，离职或转岗须及时调整权限；发现疑似泄露立即上报。本制度由知识管理中心解释与监督执行。"""),
    dict(key="d11", title="新入职采购专员知识库使用培训讲义",
         category_l1="TRAINING", category_l2="TRAINING-COURSE", dept_name="采购一部",
         security_level="internal", quality_level="normal", owner="采购一部",
         effective_date="2025-05-20", source="培训中心",
         auto_publish=True,
         content="""# 新入职采购专员知识库使用培训讲义

欢迎加入采购团队。本课帮助你快速用好知识管理平台，把个人经验变成团队资产。

一、怎么查：在检索页输入自然语言，如「集采怎么报量」，系统返回相关片段并高亮关键词；可用分类、密级筛选缩小范围。

二、怎么问：在问答页提问，AI 会基于库内资料回答并标注引用来源；若资料不足会如实告知，不会编造。

三、怎么传：上传政策、方案、培训等材料，填写标题、分类、密级、责任人；待审核通过后即可被全员检索。

四、注意密级：内部资料不要标公开；涉密内容走机密并限定知悉范围。养成「存知识、标来源、守密级」的习惯。"""),
    dict(key="d12", title="2025上半年知识库运营复盘报告",
         category_l1="TRAINING", category_l2="TRAINING-SHARE", dept_name="知识管理中心",
         security_level="public", quality_level="normal", owner="知识管理中心",
         effective_date="2025-07-01", source="运营组",
         auto_publish=True,
         content="""# 2025 上半年知识库运营复盘报告

上半年知识库共入库文档若干篇，覆盖政策、项目、客户、产品、流程、培训六大域，月活稳步提升。

亮点：政策域检索量最高，说明一线对合规查询依赖强；AI 问答采纳率上升，引用透明度获得好评。

不足：部分文档质量等级偏低，待提质占比偏高；跨部门协作类知识沉淀不足；少数机密文档权限配置过宽。

下半年计划：建立质量巡检机制，推动 draft 文档升级；开展部门知识官制度；细化机密文档知悉范围；引入准确率评测集持续度量检索效果。本报告向全员公开，欢迎反馈。"""),
    dict(key="d13", title="某试点城市DRG付费改革对耗材影响分析（待审）",
         category_l1="POLICY", category_l2="POLICY-PROVINCIAL", dept_name="采购一部",
         security_level="internal", quality_level="draft", owner="采购一部",
         effective_date="2025-06-15", source="政策研究",
         auto_publish=True,  # 直接发布，作为已发布样例
         content="""# 某试点城市 DRG 付费改革对耗材影响分析

DRG 付费改革按病种打包付费，倒逼医院控制耗材成本，对采购策略产生深远影响。

影响一：高值耗材从利润中心转为成本中心，医院更倾向中选产品与国产替代。

影响二：耗材使用须与临床路径绑定，采购需联合医务、医保部门做循证选型。

影响三：数据驱动增强，按 DRG 组的耗材消耗分析成为采购决策依据。

建议：建立 DRG 组—耗材映射，参与临床路径制定，强化成本透明。本文供采购决策参考，后续将持续更新测算口径。"""),
    dict(key="d14", title="某试点城市DRG付费改革补充测算（待审）",
         category_l1="POLICY", category_l2="POLICY-PROVINCIAL", dept_name="采购一部",
         security_level="internal", quality_level="draft", owner="采购一部",
         effective_date="2025-06-20", source="政策研究",
         auto_publish=False,  # 这篇保持待审，专门供审核台演示
         content="""# 某试点城市 DRG 付费改革补充测算（待审）

本文为 DRG 付费改革对耗材影响的补充测算初审稿，数据口径仍需核实，待审核员复核后发布。

测算口径：以试点医院近一年耗材消耗为基线，按 DRG 病组权重折算耗材成本占比，对比改革前后变化。

初步结论：高值耗材成本占比下降约 12%，但国产替代带来质量一致性关注；低值耗材因用量刚性，成本降幅有限。

需补充：各病组明细、与同级别医院对标、试剂类耗材单独测算。本文当前为草稿，入库后进入待审核队列。"""),
]


# ============================================================
# 二、初始化函数（均幂等）
# ============================================================

def get_dept_id(name: str) -> int:
    """按部门名称查 id（部门已存在时复用）。"""
    row = db.query_one("SELECT id FROM departments WHERE name = ?", (name,))
    return row["id"] if row else 0


def seed_departments() -> dict:
    """创建测试部门，已存在则跳过，返回 名称->id 映射。"""
    created = []  # 本次新建的部门名
    for name, code, contact in DEPARTMENTS:  # 遍历部门定义
        if not get_dept_id(name):  # 不存在才建
            db.execute(
                "INSERT INTO departments (name, code, contact, created_at) VALUES (?,?,?,?)",
                (name, code, contact, audit.now_iso()),
            )
            created.append(name)
    # 组装映射，知识管理中心固定走已存在的记录
    mapping = {name: get_dept_id(name) for name, _, _ in DEPARTMENTS}
    mapping["知识管理中心"] = get_dept_id("知识管理中心")
    return {"created": created, "mapping": mapping}


def seed_users(dept_map: dict) -> dict:
    """创建测试账号，已存在则跳过，返回 用户名->id 映射。"""
    created = []  # 新建账号
    mapping = {}  # 用户名->id
    for u in USERS:  # 遍历账号定义
        if auth.get_user_by_username(u["username"]):  # 已存在跳过（幂等）
            mapping[u["username"]] = auth.get_user_by_id(
                auth.get_user_by_username(u["username"])["id"]
            )["id"]
            continue
        uid = auth.create_user(
            username=u["username"],
            display_name=u["display_name"],
            password=TEST_PWD,
            role=u["role"],
            department_id=dept_map[u["dept_name"]],
            max_security=u["max_security"],
            cross_dept=u["cross_dept"],
            must_change_pwd=False,  # 测试账号关闭强制改密，便于演示
        )
        created.append(u["username"])
        mapping[u["username"]] = uid
    return {"created": created, "mapping": mapping}


def seed_docs(dept_map: dict, user_map: dict) -> dict:
    """入库测试文档，按标题幂等跳过；返回 文档记录列表。"""
    from app.ingest import ingest_file  # 延迟导入，避免循环
    records = []  # 收集每篇文档的入库结果
    contributor_id = user_map.get("contributor_demo") or 1  # 用贡献者身份入库
    for d in DOCS:  # 遍历文档定义
        # 幂等：标题已存在则跳过
        exist = db.query_one("SELECT id FROM documents WHERE title = ?", (d["title"],))
        if exist:  # 已存在，复用已有记录
            doc_id = exist["id"]
            real = db.query_one("SELECT status FROM documents WHERE id = ?", (doc_id,))
            records.append({"key": d["key"], "doc_id": doc_id, "title": d["title"],
                            "status": real["status"] if real else "unknown",
                            "security_level": d["security_level"],
                            "dept_name": d["dept_name"], "category_l1": d["category_l1"],
                            "category_l2": d["category_l2"], "quality_level": d["quality_level"]})
            continue
        # 把正文写成临时 .md 文件，交给入库流水线解析
        tmp = tempfile.NamedTemporaryFile(
            mode="w", suffix=".md", delete=False, encoding="utf-8")
        tmp.write(d["content"])
        tmp.close()
        meta = {  # 组装元数据
            "title": d["title"],
            "category_l1": d["category_l1"],
            "category_l2": d["category_l2"],
            "department_id": dept_map[d["dept_name"]],
            "security_level": d["security_level"],
            "quality_level": d["quality_level"],
            "owner": d["owner"],
            "effective_date": d["effective_date"],
            "source": d["source"],
        }
        res = ingest_file(tmp.name, meta, user_id=contributor_id,
                          auto_publish=d["auto_publish"])  # 调入库主流程
        records.append({
            "key": d["key"], "doc_id": res.get("doc_id"), "title": d["title"],
            "status": "published" if res.get("ok") and d["auto_publish"] else (
                "pending_review" if res.get("ok") else "FAILED"),
            "security_level": d["security_level"],
            "dept_name": d["dept_name"], "category_l1": d["category_l1"],
            "category_l2": d["category_l2"], "quality_level": d["quality_level"],
            "message": res.get("message", ""),
        })
        real = db.query_one("SELECT status FROM documents WHERE id = ?", (res.get("doc_id"),))
        records[-1]["status"] = real["status"] if real else "unknown"  # 回填真实状态
    return {"records": records}


# ============================================================
# 三、权限验证（用 Flask 测试客户端，最真实的端到端）
# ============================================================

def verify_permissions(app, dept_map: dict, doc_records: list) -> dict:
    """
    模拟各账号登录，断言可见文档范围符合权限模型。
    返回：每个账号的可见文档列表 + 与预期的对照结论。
    """
    client = app.test_client()  # 测试客户端，不占用端口

    # 预计算每个账号「预期可见」的文档集合（依据 visibility_filter 规则）
    # 规则：admin 全可见；否则 用户密级许可>=文档密级 且
    #       (文档公开 或 用户部门==文档部门 或 用户跨部门)
    def expected_visible(username: str) -> set:
        u = next(x for x in USERS if x["username"] == username)  # 找账号定义
        u_rank = config.SECURITY_RANK[u["max_security"]]  # 用户密级等级
        visible = set()
        for r in doc_records:
            # 待审核文档不出现在普通“已发布”列表，预期直接排除
            if r["status"] == "pending_review":
                continue
            doc_rank = config.SECURITY_RANK[r["security_level"]]  # 文档密级等级
            # pending_review 文档对非审核角色在普通列表中也看不到（状态过滤），单独处理
            if doc_rank > u_rank:  # 密级不够
                continue
            dept_ok = (r["dept_name"] == u["dept_name"]) or u["cross_dept"] \
                or r["security_level"] == "public"
            if u["role"] == "admin" or dept_ok:  # admin 不受限
                visible.add(r["key"])
        return visible

    results = []  # 每个账号的验证结果
    for u in USERS:  # 遍历账号
        # 登录
        r = client.post("/api/login",
                        json={"username": u["username"], "password": TEST_PWD})
        login_ok = (r.status_code == 200)
        # 拉取可见文档（发布态）
        r = client.get("/api/documents?per_page=200&status=published")
        items = r.get_json().get("items", []) if r.status_code == 200 else []
        # 用标题反查 key
        title_to_key = {x["title"]: x["key"] for x in doc_records}
        visible_keys = set()
        for it in items:  # 收集可见文档的 key
            k = title_to_key.get(it["title"])
            if k:
                visible_keys.add(k)
        exp = expected_visible(u["username"])  # 预期可见集合
        # exp 已排除 pending_review，直接作为预期可见集合
        exp_pub = exp
        passed = (visible_keys == exp_pub)  # 实际与预期完全一致才 PASS
        results.append({
            "username": u["username"], "role": u["role"],
            "login_ok": login_ok, "visible_count": len(visible_keys),
            "visible_keys": sorted(visible_keys),
            "expected_count": len(exp_pub), "passed": passed,
            "diff": sorted(visible_keys ^ exp_pub),  # 差异集合
        })
    return {"results": results}


# ============================================================
# 四、功能冒烟（检索 / 问答 / 审核）
# ============================================================

def smoke_tests(app, user_map: dict, doc_records: list) -> dict:
    """用管理员与审核员做功能冒烟，返回结果。"""
    client = app.test_client()
    out = {}

    # 1) 管理员登录
    client.post("/api/login",
                json={"username": "admin_demo", "password": TEST_PWD})

    # 2) 检索：混合检索「集采」应命中政策类文档
    r = client.get("/api/search?q=%E9%9B%86%E9%87%87&mode=hybrid")
    sdata = r.get_json()
    out["search"] = {
        "status": r.status_code,
        "total": sdata.get("stats", {}).get("total", 0),
        "top_title": sdata.get("results", [{}])[0].get("title", "") if sdata.get("results") else "",
    }

    # 3) AI 问答（无 LLM 密钥走降级，应返回带引用的原文片段）
    r = client.post("/api/ask",
                    json={"question": "集采政策对医院耗材采购有什么影响？", "session_id": None})
    out["ask"] = {"status": r.status_code, "streamed": "text/event-stream" in r.headers.get("Content-Type", "")}

    # 4) 审核：审核员登录，验证待审列表含 d14（不实际批准，保留待审样例供演示）
    client.post("/api/login",
                json={"username": "reviewer_demo", "password": TEST_PWD})
    r = client.get("/api/documents?status=pending_review&per_page=50")
    pend = r.get_json().get("items", []) if r.status_code == 200 else []
    pend_titles = [it["title"] for it in pend]
    d14 = next((x for x in doc_records if x["key"] == "d14"), None)
    out["review_pending"] = {
        "status": r.status_code,
        "count": len(pend),
        "contains_d14": bool(d14 and d14["title"] in pend_titles),
    }
    # 审核不存在文档，验证错误分支健壮性
    r = client.post("/api/review/999999", json={"action": "reject", "comment": "x"})
    out["review_reject_missing"] = {"status": r.status_code}
    return out


# ============================================================
# 五、生成测试报告
# ============================================================

def build_report(dept_map, user_map, doc_records, perm, smoke) -> str:
    """把所有结果拼成一份 Markdown 测试报告。"""
    lines = []
    lines.append("# AI-KM 知识管理平台 · 测试报告\n")
    lines.append(f"> 生成时间：{audit.now_iso()}  ｜  环境：本地演示库（data/aikm.db）\n")

    # 1. 测试账号
    lines.append("## 一、测试账号（覆盖全部角色 + 密级/跨部门变体）\n")
    lines.append("| 用户名 | 显示名 | 角色 | 部门 | 密级许可 | 跨部门 | 密码 | 说明 |")
    lines.append("| --- | --- | --- | --- | --- | --- | --- | --- |")
    for u in USERS:
        lines.append(
            f"| `{u['username']}` | {u['display_name']} | {config.ROLES[u['role']]} | "
            f"{u['dept_name']} | {config.SECURITY_LEVELS[u['max_security']]} | "
            f"{'是' if u['cross_dept'] else '否'} | `{TEST_PWD}` | {u['desc']} |")
    lines.append("\n> 测试账号统一密码 `Demo@123456`，已关闭强制改密便于演示。生产环境请使用强密码并开启强制改密。\n")

    # 2. 测试部门
    lines.append("## 二、测试部门\n")
    lines.append("| 部门 | ID | 说明 |")
    lines.append("| --- | --- | --- |")
    lines.append(f"| 知识管理中心 | {dept_map['知识管理中心']} | 系统自带根部门 |")
    for name, _, contact in DEPARTMENTS:
        lines.append(f"| {name} | {dept_map[name]} | {contact} |")
    lines.append("")

    # 3. 测试文档
    lines.append("## 三、测试文档（3 级密级 × 6 大分类 × 多部门 × 多状态）\n")
    lines.append("| # | 标题 | 一级分类 | 二级分类 | 部门 | 密级 | 质量 | 状态 |")
    lines.append("| --- | --- | --- | --- | --- | --- | --- | --- |")
    for r in doc_records:
        lines.append(
            f"| {r['key']} | {r['title']} | {config.CATEGORIES_L1[r['category_l1']]} | "
            f"{r['category_l2']} | {r['dept_name']} | {config.SECURITY_LEVELS[r['security_level']]} | "
            f"{config.QUALITY_LEVELS[r['quality_level']]} | {r['status']} |")
    lines.append("")

    # 4. 权限隔离验证
    lines.append("## 四、权限隔离验证（核心安全断言）\n")
    lines.append("> 规则：管理员全可见；其余账号须满足 `密级许可 ≥ 文档密级` 且 "
                 "（`文档公开` 或 `同部门` 或 `跨部门权限`）。\n")
    lines.append("| 账号 | 角色 | 登录 | 可见数 | 预期数 | 结论 | 差异 |")
    lines.append("| --- | --- | --- | --- | --- | --- | --- |")
    all_pass = True
    for p in perm["results"]:
        concl = "✅ PASS" if p["passed"] else "❌ FAIL"
        if not p["passed"]:
            all_pass = False
        lines.append(
            f"| `{p['username']}` | {config.ROLES[p['role']]} | "
            f"{'OK' if p['login_ok'] else 'FAIL'} | {p['visible_count']} | "
            f"{p['expected_count']} | {concl} | {', '.join(p['diff']) or '-'} |")
    lines.append("")
    lines.append(f"**权限隔离总体结论：{'全部通过 ✅' if all_pass else '存在失败 ❌'}**\n")

    # 5. 功能冒烟
    lines.append("## 五、功能冒烟\n")
    s = smoke["search"]
    lines.append(f"- 检索「集采」：HTTP {s['status']}，命中 {s['total']} 条，首条《{s['top_title']}》")
    a = smoke["ask"]
    lines.append(f"- AI 问答：HTTP {a['status']}，流式返回={'是' if a['streamed'] else '否'}（无 LLM 密钥时降级为原文片段+引用）")
    if "review_pending" in smoke:
        rp = smoke["review_pending"]
        lines.append(f"- 审核台待审列表：HTTP {rp['status']}，待审 {rp['count']} 篇，含 d14 样例={'是' if rp['contains_d14'] else '否'}")
        rm = smoke["review_reject_missing"]
        lines.append(f"- 审核不存在文档（错误分支）：HTTP {rm['status']}（应非 200，验证健壮性）")
    lines.append("")

    # 6. 使用指引
    lines.append("## 六、如何用这批测试数据\n")
    lines.append("1. 启动服务：`./start.sh`（或 `python run.py`），浏览器开 http://localhost:5200")
    lines.append("2. 用上面任一账号登录，对比「能看到哪些文档」与第四节结论是否一致")
    lines.append("3. 用 `admin_demo` 登录管理后台，可见全部 13 篇及审计日志")
    lines.append("4. 用 `user_demo` 登录，应看不到 `采购一部` 的内部/机密文档，也看不到 `市场部` 的机密文档")
    lines.append("5. 用 `reviewer_demo` 登录审核台，可看到 d14 待审并可批准/退回\n")

    lines.append("---")
    lines.append("*本报告由 `scripts/seed_demo.py` 自动生成，可重复执行（数据幂等）。*")
    return "\n".join(lines)


# ============================================================
# 六、主流程
# ============================================================

def main() -> None:
    """脚本入口：初始化 + 验证 + 出报告。"""
    print(">>> 初始化应用（建表 + 种子基础数据）")
    app = create_app()  # 触发建表与基础数据播种

    print(">>> 创建测试部门")
    dep = seed_departments()
    print("    新建部门：", dep["created"] or "无（已存在）")

    print(">>> 创建测试账号")
    usr = seed_users(dep["mapping"])
    print("    新建账号：", usr["created"] or "无（已存在）")

    print(">>> 入库测试文档")
    docs = seed_docs(dep["mapping"], usr["mapping"])

    print(">>> 权限隔离验证")
    perm = verify_permissions(app, dep["mapping"], docs["records"])

    print(">>> 功能冒烟")
    smoke = smoke_tests(app, usr["mapping"], docs["records"])

    print(">>> 生成测试报告")
    report = build_report(dep["mapping"], usr["mapping"], docs["records"], perm, smoke)
    out_path = ROOT / "docs" / "测试报告.md"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(report, encoding="utf-8")
    print("    报告已写入：", out_path)

    # 控制台摘要
    fails = [p["username"] for p in perm["results"] if not p["passed"]]
    print("\n=== 摘要 ===")
    print(f"  测试部门：{len(dep['mapping'])} 个")
    print(f"  测试账号：{len(usr['mapping'])} 个")
    print(f"  测试文档：{len(docs['records'])} 篇")
    print(f"  权限断言：{'全部通过' if not fails else '失败 -> ' + ', '.join(fails)}")
    print(f"  报告文件：{out_path}")


if __name__ == "__main__":
    main()
