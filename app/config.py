# -*- coding: utf-8 -*-
"""
配置中心模块
============
本模块集中管理 AI-KM 平台的所有可变参数。
设计原则：任何"以后可能要改"的东西都放这里，绝不散落在业务代码中硬编码。
所有配置项都可以通过环境变量（.env 文件）覆盖，实现"改配置不改代码"。
"""

import os  # 导入 os 模块，用于读取环境变量
from pathlib import Path  # 导入 Path 类，用于跨平台处理文件路径


# ============================================================
# 一、路径配置
# ============================================================

# BASE_DIR 指向项目根目录。__file__ 是当前文件路径，.parent 取上级目录，两次 .parent 即从 app/config.py 回到项目根
BASE_DIR = Path(__file__).parent.parent

# DATA_DIR 是所有运行时数据的存放目录（数据库、上传文件、索引），迁移时整个目录拷走即可
DATA_DIR = Path(os.getenv("AIKM_DATA_DIR", str(BASE_DIR / "data")))

# UPLOAD_DIR 存放用户上传的原始文件
UPLOAD_DIR = DATA_DIR / "uploads"

# INDEX_DIR 存放向量索引的缓存文件
INDEX_DIR = DATA_DIR / "index"

# DB_PATH 是 SQLite 数据库文件的完整路径
DB_PATH = Path(os.getenv("AIKM_DB_PATH", str(DATA_DIR / "aikm.db")))

# EVALSET_DIR 存放检索质量评测集文件
EVALSET_DIR = Path(os.getenv("AIKM_EVALSET_DIR", str(BASE_DIR / "evalset")))

# 启动时确保这些目录都存在，parents=True 表示自动创建多级目录，exist_ok=True 表示已存在不报错
for _dir in (DATA_DIR, UPLOAD_DIR, INDEX_DIR, EVALSET_DIR):
    _dir.mkdir(parents=True, exist_ok=True)


# ============================================================
# 二、Web 服务配置
# ============================================================

# SECRET_KEY 用于 Flask 会话 Cookie 的签名，生产环境必须通过环境变量设置为随机值。
# 重要安全改动：不再使用硬编码的弱默认值。
# 若环境变量未设置，则每次启动临时生成一个随机密钥（仅供本地开发/临时演示）。
# 生产部署务必通过 AIKM_SECRET_KEY 固定一个随机值，否则服务重启后所有用户登录态失效。
SECRET_KEY = os.getenv("AIKM_SECRET_KEY", "")
if not SECRET_KEY:  # 未配置时生成随机密钥，避免"弱密钥被复用"导致的会话伪造风险
    import secrets as _secrets  # 延迟导入密码学随机模块
    SECRET_KEY = _secrets.token_hex(32)  # 生成 32 字节（64 个十六进制字符）的随机密钥

# HOST 是服务监听地址，0.0.0.0 表示接受所有网卡的连接（内网部署必需）
HOST = os.getenv("AIKM_HOST", "0.0.0.0")

# PORT 是服务监听端口，默认 5200（避开你现有的 5100 智采日报服务）
PORT = int(os.getenv("AIKM_PORT", "5200"))

# DEBUG 是调试模式开关，生产环境必须为 False（否则会泄露堆栈信息）
DEBUG = os.getenv("AIKM_DEBUG", "false").lower() == "true"

# SESSION_HOURS 是登录会话的有效小时数，超时后需要重新登录
SESSION_HOURS = int(os.getenv("AIKM_SESSION_HOURS", "8"))


# ============================================================
# 三、模型适配层配置（支撑"云 API ↔ 私有化"平滑切换）
# ============================================================

# --- LLM（大语言模型，负责生成问答） ---

# LLM_PROVIDER 决定使用哪个大模型服务商："deepseek" / "openai" / "ollama"
# 10 月私有化时只需把这个值改成 "ollama"，其余代码零改动
LLM_PROVIDER = os.getenv("AIKM_LLM_PROVIDER", "deepseek")

# LLM_BASE_URL 是模型服务的接口地址
LLM_BASE_URL = os.getenv("AIKM_LLM_BASE_URL", "https://api.deepseek.com/v1")

# LLM_API_KEY 是访问模型服务的密钥（私有化 Ollama 不需要，留空即可）
LLM_API_KEY = os.getenv("AIKM_LLM_API_KEY", "")

