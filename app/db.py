# -*- coding: utf-8 -*-
"""
数据库访问层
============
职责：
1. 定义完整的 SQLite 表结构（含 FTS5 全文索引）
2. 提供线程安全的连接管理
3. 提供参数化查询的统一入口（杜绝 SQL 注入）

设计说明：
- 选用 SQLite 而非 PostgreSQL/MySQL，是因为内网单机部署零运维、单文件备份，万级文档性能完全够用。
- 所有 SQL 一律使用 ? 占位符参数化，绝不做字符串拼接（NFR-7 安全要求）。
- 数据访问集中在本层，未来要换 PostgreSQL 只需替换本文件的实现。
"""

import sqlite3  # Python 标准库自带的 SQLite 驱动，无需额外安装
import threading  # 线程模块，用于实现每线程独立数据库连接
from datetime import datetime, timedelta  # 处理编辑锁超时计算
from typing import Any, Iterable, Optional  # 类型注解，提升代码可读性

from . import config  # 导入同包下的配置模块

# _local 是线程本地存储对象。SQLite 连接对象不能跨线程共享，
# 所以给每个线程分配一个独立连接，避免 "SQLite objects created in a thread..." 报错
_local = threading.local()


# ============================================================
# 一、数据库表结构定义（DDL）
# ============================================================

