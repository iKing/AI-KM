# AI-KM 标准 REST API 参考（v1）

> 配套在线文档：`/api/v1/docs`（Swagger UI，需登录后访问，自动读取 `/api/v1/openapi.json`）。
> 本文件为离线速查版。

## 1. 基础信息

- 统一前缀：`/api/v1`
- 响应信封：`{"ok": true/false, "data": ..., "error": "...", "page": {...}}`
- 列表接口分页参数：`page`（从 1 开始）、`per_page`（默认 20，上限 100）
- 分页返回：`page = {"page":, "per_page":, "total":, "pages":}`

## 2. 鉴权

两种等价方式（任选其一）：

1. **浏览器会话**：已登录后在页面/控制台直接调用。
2. **API Key（程序调用）**：HTTP 头
   ```
   Authorization: Bearer aikm_xxxx
   ```
   Key 在「管理后台 → API 密钥」创建，明文仅创建时展示一次，库里只存哈希。
   未带鉴权返回 `401 {"ok":false,"error":"未登录或会话已过期"}`。

## 3. 端点清单

| 方法 | 路径 | 说明 | 权限 |
|------|------|------|------|
| GET | `/api/v1/health` | 健康检查（返回文档总数与版本） | 登录/Key |
| GET | `/api/v1/documents` | 文档列表（支持 `category_l1`/`security_level`/`tag`/`keyword`/`space_id`/`status` 过滤） | 登录/Key |
| GET | `/api/v1/documents/<id>` | 文档详情（含 `tags`/`out_links`/`in_links`/`chunks`） | 登录/Key |
| GET | `/api/v1/search?q=<词>` | 知识检索（`mode=hybrid\|bm25\|vector`、`top_k`） | 登录/Key |
| GET | `/api/v1/spaces` | 知识树（当前用户可见节点 + 文档数） | 登录/Key |
| GET | `/api/v1/tags` | 标签列表（含使用计数） | 登录/Key |
| GET | `/api/v1/comments?doc_id=<id>` | 文档评论列表 | 登录/Key |
| GET | `/api/v1/users` | 用户列表（脱敏） | 仅管理员 |
| GET | `/api/v1/audit` | 审计日志（支持 `action`/`keyword`/`date_from`/`date_to`） | 仅管理员 |
| GET | `/api/v1/openapi.json` | OpenAPI 3.0 规范 | 登录/Key |

## 4. 调用示例

```bash
# 用 API Key 拉文档列表（第 1 页，每页 5 条）
curl -H "Authorization: Bearer $AIKM_API_KEY" \
     "http://localhost:5200/api/v1/documents?page=1&per_page=5"

# 检索
curl -H "Authorization: Bearer $AIKM_API_KEY" \
     "http://localhost:5200/api/v1/search?q=供应商准入&mode=hybrid"

# 按标签过滤
curl -H "Authorization: Bearer $AIKM_API_KEY" \
     "http://localhost:5200/api/v1/documents?tag=采购制度"
```

## 5. 与老接口 `/api/` 的关系

- `/api/`（动作式）：登录、上传、在线编辑、版本回滚、MFA、SSO、diagrams、评论、管理后台等**写操作与页面支撑接口**仍走这里。
- `/api/v1/`（资源式）：面向第三方集成/自动化的**只读资源接口**，统一信封 + 分页 + OpenAPI 文档。
- 两者共存，互不影响；后续新增对外集成优先落在 v1。

## 6. P2 其它新增能力（非 v1）

- **MFA 多因子认证**：`/api/mfa/status`、`/api/mfa/setup`、`/api/mfa/confirm`、`/api/mfa/disable`；登录二次验证 `/api/login/mfa`（开启后密码登录会返回 `mfa_required`）。
- **SSO / LDAP / OIDC**：`/api/login/ldap`、`/api/sso/oidc/authorize`、`/api/sso/oidc/callback`。由 `AIKM_SSO_ENABLED` 等开关控制，未配置不暴露。
- **diagrams.net 内联画图**：`GET/POST /api/documents/<id>/diagrams`、`PUT/DELETE /api/documents/<id>/diagrams/<did>`。