# LLM_MODEL 是具体使用的模型名称
LLM_MODEL = os.getenv("AIKM_LLM_MODEL", "deepseek-chat")

# LLM_TIMEOUT 是模型调用的超时秒数，超时后走降级逻辑
LLM_TIMEOUT = int(os.getenv("AIKM_LLM_TIMEOUT", "120"))

# LLM_TEMPERATURE 控制生成的随机性，知识问答场景要求准确，所以设得很低
LLM_TEMPERATURE = float(os.getenv("AIKM_LLM_TEMPERATURE", "0.1"))

# --- Embedding（向量模型，负责把文本转成向量用于语义检索） ---

# EMBED_PROVIDER 决定使用哪个向量服务："siliconflow" / "openai" / "ollama" / "hash"
# "hash" 是无外网时的本地降级方案（基于字符哈希，效果差但保证系统可跑）
EMBED_PROVIDER = os.getenv("AIKM_EMBED_PROVIDER", "siliconflow")

# EMBED_BASE_URL 是向量服务的接口地址
EMBED_BASE_URL = os.getenv("AIKM_EMBED_BASE_URL", "https://api.siliconflow.cn/v1")

# EMBED_API_KEY 是向量服务的密钥
EMBED_API_KEY = os.getenv("AIKM_EMBED_API_KEY", "")

# EMBED_MODEL 是向量模型名称，BAAI/bge-m3 对中文效果好且支持长文本
EMBED_MODEL = os.getenv("AIKM_EMBED_MODEL", "BAAI/bge-m3")

# EMBED_DIM 是向量维度，bge-m3 是 1024 维。切换模型时必须同步修改并重建索引
EMBED_DIM = int(os.getenv("AIKM_EMBED_DIM", "1024"))

# EMBED_BATCH 是批量向量化时每批的文本条数，太大容易超时，太小效率低
EMBED_BATCH = int(os.getenv("AIKM_EMBED_BATCH", "16"))

# EMBED_TIMEOUT 是向量接口的超时秒数
EMBED_TIMEOUT = int(os.getenv("AIKM_EMBED_TIMEOUT", "60"))


# ============================================================
# 四、文档入库配置
# ============================================================

# ALLOWED_EXTENSIONS 是允许上传的文件扩展名白名单（安全措施，防止上传可执行文件）
ALLOWED_EXTENSIONS = {".docx", ".pdf", ".xlsx", ".xls", ".md", ".txt", ".html", ".htm"}

# MAX_FILE_SIZE_MB 是单个文件的最大体积限制（兆字节）
MAX_FILE_SIZE_MB = int(os.getenv("AIKM_MAX_FILE_SIZE_MB", "50"))

# IMAGE_MAX_MB 是富文本编辑器内嵌图片的单张体积上限（兆字节），图片走 /uploads 单独存储
IMAGE_MAX_MB = int(os.getenv("AIKM_IMAGE_MAX_MB", "10"))

# ALLOWED_IMAGE_EXTS 是内嵌图片允许的扩展名白名单（仅静态图片格式，杜绝可执行文件）
ALLOWED_IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".svg"}

# MIN_CONTENT_LENGTH 是正文最小字数，低于此值判定为"无效文档"拒绝入库（规范第三章红线1）
MIN_CONTENT_LENGTH = int(os.getenv("AIKM_MIN_CONTENT_LENGTH", "200"))

# CHUNK_SIZE 是知识切片的目标字数。太大则检索精度低，太小则语义不完整
CHUNK_SIZE = int(os.getenv("AIKM_CHUNK_SIZE", "600"))

# CHUNK_OVERLAP 是相邻切片的重叠字数，防止关键信息正好被切断在两片之间
CHUNK_OVERLAP = int(os.getenv("AIKM_CHUNK_OVERLAP", "80"))


# ============================================================
# 五、检索配置
# ============================================================

# SEARCH_TOP_K 是最终返回给用户的检索结果条数
SEARCH_TOP_K = int(os.getenv("AIKM_SEARCH_TOP_K", "10"))

# RECALL_TOP_K 是每一路（BM25/向量）召回的候选条数，召回多一些再融合排序，效果更好
RECALL_TOP_K = int(os.getenv("AIKM_RECALL_TOP_K", "50"))

# RRF_K 是 RRF（倒数排名融合）算法的平滑常数，业界经验值为 60
RRF_K = int(os.getenv("AIKM_RRF_K", "60"))