# SCHEMA_SQL 是完整的建表语句。使用 IF NOT EXISTS 保证可重复执行（幂等）
SCHEMA_SQL = """
-- ------------------------------------------------------------
-- 部门表：知识资产按部门归属，是权限隔离的基础单元
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS departments (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,   -- 部门主键，自增
    name        TEXT NOT NULL UNIQUE,                -- 部门名称，全局唯一
    code        TEXT,                                -- 部门编码，便于外部系统对接
    contact     TEXT,                                -- 部门知识库联络人姓名（SG1 要求各部门指定）
    created_at  TEXT NOT NULL                        -- 创建时间，ISO8601 字符串
);

-- ------------------------------------------------------------
-- 用户表：账号体系与权限载体
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS users (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,  -- 用户主键
    username        TEXT NOT NULL UNIQUE,               -- 登录名，全局唯一
    display_name    TEXT NOT NULL,                      -- 显示名（真实姓名）
    password_hash   TEXT NOT NULL,                      -- 密码哈希（PBKDF2-SHA256），绝不存明文
    password_salt   TEXT NOT NULL,                      -- 每用户独立的随机盐值，防彩虹表攻击
    role            TEXT NOT NULL DEFAULT 'user',       -- 角色：admin/reviewer/contributor/user
    department_id   INTEGER,                            -- 所属部门
    max_security    TEXT NOT NULL DEFAULT 'internal',   -- 密级许可上限：最高能看到哪一级密级的文档
    cross_dept      INTEGER NOT NULL DEFAULT 0,         -- 是否可跨部门查看，1=可以（管理层/审核员）
    active          INTEGER NOT NULL DEFAULT 1,         -- 账号是否启用，0=已停用（停用而非删除，保留审计痕迹）
    must_change_pwd INTEGER NOT NULL DEFAULT 0,         -- 是否强制修改密码，1=首次登录必须改（FR-1.6）
    last_login_at   TEXT,                               -- 最后登录时间
    created_at      TEXT NOT NULL,                      -- 创建时间
    -- 以下三列为 P2 MFA（多因子认证）扩展，老库升级时由 _migrate_columns 补列
    mfa_secret      TEXT,                               -- TOTP 密钥（Base32），开启 MFA 后才有
    mfa_enabled     INTEGER NOT NULL DEFAULT 0,          -- 是否已开启 MFA，1=开启
    mfa_backup      TEXT,                               -- 备用码列表（JSON 数组），用于手机丢失时紧急登录
    FOREIGN KEY (department_id) REFERENCES departments(id)  -- 外键关联部门表
);

-- ------------------------------------------------------------
-- 文档主表：知识资产的元数据核心
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS documents (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,  -- 文档主键
    title           TEXT NOT NULL,                      -- 标题（必填，规范 3.2 命名法）
    category_l1     TEXT NOT NULL,                      -- 一级分类（必填，六大域之一）
    category_l2     TEXT,                               -- 二级分类（可选）
    department_id   INTEGER NOT NULL,                   -- 归属部门（必填，决定可见范围）
    space_id        INTEGER,                            -- 所属知识树节点（书架/书/章），可空，实现 BookStack 式层级组织
    security_level  TEXT NOT NULL DEFAULT 'internal',   -- 密级：public/internal/confidential
    quality_level   TEXT NOT NULL DEFAULT 'normal',     -- 质量等级：verified/normal/draft
    owner           TEXT NOT NULL,                      -- 内容责任人（必填，复审通知对象）
    effective_date  TEXT,                               -- 生效日期 YYYY-MM-DD
    expire_date     TEXT,                               -- 失效日期，到期触发复审提醒
    next_review_at  TEXT,                               -- 下次复审日期，按分类周期自动计算
    source          TEXT,                               -- 来源（机构名或 URL）
    summary         TEXT,                               -- 摘要（可自动生成）
    status          TEXT NOT NULL DEFAULT 'uploaded',   -- 状态机当前状态
    reject_reason   TEXT,                               -- 审核退回理由
    content_hash    TEXT,                               -- 正文内容的 SHA256 哈希，用于重复检测
    file_path       TEXT,                               -- 原始文件的存储路径
    file_name       TEXT,                               -- 原始文件名
    file_size       INTEGER DEFAULT 0,                  -- 文件字节数
    file_ext        TEXT,                               -- 文件扩展名
    content_length  INTEGER DEFAULT 0,                  -- 提取出的正文字数
    chunk_count     INTEGER DEFAULT 0,                  -- 切片数量
    body_text       TEXT,                               -- 正文权威源（Markdown/纯文本），在线编辑器读写它；首次入库时存解析出的全文
    body_html       TEXT,                               -- 正文富文本 HTML（Quill 产出，经服务端净化），用于编辑器回显与文档展示；为空时回退按切片渲染
    version         INTEGER NOT NULL DEFAULT 1,         -- 版本号，更新时递增
    parse_error     TEXT,                               -- 解析失败时的错误信息
    created_by      INTEGER,                            -- 上传人用户 ID
    reviewed_by     INTEGER,                            -- 审核人用户 ID
    reviewed_at     TEXT,                               -- 审核时间
    created_at      TEXT NOT NULL,                      -- 创建时间
    updated_at      TEXT NOT NULL,                      -- 最后更新时间
    -- 以下四列为「并发编辑锁 + 文档级细粒度可见权限」扩展（P 完整版补齐）
    edit_lock_user  INTEGER,                            -- 并发编辑锁：当前持有锁的用户 ID（NULL=无锁），用于防止多人同时编辑互相覆盖
    edit_lock_at    TEXT,                               -- 并发编辑锁获取时间（ISO8601），超过 TTL 自动失效（前端心跳续期）
    edit_lock_name  TEXT,                               -- 并发编辑锁持有者显示名快照，前端提示"谁正在编辑"用
    visibility_mode TEXT NOT NULL DEFAULT 'inherit',   -- 文档级可见权限模式：inherit=继承全局规则 / restricted=走 doc_acl 细粒度白名单控制
    FOREIGN KEY (department_id) REFERENCES departments(id),
    FOREIGN KEY (created_by) REFERENCES users(id)
);

-- 为高频过滤字段建立索引，加速检索时的权限过滤与条件筛选
CREATE INDEX IF NOT EXISTS idx_doc_status   ON documents(status);
CREATE INDEX IF NOT EXISTS idx_doc_cat      ON documents(category_l1);
CREATE INDEX IF NOT EXISTS idx_doc_dept     ON documents(department_id);
CREATE INDEX IF NOT EXISTS idx_doc_sec      ON documents(security_level);
CREATE INDEX IF NOT EXISTS idx_doc_hash     ON documents(content_hash);
CREATE INDEX IF NOT EXISTS idx_doc_review   ON documents(next_review_at);

-- ------------------------------------------------------------
-- 文档版本历史表：支撑"可回溯、可回滚"（FR-7.1）
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS doc_versions (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,  -- 版本记录主键
    doc_id      INTEGER NOT NULL,                   -- 关联的文档 ID
    version     INTEGER NOT NULL,                   -- 版本号
    title       TEXT,                               -- 该版本的标题快照
    file_path   TEXT,                               -- 该版本的文件路径
    content     TEXT,                               -- 该版本的正文快照（纯文本，用于版本 diff）
    body_html   TEXT,                               -- 该版本的富文本 HTML 快照（用于回滚后还原编辑器与展示）
    changed_by  INTEGER,                            -- 变更操作人
    change_note TEXT,                               -- 变更说明
    created_at  TEXT NOT NULL,                      -- 版本生成时间
    FOREIGN KEY (doc_id) REFERENCES documents(id)
);
CREATE INDEX IF NOT EXISTS idx_ver_doc ON doc_versions(doc_id);

-- ------------------------------------------------------------
-- 知识切片表：检索的最小单元
-- 说明：AI 检索不是检索整篇文档，而是检索文档中的片段，
-- 这样才能精确定位到"答案在哪一段"，也才能做到引用溯源。
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS chunks (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,  -- 切片主键
    doc_id        INTEGER NOT NULL,                   -- 所属文档
    seq           INTEGER NOT NULL,                   -- 在文档中的顺序号，从 0 开始
    heading_path  TEXT,                               -- 章节路径，如"第二章 采购规则 > 2.1 报价规则"
    anchor        TEXT,                               -- 段落锚点：基于章节路径生成的稳定锚 id，支持 #anchor 直达（P1 段落锚点）
    content       TEXT NOT NULL,                      -- 切片正文
    char_count    INTEGER DEFAULT 0,                  -- 字数
    created_at    TEXT NOT NULL,                      -- 创建时间
    FOREIGN KEY (doc_id) REFERENCES documents(id)
);
CREATE INDEX IF NOT EXISTS idx_chunk_doc ON chunks(doc_id);
-- 注意：idx_chunk_anchor（按 anchor 锚点定位）依赖 P1 才加的 anchor 列，
-- 对"已存在但不含 anchor 的老库"不能在静态脚本里建（会报 no such column），
-- 因此改由 _migrate_columns 在确认 anchor 列存在后创建（见下方迁移逻辑）。

-- ------------------------------------------------------------
-- 切片向量表：语义检索的数据基础
-- 说明：向量以 float32 二进制 blob 形式存储，比存 JSON 文本省 5 倍空间且读取快。
-- 单独建表而非放在 chunks 里，是为了切换 embedding 模型时可以整表清空重建，不影响正文。
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS chunk_vectors (
    chunk_id    INTEGER PRIMARY KEY,   -- 与 chunks.id 一一对应
    doc_id      INTEGER NOT NULL,      -- 冗余存一份文档 ID，方便按文档批量删除向量
    dim         INTEGER NOT NULL,      -- 向量维度，用于校验模型是否被换过
    model       TEXT,                  -- 生成该向量的模型名，便于排查索引不一致问题
    vector      BLOB NOT NULL,         -- float32 数组的原始字节
    created_at  TEXT NOT NULL,         -- 创建时间
    FOREIGN KEY (chunk_id) REFERENCES chunks(id)
);
CREATE INDEX IF NOT EXISTS idx_vec_doc ON chunk_vectors(doc_id);

-- ------------------------------------------------------------
-- FTS5 全文索引虚拟表：关键词检索（BM25）的数据基础
-- 说明：
-- 1. content='' 表示这是"外部内容表"模式，FTS5 只存索引不存原文，节省一半空间。
-- 2. tokenized 字段存的是 jieba 分词后用空格连接的文本。
--    SQLite 内置分词器不认识中文（会把整句当一个词），必须先用 jieba 切好再喂给它。
-- ------------------------------------------------------------
CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5(
    tokenized,                    -- 分词后的可检索文本
    chunk_id UNINDEXED,           -- 关联的切片 ID，UNINDEXED 表示不参与全文匹配只作为附加数据
    doc_id   UNINDEXED,           -- 关联的文档 ID
    tokenize = 'unicode61'        -- 使用 unicode61 分词器，配合预分词的空格切分即可正常工作
);

-- ------------------------------------------------------------
-- 标签表与关联表：补充检索维度
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS tags (
    id    INTEGER PRIMARY KEY AUTOINCREMENT,  -- 标签主键
    name  TEXT NOT NULL UNIQUE                -- 标签名，唯一
);
CREATE TABLE IF NOT EXISTS doc_tags (
    doc_id  INTEGER NOT NULL,                 -- 文档 ID
    tag_id  INTEGER NOT NULL,                 -- 标签 ID
    PRIMARY KEY (doc_id, tag_id)              -- 联合主键，防止重复打同一标签
);

-- ------------------------------------------------------------
-- 文档交叉引用表：记录文档之间的引用关系（P1 交叉引用）
-- 用户在正文中用 [[文档标题]] / [[doc:ID]] 语法引用其他文档，
-- 解析时把"谁引用了谁"落到本表，文档页即可展示"引用/被引用"双向关系。
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS doc_links (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,  -- 引用记录主键
    from_doc_id INTEGER NOT NULL,                   -- 引用方文档 ID
    to_doc_id   INTEGER NOT NULL,                   -- 被引用方文档 ID
    link_text   TEXT,                               -- 链接显示文本（[[目标|显示文本]] 中的显示文本）
    created_at  TEXT NOT NULL,                      -- 建立时间
    FOREIGN KEY (from_doc_id) REFERENCES documents(id),
    FOREIGN KEY (to_doc_id) REFERENCES documents(id)
);
CREATE INDEX IF NOT EXISTS idx_link_from ON doc_links(from_doc_id);
CREATE INDEX IF NOT EXISTS idx_link_to   ON doc_links(to_doc_id);

-- ------------------------------------------------------------
-- 文档评论表：页面级评论（P1 页面级评论）
-- 任何登录用户都可对可见文档发表评论，用于协作讨论；作者本人或管理员可删除。
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS doc_comments (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,  -- 评论主键
    doc_id      INTEGER NOT NULL,                   -- 所属文档
    user_id     INTEGER NOT NULL,                   -- 评论人
    content     TEXT NOT NULL,                      -- 评论内容
    created_at  TEXT NOT NULL,                      -- 评论时间
    FOREIGN KEY (doc_id) REFERENCES documents(id),
    FOREIGN KEY (user_id) REFERENCES users(id)
);
CREATE INDEX IF NOT EXISTS idx_comment_doc ON doc_comments(doc_id);  -- 按文档查评论

-- ------------------------------------------------------------
-- 文档内嵌图（diagrams.net）表：P2 内联画图
-- 用户在编辑器里画的流程图/架构图以 diagrams.net 的 XML 形式存这里，
-- 文档页按需渲染。一张文档可有多张图。
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS doc_diagrams (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,  -- 图主键
    doc_id      INTEGER NOT NULL,                   -- 所属文档
    title       TEXT NOT NULL DEFAULT '未命名图',    -- 图标题
    diagram_xml TEXT NOT NULL,                      -- diagrams.net 导出的 mxGraphModel XML（图的全部内容）
    created_by  INTEGER,                            -- 创建人
    created_at  TEXT NOT NULL,                      -- 创建时间
    updated_at  TEXT NOT NULL,                      -- 最后更新时间
    FOREIGN KEY (doc_id) REFERENCES documents(id)
);
CREATE INDEX IF NOT EXISTS idx_diagram_doc ON doc_diagrams(doc_id);  -- 按文档查图

-- ------------------------------------------------------------
-- 文档级细粒度可见权限表（P 完整版补齐）：BookStack 式每对象权限
-- 当 documents.visibility_mode='restricted' 时，本表决定"哪些主体能看/能编辑"。
-- principal_type 取值 'user'（指定用户）或 'role'（指定角色）；
-- perm 取值 'view'（仅看）或 'view_edit'（可看可编辑）。
-- 文档创建人(created_by)、管理员、审核员始终拥有访问权（见 auth.visibility_filter）。
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS doc_acl (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,  -- 权限记录主键
    doc_id        INTEGER NOT NULL,                   -- 关联文档 ID
    principal_type TEXT NOT NULL,                     -- 主体类型：user 用户 / role 角色
    principal_id  TEXT NOT NULL,                      -- 主体标识：user 时为用户 ID；role 时为角色名（admin/reviewer/...）
    perm          TEXT NOT NULL DEFAULT 'view',       -- 权限：view 仅查看 / view_edit 可看可编辑
    created_by    INTEGER,                            -- 授权人用户 ID
    created_at    TEXT NOT NULL,                      -- 授权时间
    FOREIGN KEY (doc_id) REFERENCES documents(id)
);
CREATE INDEX IF NOT EXISTS idx_acl_doc ON doc_acl(doc_id);          -- 按文档查权限
CREATE INDEX IF NOT EXISTS idx_acl_principal ON doc_acl(principal_type, principal_id);  -- 按主体查权限

-- ------------------------------------------------------------
-- 二级分类配置表：让分类可运行时扩展而不必改代码（NFR-4 可扩展）
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS categories_l2 (
    code        TEXT PRIMARY KEY,   -- 二级分类编码
    parent      TEXT NOT NULL,      -- 所属一级分类编码
    name        TEXT NOT NULL,      -- 中文名称
    sort_order  INTEGER DEFAULT 0,  -- 排序权重
    active      INTEGER DEFAULT 1   -- 是否启用
);

-- ------------------------------------------------------------
-- 知识树表：实现 BookStack 式层级组织（书架→书→章），自引用树
-- 文档通过 documents.space_id 挂到某个节点上，形成可嵌套的知识结构
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS knowledge_spaces (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,  -- 节点主键
    parent_id     INTEGER,                            -- 父节点 ID，NULL 表示顶层（书架）
    name          TEXT NOT NULL,                      -- 节点名称
    kind          TEXT NOT NULL DEFAULT 'shelf',      -- 类型：shelf 书架 / book 书籍 / chapter 章节
    department_id INTEGER,                            -- 归属部门（决定可见范围，可空=全公司可见）
    security_level TEXT NOT NULL DEFAULT 'internal',  -- 密级：public/internal/confidential
    description   TEXT,                               -- 节点说明
    sort_order    INTEGER DEFAULT 0,                  -- 排序权重，越小越靠前
    created_by    INTEGER,                            -- 创建人
    created_at    TEXT NOT NULL,                      -- 创建时间
    FOREIGN KEY (parent_id) REFERENCES knowledge_spaces(id),
    FOREIGN KEY (department_id) REFERENCES departments(id)
);
CREATE INDEX IF NOT EXISTS idx_space_parent ON knowledge_spaces(parent_id);
CREATE INDEX IF NOT EXISTS idx_space_dept   ON knowledge_spaces(department_id);

-- ------------------------------------------------------------
-- 审计日志表：只增不改（FR-11.3）
-- 本表在应用层没有任何 UPDATE / DELETE 接口，是合规审计的可信底账。
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS audit_logs (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,  -- 日志主键
    ts           TEXT NOT NULL,                      -- 发生时间
    user_id      INTEGER,                            -- 操作人 ID，系统操作为空
    username     TEXT,                               -- 操作人用户名快照（用户改名后仍可追溯）
    ip           TEXT,                               -- 来源 IP
    action       TEXT NOT NULL,                      -- 动作类型，如 login/search/chat/upload
    target_type  TEXT,                               -- 操作对象类型，如 document/user
    target_id    TEXT,                               -- 操作对象 ID
    detail       TEXT,                               -- 详细内容（JSON 字符串）
    result       TEXT,                               -- 结果：success / fail / denied
    cost_ms      INTEGER DEFAULT 0,                  -- 耗时毫秒，用于性能分析
    sensitive    INTEGER DEFAULT 0                   -- 是否涉及机密文档，1=是（便于一键筛选，FR-11.5）
);
CREATE INDEX IF NOT EXISTS idx_audit_ts     ON audit_logs(ts);
CREATE INDEX IF NOT EXISTS idx_audit_user   ON audit_logs(user_id);
CREATE INDEX IF NOT EXISTS idx_audit_action ON audit_logs(action);
CREATE INDEX IF NOT EXISTS idx_audit_sens   ON audit_logs(sensitive);

-- ------------------------------------------------------------
-- API 密钥表：供 SG2 上层 AI 场景调用（FR-8.4）
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS api_keys (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,  -- 主键
    name         TEXT NOT NULL,                      -- 密钥用途名称，如"政策解读场景"
    key_hash     TEXT NOT NULL UNIQUE,               -- 密钥的哈希值，明文只在创建时显示一次
    key_prefix   TEXT NOT NULL,                      -- 密钥前缀，用于界面识别（如 aikm_a1b2）
    bind_user_id INTEGER,                            -- 绑定的用户身份，决定该 Key 的数据可见范围
    active       INTEGER NOT NULL DEFAULT 1,         -- 是否有效，0=已吊销
    call_count   INTEGER NOT NULL DEFAULT 0,         -- 累计调用次数
    last_used_at TEXT,                               -- 最后调用时间
    created_at   TEXT NOT NULL                       -- 创建时间
);

-- ------------------------------------------------------------
-- 评测用例表：支撑"检索准确率 ≥ 90%"的客观证明（FR-6）
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS eval_cases (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,  -- 用例主键
    question      TEXT NOT NULL,                      -- 测试问题
    expect_doc_ids TEXT,                              -- 期望命中的文档 ID 列表，逗号分隔（金标准）
    expect_keywords TEXT,                             -- 期望答案中包含的关键词，逗号分隔
    category_l1   TEXT,                               -- 所属知识域，用于分域统计
    case_type     TEXT DEFAULT 'normal',              -- 用例类型：normal 常规 / paraphrase 同义改写 / outside 库外问题
    note          TEXT,                               -- 备注
    active        INTEGER DEFAULT 1,                  -- 是否启用
    created_at    TEXT NOT NULL                       -- 创建时间
);

-- ------------------------------------------------------------
-- 评测运行记录表：每次跑分留档，可对比历史（FR-6.3）
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS eval_runs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,  -- 运行主键
    started_at  TEXT NOT NULL,                      -- 开始时间
    finished_at TEXT,                               -- 结束时间
    total       INTEGER DEFAULT 0,                  -- 用例总数
    passed      INTEGER DEFAULT 0,                  -- 通过数
    recall_at_k REAL DEFAULT 0,                     -- Recall@K 指标（核心 KPI）
    mrr         REAL DEFAULT 0,                     -- 平均倒数排名，衡量排序质量
    top_k       INTEGER DEFAULT 5,                  -- 本次评测使用的 K 值
    mode        TEXT,                               -- 检索模式：hybrid/bm25/vector
    config_snap TEXT,                               -- 当次配置快照（JSON），便于复现
    run_by      INTEGER                             -- 执行人
);

-- ------------------------------------------------------------
-- 评测明细表：定位具体哪些题没过
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS eval_results (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,  -- 明细主键
    run_id    INTEGER NOT NULL,                   -- 所属运行批次
    case_id   INTEGER NOT NULL,                   -- 用例 ID
    question  TEXT,                               -- 问题快照
    hit       INTEGER DEFAULT 0,                  -- 是否命中，1=命中
    rank      INTEGER DEFAULT 0,                  -- 命中位置排名，0 表示未命中
    got_docs  TEXT,                               -- 实际返回的文档 ID 列表
    FOREIGN KEY (run_id) REFERENCES eval_runs(id)
);
CREATE INDEX IF NOT EXISTS idx_evalres_run ON eval_results(run_id);

-- ------------------------------------------------------------
-- 会话与消息表：多轮对话（FR-5.6）
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS chat_sessions (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,  -- 会话主键
    user_id     INTEGER NOT NULL,                   -- 所属用户
    title       TEXT,                               -- 会话标题，取首个问题的前若干字
    created_at  TEXT NOT NULL,                      -- 创建时间
    updated_at  TEXT NOT NULL                       -- 最后活跃时间
);
CREATE TABLE IF NOT EXISTS chat_messages (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,  -- 消息主键
    session_id  INTEGER NOT NULL,                   -- 所属会话
    role        TEXT NOT NULL,                      -- 角色：user 用户 / assistant 助手
    content     TEXT NOT NULL,                      -- 消息内容
    citations   TEXT,                               -- 引用来源列表（JSON），实现答案溯源
    created_at  TEXT NOT NULL,                      -- 创建时间
    FOREIGN KEY (session_id) REFERENCES chat_sessions(id)
);
CREATE INDEX IF NOT EXISTS idx_msg_session ON chat_messages(session_id);

-- ------------------------------------------------------------
-- 问答反馈表：用户反馈沉淀为评测素材（FR-5.7）
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS feedback (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,  -- 反馈主键
    message_id  INTEGER,                            -- 关联的助手消息
    user_id     INTEGER,                            -- 反馈人
    question    TEXT,                               -- 问题快照
    answer      TEXT,                               -- 答案快照
    rating      TEXT,                               -- 评价：good 有用 / bad 无用 / wrong 有误
    comment     TEXT,                               -- 文字补充
    created_at  TEXT NOT NULL                       -- 创建时间
);

-- ------------------------------------------------------------
-- 检索日志表：支撑效能度量与知识缺口分析（FR-10.2、FR-10.3）
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS search_logs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,  -- 主键
    ts          TEXT NOT NULL,                      -- 检索时间
    user_id     INTEGER,                            -- 检索人
    query       TEXT NOT NULL,                      -- 查询词
    mode        TEXT,                               -- 检索模式
    result_count INTEGER DEFAULT 0,                 -- 返回结果数，0 表示零结果（知识缺口信号）
    cost_ms     INTEGER DEFAULT 0,                  -- 耗时
    source      TEXT DEFAULT 'web'                  -- 来源：web 界面 / api 接口
);
CREATE INDEX IF NOT EXISTS idx_slog_ts ON search_logs(ts);
CREATE INDEX IF NOT EXISTS idx_slog_cnt ON search_logs(result_count);

-- ------------------------------------------------------------
-- 系统配置表：运行时可改的配置（模型切换等），优先级高于环境变量
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS settings (
    key         TEXT PRIMARY KEY,  -- 配置键
    value       TEXT,              -- 配置值
    updated_at  TEXT,              -- 更新时间
    updated_by  INTEGER            -- 更新人
);
"""


