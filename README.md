# 晓梦庄子 · AI-KM 知识管理平台

面向医药采购 AI 公司的企业知识资产化与 AI 生产力重构底层工具。
把分散的政策、项目、客户、产品、制度、培训知识统一入库、混合检索、可信问答，
支撑公司 SG1（知识资产化）与 SG2（AI 生产力重构）战略落地。

---

## 一、八个"可"对照

| 要求 | 落地方式 |
|------|----------|
| **可用** | 完整 Web 界面（检索/问答/入库/审核/看板/管理）+ JSON API，启动即可用 |
| **可维护** | 全模块逐行中文注释；配置中心 `config.py` 与业务代码解耦；清晰分层（db/auth/ingest/search/rag/providers/web） |
| **可落地** | 零额外重依赖，Flask 单文件部署；Docker Compose 一键起；本地先跑通再上内网 |
| **可拓展** | 模型双适配层（云↔私有化只改配置）；分类/密级/质量等级可运行时扩展；评测接口可插拔 |
| **可控** | RBAC 五角色 + 三级密级 + 部门隔离，权限在**数据层**过滤，无权限数据查不出 |
| **可审计** | 审计日志只增不改（`audit.py` 无删除接口），覆盖 22 类操作，可导出 CSV |
| **安全可靠** | PBKDF2 加盐哈希、参数化 SQL 防注入、文件类型白名单、路径穿越消毒、API Key 哈希存储 |
| **易用** | 入库即按《入库规范》门禁校验并给出"说人话"的错误；检索高亮；问答带可点击引用溯源 |

---

## 二、核心能力

- **混合检索**：BM25（FTS5 + 结巴分词）+ 向量语义检索 + RRF 倒数排名融合 + 质量加权
- **RAG 可信问答**：检索→组装上下文→LLM 生成→引用溯源；三条红线（有据可依 / 必须溯源 / 无则拒答）
- **规范化入库**：docx/pdf/xlsx/md/html/txt 解析 → 质量红线校验 → 语义切片 → 向量化 → 待审核
- **权限与审计**：五角色 RBAC、三级密级、部门隔离；全操作留痕、涉敏可一键筛选
- **效能看板**：检索量、零结果率（知识缺口）、热门词、趋势，反推该补什么知识
- **客观评测**：用「金标准」评测集度量 Recall@K / MRR，证明"AI 检索准确率 ≥ 90%"（SRS 核心 KPI）

---

## 三、快速开始

### 方式一：本地运行（先本地跑通）

```bash
# 1. 准备虚拟环境并安装依赖（已用清华镜像验证）
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple

# 2. 复制并填写环境变量（密钥等）
cp .env.example .env
#   编辑 .env：填入 AIKM_LLM_API_KEY / AIKM_EMBED_API_KEY
#   本地无密钥也可跑：EMBED_PROVIDER 设为 hash 启用本地降级语义检索

# 3. 启动
./start.sh            # 自动选装好依赖的 Python；优先 gunicorn，无则退回 Flask 开发服务器
#   或：python run.py
#   默认访问 http://localhost:5200
#   默认管理员：admin / Admin@123456（首次登录强制改密）
```

> 本地无外网/无密钥时：BM25 关键词检索始终可用；向量检索会因密钥无效而**优雅降级**
> （系统返回"仅关键词检索可用"，不影响其余功能）。设置 `AIKM_EMBED_PROVIDER=hash` 可启用本地语义检索。

### 方式二：Docker 一键部署（生产推荐，上内网服务器）

```bash
cp .env.example .env
# 编辑 .env：至少把 AIKM_SECRET_KEY 改成随机串（python -c "import secrets;print(secrets.token_hex(32))"）
#           无外网密钥则设 AIKM_EMBED_PROVIDER=hash 走本地离线检索
docker compose up -d --build
docker compose ps     # 确认 aikm 状态 healthy
# 访问 http://<服务器IP>:5200
```

生产镜像要点：gunicorn 多线程服务器（非 Flask 开发服务器）、容器内非 root 用户运行、
内置 `/login` 健康检查、数据用命名卷持久化。详细步骤、HTTPS 反代、备份升级与排障见
**[docs/部署与运维手册.md](docs/部署与运维手册.md)**。

---

## 四、演示评测闭环（证明准确率 ≥ 90%）

```bash
# 1. 启动服务并登录后，入库示例政策文档（也可在 Web「知识入库」页操作）
#    上传 evalset/sample_policy.md，元数据参考 docs/templates/metadata_template.csv
# 2. 导入评测用例
python scripts/import_eval.py
# 3. 运行评测（混合检索 / K=5）
python scripts/run_eval.py --mode hybrid --k 5
#   输出示例：Recall@5 = 1.0  ✅ 达标 (≥90%)
```

也可在 Web「管理后台 → 检索评测」页点「运行评测」查看结果。

---

## 五、目录结构

```
AI-KM/
├── run.py                  # 启动入口（含 .env 预加载）
├── app/
│   ├── __init__.py         # 应用工厂 create_app + 初始数据播种
│   ├── config.py           # 配置中心（所有可调参数，支持环境变量覆盖）
│   ├── db.py               # SQLite 表结构 + 线程安全连接 + 参数化查询
│   ├── auth.py             # 认证/会话/RBAC/数据层权限过滤/API Key
│   ├── audit.py            # 审计日志（只增不改）
│   ├── rag.py              # RAG 问答编排（检索→生成→溯源→拒答）
│   ├── eval.py             # 检索准确率评测（Recall@K / MRR）
│   ├── ingest/             # 解析(docx/pdf/xlsx/md/html/txt)→切片→入库流水线
│   ├── search/             # 结巴分词 + 混合检索引擎（BM25+向量+RRF）
│   ├── providers/          # 模型双适配层（LLM / Embedding：云↔私有）
│   └── web/                # 蓝图：页面路由 + JSON API + 模板 + 静态资源
├── docs/                   # SRS 需求规格说明书 + 知识分类与入库规范
├── evalset/                # 示例评测集与可入库示例文档
├── scripts/                # init_data / import_eval / run_eval
├── docker-compose.yml      # 一键部署
├── Dockerfile
└── requirements.txt
```

---

## 六、切换私有化部署（10 月）

业务代码零改动，仅改 `.env`：

```ini
AIKM_LLM_PROVIDER=ollama
AIKM_LLM_BASE_URL=http://localhost:11434/v1
AIKM_LLM_API_KEY=          # Ollama 无需密钥
AIKM_EMBED_PROVIDER=ollama
AIKM_EMBED_MODEL=bge-m3
```

> 切换 Embedding 模型后必须「管理后台 → 索引管理 → 全量重建」，否则新旧向量混用导致检索错乱。

---

## 七、主要 API 速查

| 方法 | 路径 | 说明 |
|------|------|------|
| POST | `/api/login` | 登录 |
| GET  | `/api/search?q=&mode=` | 混合/关键词/语义检索 |
| POST | `/api/ask` | 流式问答（SSE），支持 `session_id` 多轮 |
| POST | `/api/upload` | 单文件入库（表单：file + 元数据） |
| POST | `/api/review/<id>` | 审核通过/退回 |
| GET  | `/api/documents` | 文档列表（权限过滤 + 分页） |
| GET  | `/api/dashboard/stats` | 效能看板数据 |
| GET/POST | `/api/admin/*` | 用户/密钥/审计/评测/索引/配置（仅管理员） |

> 所有 `/api/*` 同时支持浏览器会话与 `Authorization: Bearer <API Key>` 调用，供 SG2 上层 AI 场景集成。