# QUALITY_WEIGHTS 是不同质量等级的检索加权系数（规范第七章）
# verified 已验证知识加权提升，draft 待提质知识降权，实现"高质量优先"
QUALITY_WEIGHTS = {
    "verified": 1.2,  # 已经过人工校验的高质量知识，排序时加权 20%
    "normal": 1.0,    # 常规知识，不加不减
    "draft": 0.8,     # 待提质知识，降权 20%，避免劣质内容挤占前排
}


# ============================================================
# 六、RAG 问答配置
# ============================================================

# RAG_CONTEXT_CHUNKS 是送给大模型作为参考依据的知识片段数量
RAG_CONTEXT_CHUNKS = int(os.getenv("AIKM_RAG_CONTEXT_CHUNKS", "6"))

# RAG_MAX_CONTEXT_CHARS 是上下文总字数上限，防止超出模型窗口
RAG_MAX_CONTEXT_CHARS = int(os.getenv("AIKM_RAG_MAX_CONTEXT_CHARS", "8000"))

# RAG_MIN_SCORE 是判定"检索到有效内容"的最低分数阈值，低于此值视为无相关知识，触发拒答
RAG_MIN_SCORE = float(os.getenv("AIKM_RAG_MIN_SCORE", "0.01"))

# RAG_HISTORY_TURNS 是多轮对话保留的历史轮数
RAG_HISTORY_TURNS = int(os.getenv("AIKM_RAG_HISTORY_TURNS", "3"))


# ============================================================
# 七、业务枚举常量（与《知识分类标准与入库规范》严格对应）
# ============================================================

# CATEGORIES_L1 是六大一级知识域，键为编码，值为中文名称。前端下拉框由此生成
CATEGORIES_L1 = {
    "POLICY": "政策法规库",
    "PROJECT": "项目知识库",
    "CUSTOMER": "客户与市场",
    "PRODUCT": "产品与技术",
    "PROCESS": "制度与流程",
    "TRAINING": "培训与沉淀",
}

# CATEGORIES_L2 是二级分类，按一级分类分组。规范允许部门补充，因此这里是初始值，运行时可在数据库中扩展
CATEGORIES_L2 = {
    "POLICY": {
        "POLICY-NATIONAL": "国家级政策",
        "POLICY-PROVINCIAL": "省级政策",
        "POLICY-CITY": "市级/地方细则",
        "POLICY-BIDDING": "招投标法规",
        "POLICY-INSURANCE": "医保相关",
        "POLICY-REGISTRATION": "器械注册与准入",
        "POLICY-INTERPRET": "政策解读与分析",
    },
    "PROJECT": {
        "PROJECT-PROPOSAL": "项目方案与建议书",
        "PROJECT-BID": "投标文件",
        "PROJECT-IMPL": "实施与交付记录",
        "PROJECT-ACCEPT": "验收材料",
        "PROJECT-REVIEW": "项目复盘总结",
    },
    "CUSTOMER": {
        "CUSTOMER-FAQ": "常见客户问答",
        "CUSTOMER-PROFILE": "客户档案",
        "CUSTOMER-COMPETITOR": "竞品分析",
        "CUSTOMER-INDUSTRY": "行业研究",
        "CUSTOMER-NEWS": "市场动态",
    },
    "PRODUCT": {
        "PRODUCT-MANUAL": "产品手册",
        "PRODUCT-API": "接口文档",
        "PRODUCT-TECH": "技术方案",
        "PRODUCT-ARCH": "架构设计",
        "PRODUCT-TROUBLE": "故障处理",
    },
    "PROCESS": {
        "PROCESS-SYSTEM": "公司制度",
        "PROCESS-SOP": "操作规程",
        "PROCESS-APPROVAL": "审批流程",
        "PROCESS-TEMPLATE": "模板表单",
    },
    "TRAINING": {
        "TRAINING-COURSE": "培训课件",
        "TRAINING-GUIDE": "操作指南",
        "TRAINING-SHARE": "经验分享",
        "TRAINING-AICASE": "AI应用案例",
    },
}

# SECURITY_LEVELS 是三级密级定义（规范第五章）
SECURITY_LEVELS = {
    "public": "公开",
    "internal": "内部",
    "confidential": "机密",
}

# SECURITY_RANK 把密级转成数字，用于比较大小（用户密级许可 >= 文档密级才可见）
SECURITY_RANK = {"public": 1, "internal": 2, "confidential": 3}