# ============================================================
# 二、连接管理
# ============================================================

def get_conn() -> sqlite3.Connection:
    """
    获取当前线程的数据库连接。
    第一次调用时创建连接并做好各项设置，之后复用同一个连接对象。
    """
    # 检查当前线程是否已有连接；getattr 的第三个参数是默认值，避免属性不存在时抛异常
    conn = getattr(_local, "conn", None)
    if conn is None:  # 当前线程还没有连接，需要新建
        # 建立连接。check_same_thread=False 是必需的，因为 Flask 的请求可能在不同线程处理；
        # 我们通过线程本地存储保证了实际上不会跨线程共用，所以关闭这个检查是安全的
        conn = sqlite3.connect(
            str(config.DB_PATH),      # 数据库文件路径
            check_same_thread=False,  # 关闭线程检查（我们自己保证了线程隔离）
            timeout=30.0,             # 遇到数据库锁时最多等待 30 秒再报错
        )
        # 设置行工厂为 sqlite3.Row，这样查询结果可以像字典一样用列名访问（row["title"]）
        conn.row_factory = sqlite3.Row
        # 开启 WAL 日志模式：读写可以并发，大幅提升多用户同时使用时的性能
        conn.execute("PRAGMA journal_mode=WAL")
        # 开启外键约束（SQLite 默认是关闭的），保证数据引用完整性
        conn.execute("PRAGMA foreign_keys=ON")
        # 设置同步级别为 NORMAL：在 WAL 模式下兼顾安全与性能的推荐配置
        conn.execute("PRAGMA synchronous=NORMAL")
        # 把连接对象存到线程本地存储，供本线程后续复用
        _local.conn = conn
    return conn


