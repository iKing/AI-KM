# 更新日志（Changelog）

本项目所有重要变更均记录于此。格式参考 [Keep a Changelog](https://keepachangelog.com/)，
版本号遵循 [语义化版本](https://semver.org/lang/zh-CN/)（主版本.次版本.修订号）。

> 版本基线：`main` 分支的最新打标签提交即当前发布版本，详见 GitHub Releases。

---

## [1.0.0] — 2026-08-12

首个完整可交付基线。补齐对标 BookStack 的全部核心能力，并通过 183/183 项自动化验证零回归。

### 新增（Added）
- **认证与权限**：RBAC 五角色、三级密级、部门隔离（数据层过滤）；MFA(TOTP) 与 SSO(LDAP/OIDC) 优雅降级；登录限流；审计日志只增不改。
- **知识入库**：docx/pdf/xlsx/md/html/txt 解析 → 质量红线校验 → 语义切片 → 向量化 → 待审核状态机。
- **混合检索 + RAG**：BM25(FTS5+结巴) ∥ 向量 + RRF 融合 + 质量加权；RAG 三条红线（有据/溯源/拒答）+ 引用可点击。
- **版本历史**：每次编辑生成版本快照，支持行级对比与一键回滚。
- **富文本编辑器**：Quill 本地化（内网离线），图片内嵌上传，XSS 净化，富文本与纯文本双存。
- **标签体系 + 交叉引用 `[[…]]` + 段落锚点**直达。
- **标准 REST API v1** + OpenAPI 文档；diagrams.net 内联画图（图存库）。
- **P 完整版三项补齐（对标 BookStack 最后一公里）**：
  1. 并发编辑锁（防多人同时改互相覆盖，TTL 自动失效 + 心跳续期 + 保存释放）
  2. Docx / PDF 服务端一键导出（中文用内置 CID 字体，免外部字体文件）
  3. 文档级细粒度可见权限（每文档 `inherit`/`restricted` + 用户/角色白名单 `view`/`view_edit`，私有语义成立）

### 变更（Changed）
- 编辑类接口（保存/标签/回滚/设权限）统一由 `auth.can_edit_doc()` 闸门判定，替代原先散落的 `role_required` 装饰器。
- `visibility_filter()` 重写：`restricted` 模式下「本部门匹配」不再自动放行，必须命中文档级白名单，修复"私有文档被同部门看到"漏洞；密级红线对全部可见路径一律生效。

### 安全（Security）
- `.env`（真实 API Key）、`.workbuddy/`（含服务器 IP/密钥记忆）、`data/`（运行时库）均不入库。
- 密码 PBKDF2-SHA256 20 万次加盐哈希；参数化 SQL；文件类型白名单 + 路径穿越消毒；API Key 仅存哈希。

### 验证（Verified）
- 自动化验证套件 6 个，共 **183/183 全通过**：P1 标签/引用/锚点 25、P1 导出/评论/审计 36、P2 版本 26、P2 综合 48、富文本 25、完整版三项补齐 23。

---

## 待办（后续版本规划，见 GitHub Milestone）

- [ ] 推到内网 192.168.0.61 实际部署并做端到端冒烟
- [ ] SSO 真实 IdP 联调（LDAP/OIDC）
- [ ] diagrams.net 内网自托管 embed（离线可用）
- [ ] 向量检索接入有效 Embedding 服务（当前无密钥时降级 BM25）
- [ ] 性能压测（万级文档规模 P95<1.5s 设计指标验证）

[1.0.0]: https://github.com/iKing/AI-KM/releases/tag/v1.0.0