# QUALITY_LEVELS 是三级质量等级（规范第七章）
QUALITY_LEVELS = {
    "verified": "已验证高质量",
    "normal": "常规",
    "draft": "待提质",
}

# DOC_STATUS 是文档状态机的全部状态（SRS FR-3.5）
DOC_STATUS = {
    "uploaded": "已上传",
    "parsing": "解析中",
    "parsed": "已解析",
    "pending_review": "待审核",
    "published": "已发布",
    "rejected": "已退回",
    "archived": "已下架",
}

# ROLES 是五种用户角色（SRS 3.1）
ROLES = {
    "admin": "系统管理员",
    "reviewer": "知识审核员",
    "contributor": "知识贡献者",
    "user": "普通用户",
}

# SPACE_KINDS 是知识树节点的类型（对应 BookStack 的书架/书/章）
SPACE_KINDS = {
    "shelf": "书架",
    "book": "书籍",
    "chapter": "章节",
}

# REVIEW_CYCLE_DAYS 是各分类的默认复审周期天数（规范第八章）
REVIEW_CYCLE_DAYS = {
    "POLICY": 90,     # 政策变化快，90 天必须复审
    "PROJECT": 180,   # 项目知识相对稳定
    "CUSTOMER": 180,
    "PRODUCT": 180,
    "PROCESS": 365,
    "TRAINING": 365,
}


# ============================================================
# 八、审计与安全配置
# ============================================================

# PASSWORD_ITERATIONS 是密码哈希的迭代次数，越大越安全但越慢，20 万次是当前推荐值
PASSWORD_ITERATIONS = int(os.getenv("AIKM_PASSWORD_ITERATIONS", "200000"))

# API_RATE_LIMIT_PER_MIN 是单个 API Key 每分钟的最大调用次数（防滥用）
API_RATE_LIMIT_PER_MIN = int(os.getenv("AIKM_API_RATE_LIMIT", "60"))

# AUDIT_RETAIN_DAYS 是审计日志保留天数，0 表示永久保留（合规场景建议永久）
AUDIT_RETAIN_DAYS = int(os.getenv("AIKM_AUDIT_RETAIN_DAYS", "0"))

# HTTPS 表示服务前方是否有 TLS 终止（Nginx 等反向代理做了 HTTPS）。
# 为 true 时，会话 Cookie 标记为 Secure 并启用 HSTS，防止明文传输被窃听。
# 内网若暂未上 HTTPS，保持 false 即可（此时 Cookie 不强求 HTTPS，便于纯 HTTP 访问）。
HTTPS = os.getenv("AIKM_HTTPS", "false").lower() == "true"

# 登录暴力破解防护：同一「账号或 IP」在窗口内连续失败达到上限后，临时锁定一段时间
LOGIN_MAX_FAIL = int(os.getenv("AIKM_LOGIN_MAX_FAIL", "5"))   # 允许的最大连续失败次数
LOGIN_LOCK_MIN = int(os.getenv("AIKM_LOGIN_LOCK_MIN", "15"))  # 触发锁定后的冷却分钟数
# 失败计数滑动窗口（分钟）：只统计最近这段时间内的失败，过期自动清零
LOGIN_FAIL_WINDOW_MIN = int(os.getenv("AIKM_LOGIN_FAIL_WINDOW", "15"))


# ============================================================
# 八·补、并发编辑锁与文档级细粒度权限（P 完整版补齐）
# ============================================================

# --- 并发编辑锁（防止多人同时编辑互相覆盖） ---
# EDIT_LOCK_ENABLED 是编辑锁总开关。false 时完全不校验锁（退回"无锁"老行为，兼容极简部署）
EDIT_LOCK_ENABLED = os.getenv("AIKM_EDIT_LOCK_ENABLED", "true").lower() == "true"
# EDIT_LOCK_TTL_SECONDS 是锁的有效期（秒）。超过此时间无续期则自动失效，他人可抢占。默认 300 秒（5 分钟）
EDIT_LOCK_TTL_SECONDS = int(os.getenv("AIKM_EDIT_LOCK_TTL", "300"))
# EDIT_LOCK_HEARTBEAT_SECONDS 是前端续期间隔（秒）。应明显小于 TTL，避免锁在无操作期间意外失效
EDIT_LOCK_HEARTBEAT_SECONDS = int(os.getenv("AIKM_EDIT_LOCK_HEARTBEAT", "60"))
# EDIT_LOCK_CHECK_ON_SAVE 控制"保存时是否校验他人持锁"。true 时若锁被他人有效持有则拒绝保存，避免覆盖
EDIT_LOCK_CHECK_ON_SAVE = os.getenv("AIKM_EDIT_LOCK_CHECK_SAVE", "true").lower() == "true"