def close_conn() -> None:
    """关闭当前线程的数据库连接，通常在应用退出或测试清理时调用。"""
    conn = getattr(_local, "conn", None)  # 取出当前线程的连接
    if conn is not None:  # 有连接才需要关闭
        conn.close()  # 关闭连接释放资源
        _local.conn = None  # 清空引用，下次调用 get_conn 会重新创建


def init_db() -> None:
    """
    初始化数据库：执行建表语句。
    因为所有 DDL 都带 IF NOT EXISTS，本函数可以安全地重复调用（幂等）。
    """
    conn = get_conn()  # 拿到数据库连接
    conn.executescript(SCHEMA_SQL)  # executescript 可以一次执行多条 SQL 语句
    conn.commit()  # 提交事务，让建表生效
    _migrate_columns()  # 给已存在表补新字段（老库升级用）


def _migrate_columns() -> None:
    """
    数据库迁移：给已存在的旧表补充新加的列。

    为什么需要它：
    SQLite 的 CREATE TABLE IF NOT EXISTS 对"已存在"的表不会自动加列，
    比如先部署旧版本（无 space_id），后升级代码（documents 加了 space_id），
    如果不做迁移，老库会一直缺这个字段，写入时直接报 SQLITE_ERROR。
    这里用 PRAGMA table_info 检查列是否存在，不存在才 ALTER ADD COLUMN。
    """
    conn = get_conn()  # 拿到连接
    # 检查 documents 表是否已有 space_id 列
    cols = [r[1] for r in conn.execute("PRAGMA table_info(documents)").fetchall()]
    if "space_id" not in cols:  # 老库缺列，补上
        conn.execute("ALTER TABLE documents ADD COLUMN space_id INTEGER")  # 加可为空的列
        conn.commit()  # 提交迁移
    # 补 body_text 列：正文权威源，在线编辑器直接读写，老库升级时必须补上
    if "body_text" not in cols:  # 老库缺列，补上
        conn.execute("ALTER TABLE documents ADD COLUMN body_text TEXT")  # 加可为空的文本列
        conn.commit()  # 提交迁移
    # 补 body_html 列：富文本 HTML 权威源（P 富文本编辑器升级）。老库升级时补上
    if "body_html" not in cols:  # 老库缺列，补上
        conn.execute("ALTER TABLE documents ADD COLUMN body_html TEXT")  # 加可为空的富文本列
        conn.commit()  # 提交迁移
    # 补 doc_versions.body_html 列：版本富文本 HTML 快照（回滚时还原用）。老库升级时补上
    ver_cols = [r[1] for r in conn.execute("PRAGMA table_info(doc_versions)").fetchall()]
    if "body_html" not in ver_cols:  # 老库缺列，补上
        conn.execute("ALTER TABLE doc_versions ADD COLUMN body_html TEXT")  # 加可为空的富文本快照列
        conn.commit()  # 提交迁移
    # 补 documents.edit_lock_user / edit_lock_at / edit_lock_name / visibility_mode 列：并发编辑锁 + 文档级细粒度权限（P 完整版）
    lock_cols = [r[1] for r in conn.execute("PRAGMA table_info(documents)").fetchall()]
    if "edit_lock_user" not in lock_cols:  # 老库缺列，补上（可空）
        conn.execute("ALTER TABLE documents ADD COLUMN edit_lock_user INTEGER")
        conn.commit()  # 提交迁移
    if "edit_lock_at" not in lock_cols:  # 缺列补可空
        conn.execute("ALTER TABLE documents ADD COLUMN edit_lock_at TEXT")
        conn.commit()  # 提交迁移
    if "edit_lock_name" not in lock_cols:  # 缺列补可空
        conn.execute("ALTER TABLE documents ADD COLUMN edit_lock_name TEXT")
        conn.commit()  # 提交迁移
    if "visibility_mode" not in lock_cols:  # 缺列补默认 inherit
        conn.execute("ALTER TABLE documents ADD COLUMN visibility_mode TEXT NOT NULL DEFAULT 'inherit'")
        conn.commit()  # 提交迁移
    # 补 doc_acl 表：文档级细粒度可见权限（P 完整版）。IF NOT EXISTS 幂等
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS doc_acl (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            doc_id        INTEGER NOT NULL,
            principal_type TEXT NOT NULL,
            principal_id  TEXT NOT NULL,
            perm          TEXT NOT NULL DEFAULT 'view',
            created_by    INTEGER,
            created_at    TEXT NOT NULL,
            FOREIGN KEY (doc_id) REFERENCES documents(id)
        )
        """
    )
    conn.commit()  # 提交迁移
    conn.execute("CREATE INDEX IF NOT EXISTS idx_acl_doc ON doc_acl(doc_id)")
    conn.commit()  # 提交迁移
    # 补 chunks.anchor 列：段落锚点（P1 段落锚点）。老库升级时补上，新库由建表语句自带
    if "anchor" not in [r[1] for r in conn.execute("PRAGMA table_info(chunks)").fetchall()]:
        conn.execute("ALTER TABLE chunks ADD COLUMN anchor TEXT")  # 加可为空的锚点列
        conn.commit()  # 提交迁移
    # 段落锚点索引：依赖 anchor 列，必须在确认列存在（上方 ALTER 或新库建表）之后才能建。
    # 放在迁移里而非静态脚本，是为了兼容"老库升级时 anchor 列尚未补上"的情况，避免建索引失败导致启动崩溃。
    conn.execute("CREATE INDEX IF NOT EXISTS idx_chunk_anchor ON chunks(anchor)")  # 按锚点快速定位章节
    conn.commit()  # 提交迁移
    # 补 doc_links 表：交叉引用（P1 交叉引用）。用 IF NOT EXISTS 幂等创建
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS doc_links (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            from_doc_id INTEGER NOT NULL,
            to_doc_id   INTEGER NOT NULL,
            link_text   TEXT,
            created_at  TEXT NOT NULL,
            FOREIGN KEY (from_doc_id) REFERENCES documents(id),
            FOREIGN KEY (to_doc_id) REFERENCES documents(id)
        )
        """
    )
    conn.commit()  # 提交迁移
    # 补 doc_comments 表：页面级评论（P1 页面级评论）。用 IF NOT EXISTS 幂等创建
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS doc_comments (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            doc_id      INTEGER NOT NULL,
            user_id     INTEGER NOT NULL,
            content     TEXT NOT NULL,
            created_at  TEXT NOT NULL,
            FOREIGN KEY (doc_id) REFERENCES documents(id),
            FOREIGN KEY (user_id) REFERENCES users(id)
        )
        """
    )
    conn.commit()  # 提交迁移

    # ---- P2 迁移：MFA 三列 + doc_diagrams 表 ----
    # 补 users.mfa_secret / mfa_enabled / mfa_backup 列：MFA 多因子认证（P2）
    user_cols = [r[1] for r in conn.execute("PRAGMA table_info(users)").fetchall()]
    if "mfa_secret" not in user_cols:  # 老库缺列，补上（可空）
        conn.execute("ALTER TABLE users ADD COLUMN mfa_secret TEXT")
        conn.commit()
    if "mfa_enabled" not in user_cols:  # 缺列补默认 0
        conn.execute("ALTER TABLE users ADD COLUMN mfa_enabled INTEGER NOT NULL DEFAULT 0")
        conn.commit()
    if "mfa_backup" not in user_cols:  # 缺列补可空
        conn.execute("ALTER TABLE users ADD COLUMN mfa_backup TEXT")
        conn.commit()
    # 补 doc_diagrams 表：内联画图（P2）。IF NOT EXISTS 幂等
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS doc_diagrams (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            doc_id      INTEGER NOT NULL,
            title       TEXT NOT NULL DEFAULT '未命名图',
            diagram_xml TEXT NOT NULL,
            created_by  INTEGER,
            created_at  TEXT NOT NULL,
            updated_at  TEXT NOT NULL,
            FOREIGN KEY (doc_id) REFERENCES documents(id)
        )
        """
    )
    conn.commit()  # 提交迁移


# ============================================================
# 三、查询辅助函数（统一参数化，杜绝 SQL 注入）
# ============================================================

def query(sql: str, params: Iterable[Any] = ()) -> list[sqlite3.Row]:
    """
    执行 SELECT 查询并返回全部结果行。

    参数：
        sql:    带 ? 占位符的 SQL 语句
        params: 与占位符一一对应的参数元组
    返回：
        sqlite3.Row 列表，每行可用列名访问
    """
    cur = get_conn().execute(sql, tuple(params))  # 执行参数化查询
    rows = cur.fetchall()  # 取出全部结果
    cur.close()  # 关闭游标释放资源
    return rows


def query_one(sql: str, params: Iterable[Any] = ()) -> Optional[sqlite3.Row]:
    """执行 SELECT 查询并只返回第一行，没有结果时返回 None。"""
    cur = get_conn().execute(sql, tuple(params))  # 执行查询
    row = cur.fetchone()  # 只取第一行
    cur.close()  # 关闭游标
    return row


def execute(sql: str, params: Iterable[Any] = ()) -> int:
    """
    执行 INSERT / UPDATE / DELETE 语句并提交。
    返回：新插入行的主键 ID（INSERT 场景）或受影响行数（UPDATE/DELETE 场景）
    """
    conn = get_conn()  # 拿到连接
    cur = conn.execute(sql, tuple(params))  # 执行语句
    conn.commit()  # 立即提交，保证数据落盘
    # lastrowid 在 INSERT 时是新行 ID；UPDATE/DELETE 时为 0，此时返回 rowcount 更有意义
    result = cur.lastrowid if cur.lastrowid else cur.rowcount
    cur.close()  # 关闭游标
    return result