# --- 文档级细粒度可见权限（BookStack 式每对象权限） ---
# ACL_ENABLED 是文档级 ACL 总开关。false 时 visibility_mode 一律按 inherit 处理，仅用全局规则
ACL_ENABLED = os.getenv("AIKM_ACL_ENABLED", "true").lower() == "true"


# ============================================================
# 九、P2 安全增强：MFA / SSO / 标准 REST API 配置
# ============================================================

# --- MFA（多因子认证，TOTP） ---
# MFA_ISSUER 是 TOTP 二维码里显示的发行方名称（出现在用户的 authenticator App 中）
MFA_ISSUER = os.getenv("AIKM_MFA_ISSUER", "AI-KM")
# MFA_ENABLED 是全局总开关：true 时用户可自愿开启 TOTP 二次验证
# 注意：这里采用"用户自愿开启"而非"强制全员开启"，避免没有扫码设备的用户被锁死
MFA_ENABLED = os.getenv("AIKM_MFA_ENABLED", "true").lower() == "true"
# MFA_BACKUP_COUNT 是开启 MFA 时生成的备用码数量（备用码用于手机丢失时紧急登录）
MFA_BACKUP_COUNT = int(os.getenv("AIKM_MFA_BACKUP_COUNT", "8"))

# --- SSO / LDAP / OIDC（单点登录，配置门控，缺依赖不崩） ---
# SSO_ENABLED 是单点登录总开关。关闭时所有 SSO 路由直接提示"未启用"
SSO_ENABLED = os.getenv("AIKM_SSO_ENABLED", "false").lower() == "true"
# SSO_PROVIDER 决定使用哪种协议："ldap" / "oidc" / "saml"。当前实现 ldap 与 oidc 两套
SSO_PROVIDER = os.getenv("AIKM_SSO_PROVIDER", "ldap")
# AUTO_CREATE_USER 为 true 时，SSO 登录成功但本地无账号会自动建一个（按 username 关联部门）
SSO_AUTO_CREATE_USER = os.getenv("AIKM_SSO_AUTO_CREATE_USER", "true").lower() == "true"
# SSO_DEFAULT_ROLE 是 SSO 自动建号时赋予的默认角色（最小权限，普通用户）
SSO_DEFAULT_ROLE = os.getenv("AIKM_SSO_DEFAULT_ROLE", "user")
# SSO_DEFAULT_DEPT 是 SSO 自动建号时归属的部门名（找不到则用根部门"知识管理中心"）
SSO_DEFAULT_DEPT = os.getenv("AIKM_SSO_DEFAULT_DEPT", "知识管理中心")

# LDAP 连接配置（仅 SSO_PROVIDER=ldap 时使用）
LDAP_SERVER = os.getenv("AIKM_LDAP_SERVER", "")          # LDAP 服务器地址，如 ldap://10.0.0.10:389
LDAP_BIND_DN = os.getenv("AIKM_LDAP_BIND_DN", "")        # 服务账号 DN（用于搜索用户）
LDAP_BIND_PASSWORD = os.getenv("AIKM_LDAP_BIND_PWD", "") # 服务账号密码
LDAP_USER_BASE = os.getenv("AIKM_LDAP_USER_BASE", "")    # 用户搜索基准 DN，如 ou=users,dc=corp
LDAP_USER_FILTER = os.getenv("AIKM_LDAP_USER_FILTER", "(uid={username})")  # 用户检索过滤器模板
LDAP_USER_RDNN_ATTR = os.getenv("AIKM_LDAP_RDNN_ATTR", "uid")  # 取到本地 username 的属性
LDAP_USER_NAME_ATTR = os.getenv("AIKM_LDAP_NAME_ATTR", "cn")   # 取到显示名的属性
LDAP_USER_MAIL_ATTR = os.getenv("AIKM_LDAP_MAIL_ATTR", "mail") # 取到邮箱的属性（用于账号匹配）
LDAP_TLS = os.getenv("AIKM_LDAP_TLS", "false").lower() == "true"  # 是否启用 StartTLS