def execute_many(sql: str, seq_params: Iterable[Iterable[Any]]) -> None:
    """
    批量执行同一条 SQL（如批量插入切片），比循环单条执行快一个数量级。
    """
    conn = get_conn()  # 拿到连接
    conn.executemany(sql, [tuple(p) for p in seq_params])  # 批量执行
    conn.commit()  # 统一提交


def row_to_dict(row: Optional[sqlite3.Row]) -> Optional[dict]:
    """把 sqlite3.Row 转成普通字典，便于 JSON 序列化返回给前端。"""
    if row is None:  # 空行直接返回 None
        return None
    return {key: row[key] for key in row.keys()}  # 遍历所有列名构造字典


def rows_to_dicts(rows: Iterable[sqlite3.Row]) -> list[dict]:
    """把多行结果批量转成字典列表。"""
    return [{key: r[key] for key in r.keys()} for r in rows]  # 列表推导式逐行转换


# ============================================================
# 四·补、并发编辑锁与文档级 ACL 辅助函数（P 完整版补齐）
# ============================================================
# 编辑锁采用「乐观抢占 + 超时失效」策略：
# - 打开编辑器即抢占锁；前端每 EDIT_LOCK_HEARTBEAT 秒续期一次；
# - 锁超过 TTL 未续期则自动失效，他人可抢占；
# - 保存时若锁被他人持有且仍有效，则拒绝保存以防互相覆盖。

def _lock_fmt(dt: datetime) -> str:
    """把 datetime 格式化成与 audit.now_iso 一致的字符串，便于字符串比较与展示。"""
    return dt.strftime("%Y-%m-%d %H:%M:%S")  # 例如 "2026-08-11 10:30:45"


def acquire_edit_lock(doc_id: int, user_id: int, user_name: str, ttl_seconds: int) -> dict:
    """
    抢占/续期文档的并发编辑锁。

    抢占成功的情形（UPDATE 命中）：锁当前空闲、或本来就是自己持有、或已超过 TTL 失效。
    抢占失败（被他人持有且有效）：返回对方信息，供前端提示"他人正在编辑"。

    参数：
        doc_id:       文档 ID
        user_id:      当前用户 ID
        user_name:    当前用户显示名（快照，提示用）
        ttl_seconds:  锁的有效秒数（超过则视为失效）
    返回：
        {"ok": True}                          —— 抢占/续期成功
        {"ok": False, "holder_user_id":.., "holder_name":.., "locked_at":..}  —— 被他人占用
    """
    now = datetime.now()  # 当前时刻
    now_iso = _lock_fmt(now)  # 格式化后的当前时间
    # 失效阈值：TTL 秒之前的时间点；edit_lock_at 早于它说明锁已过期可抢占
    expired_before = _lock_fmt(now - timedelta(seconds=ttl_seconds))
    cur = get_conn().execute(
        """
        UPDATE documents
        SET edit_lock_user = ?, edit_lock_at = ?, edit_lock_name = ?
        WHERE id = ?
          AND (edit_lock_user IS NULL            -- 锁空闲
               OR edit_lock_user = ?             -- 本来就是自己持有（续期）
               OR edit_lock_at <= ?)             -- 锁已超时失效
        """,
        (user_id, now_iso, user_name, doc_id, user_id, expired_before),
    )
    get_conn().commit()  # 提交抢占结果
    if cur.rowcount > 0:  # 抢到锁
        return {"ok": True}
    # 抢占失败：查出当前持有者信息返回给调用方
    row = query_one(
        "SELECT edit_lock_user, edit_lock_name, edit_lock_at FROM documents WHERE id = ?",
        (doc_id,),
    )
    return {
        "ok": False,
        "holder_user_id": row["edit_lock_user"] if row else None,
        "holder_name": row["edit_lock_name"] if row else None,
        "locked_at": row["edit_lock_at"] if row else None,
    }