# OIDC 配置（仅 SSO_PROVIDER=oidc 时使用）
OIDC_ISSUER = os.getenv("AIKM_OIDC_ISSUER", "")          # 身份提供方 Issuer，如 https://sso.corp/auth/realms/km
OIDC_CLIENT_ID = os.getenv("AIKM_OIDC_CLIENT_ID", "")    # 本系统在 IdP 注册的客户端 ID
OIDC_CLIENT_SECRET = os.getenv("AIKM_OIDC_CLIENT_SECRET", "")  # 客户端密钥
OIDC_REDIRECT_URI = os.getenv("AIKM_OIDC_REDIRECT_URI", "")    # 回调地址，如 http://host/api/sso/oidc/callback
OIDC_SCOPES = os.getenv("AIKM_OIDC_SCOPES", "openid email profile")  # 申请的授权范围

# --- 标准 REST API v1 ---
# API_V1_PREFIX 是资源式 REST 接口的统一前缀，与老的 /api/（动作式）区分开
API_V1_PREFIX = "/api/v1"
# REST_PAGE_SIZE 是 REST 列表接口的默认每页条数
REST_PAGE_SIZE = int(os.getenv("AIKM_REST_PAGE_SIZE", "20"))
# REST_MAX_PAGE_SIZE 是每页上限，防止一次拉太多拖垮服务
REST_MAX_PAGE_SIZE = int(os.getenv("AIKM_REST_MAX_PAGE_SIZE", "100"))


def load_dotenv() -> None:
    """
    读取项目根目录下的 .env 文件并注入到环境变量。
    这样本地开发时不用每次 export 一堆变量，Docker 部署时又可以用真正的环境变量覆盖。
    注意：本函数必须在模块顶部的配置项被读取之前调用才有效，
    因此在 run.py 中会先调用它再导入 config（详见 run.py 的加载顺序说明）。
    """
    env_file = BASE_DIR / ".env"  # 拼出 .env 文件的完整路径
    if not env_file.exists():  # 如果文件不存在就直接返回，不报错（生产环境用真实环境变量）
        return
    # 以 UTF-8 编码逐行读取 .env 文件
    for line in env_file.read_text(encoding="utf-8").splitlines():
        line = line.strip()  # 去掉行首尾的空白字符
        if not line or line.startswith("#"):  # 跳过空行和以 # 开头的注释行
            continue
        if "=" not in line:  # 跳过不含等号的非法行
            continue
        key, value = line.split("=", 1)  # 以第一个等号为界分割成键和值
        key = key.strip()  # 清理键两侧空白
        value = value.strip().strip('"').strip("'")  # 清理值两侧空白以及可能存在的引号
        # 只有当环境变量尚未设置时才写入，保证真实环境变量的优先级更高
        os.environ.setdefault(key, value)


def get_runtime_config() -> dict:
    """
    返回当前生效的运行时配置摘要，供管理界面展示与故障排查使用。
    注意：密钥类字段做脱敏处理，只显示前后几位，防止在界面上泄露完整密钥。
    """

    def mask(secret: str) -> str:
        """内部辅助函数：把密钥打码，只保留头尾各 4 个字符"""
        if not secret:  # 空密钥直接返回"未配置"
            return "（未配置）"
        if len(secret) <= 8:  # 太短的密钥全部打码，避免暴露
            return "****"
        return f"{secret[:4]}****{secret[-4:]}"  # 显示头 4 位 + 星号 + 尾 4 位

    # 组装成字典返回，键为中文以便直接在管理界面展示
    return {
        "LLM服务商": LLM_PROVIDER,
        "LLM地址": LLM_BASE_URL,
        "LLM模型": LLM_MODEL,
        "LLM密钥": mask(LLM_API_KEY),
        "向量服务商": EMBED_PROVIDER,
        "向量地址": EMBED_BASE_URL,
        "向量模型": EMBED_MODEL,
        "向量维度": EMBED_DIM,
        "向量密钥": mask(EMBED_API_KEY),
        "数据库路径": str(DB_PATH),
        "上传目录": str(UPLOAD_DIR),
        "切片大小": CHUNK_SIZE,
        "切片重叠": CHUNK_OVERLAP,
        "召回条数": RECALL_TOP_K,
        "返回条数": SEARCH_TOP_K,
        "调试模式": DEBUG,
    }