def release_edit_lock(doc_id: int, user_id: int, is_admin: bool = False) -> bool:
    """
    释放文档的并发编辑锁。

    仅允许锁的持有者本人或管理员释放，避免误删他人的锁。
    返回：是否真的执行了释放（命中行数 > 0）。
    """
    # 条件：锁是「我」持有的，或我是管理员（强制释放）。1/0 作为 OR 的布尔开关
    cur = get_conn().execute(
        """
        UPDATE documents
        SET edit_lock_user = NULL, edit_lock_at = NULL, edit_lock_name = NULL
        WHERE id = ? AND (edit_lock_user = ? OR ?)
        """,
        (doc_id, user_id, 1 if is_admin else 0),
    )
    get_conn().commit()  # 提交释放
    return cur.rowcount > 0  # 命中即表示成功释放


def get_edit_lock(doc_id: int) -> Optional[sqlite3.Row]:
    """取出文档当前的编辑锁行（含 edit_lock_user/at/name），无锁时这些列为 NULL。"""
    return query_one(
        "SELECT edit_lock_user, edit_lock_name, edit_lock_at FROM documents WHERE id = ?",
        (doc_id,),
    )


def edit_lock_blocking(doc_id: int, user_id: int, ttl_seconds: int) -> Optional[str]:
    """
    判断当前用户是否因「他人正持有有效编辑锁」而被阻止保存。

    参数：
        doc_id:       文档 ID
        user_id:      当前用户 ID
        ttl_seconds:  锁有效秒数
    返回：
        None                          —— 未被阻止（无锁 / 锁是自己 / 锁已超时）
        str（对方显示名）             —— 被阻止，返回持有者姓名以便提示
    """
    row = get_edit_lock(doc_id)  # 取锁状态
    if not row or row["edit_lock_user"] is None:  # 无锁
        return None
    if row["edit_lock_user"] == user_id:  # 锁是自己的
        return None
    # 计算锁是否仍在有效期内
    try:
        locked_at = datetime.strptime(row["edit_lock_at"], "%Y-%m-%d %H:%M:%S")
    except (ValueError, TypeError):
        return None  # 时间格式异常按"已失效"放行
    if datetime.now() - locked_at > timedelta(seconds=ttl_seconds):  # 已超时
        return None
    return row["edit_lock_name"]  # 被他人有效占用，返回持有者姓名


# ---- 文档级细粒度可见权限（doc_acl）辅助 ----

# ACL 允许的权限档位与主体类型白名单，写入前校验，防止脏数据
_ACL_PERMS = ("view", "view_edit")
_ACL_PRINCIPAL_TYPES = ("user", "role")


def replace_doc_acl(doc_id: int, visibility_mode: str, entries: list[dict], operator_id: int) -> None:
    """
    整体替换某文档的细粒度权限设置。

    先删除旧设置，再按 entries 批量写入。visibility_mode 仅允许 'inherit' / 'restricted'。
    entries 每项形如 {"principal_type": "user"|"role", "principal_id": "...", "perm": "view"|"view_edit"}。
    非法项会被跳过（不抛异常，保证接口健壮性）。
    """
    # 校正可见权限模式：只接受已知两档，其余一律视作 inherit
    if visibility_mode not in ("inherit", "restricted"):
        visibility_mode = "inherit"
    # 更新文档的可见权限模式字段（幂等：无论是否切换都写一次）
    execute("UPDATE documents SET visibility_mode = ? WHERE id = ?", (visibility_mode, doc_id))
    # 清空该文档旧的全部 ACL 记录，准备整体替换
    execute("DELETE FROM doc_acl WHERE doc_id = ?", (doc_id,))
    now_iso = _lock_fmt(datetime.now())  # 授权时间
    for e in entries or []:  # 遍历前端传来的权限项
        ptype = e.get("principal_type")  # 主体类型
        pid = str(e.get("principal_id", "")).strip()  # 主体标识统一转字符串
        perm = e.get("perm", "view")  # 权限档位
        if ptype not in _ACL_PRINCIPAL_TYPES or not pid:  # 非法类型或空标识跳过
            continue
        if perm not in _ACL_PERMS:  # 非法权限档位降级为 view
            perm = "view"
        execute(
            """
            INSERT INTO doc_acl (doc_id, principal_type, principal_id, perm, created_by, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (doc_id, ptype, pid, perm, operator_id, now_iso),
        )


def get_doc_acl(doc_id: int) -> dict:
    """取出某文档的可见权限模式与全部 ACL 明细，用于前端渲染权限面板。"""
    doc = query_one("SELECT visibility_mode FROM documents WHERE id = ?", (doc_id,))  # 取模式
    rows = query(
        """
        SELECT a.id, a.principal_type, a.principal_id, a.perm, a.created_at,
               u.display_name AS principal_name
        FROM doc_acl a
        LEFT JOIN users u ON u.id = a.principal_id AND a.principal_type = 'user'
        WHERE a.doc_id = ?
        ORDER BY a.principal_type, a.principal_id
        """,
        (doc_id,),
    )
    return {
        "visibility_mode": doc["visibility_mode"] if doc else "inherit",  # 模式缺省 inherit
        "entries": rows_to_dicts(rows),  # ACL 明细列表
    }

