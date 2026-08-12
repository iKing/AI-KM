# -*- coding: utf-8 -*-
"""
入库流水线
==========
职责：编排"原始文件 → 可被 AI 检索的知识"的完整过程。

流程（SRS FR-3）：
    1. 元数据校验   —— 把《入库规范》变成代码里的门禁，不合规直接拒绝
    2. 文件落盘     —— 保存原件，支持溯源和重新解析
    3. 内容解析     —— 提取带层级的结构化文本
    4. 质量红线检查 —— 正文长度、重复检测
    5. 知识切片     —— 按语义边界切分并携带章节路径
    6. 向量化       —— 生成语义向量（失败时降级为仅关键词检索）
    7. 索引写入     —— 写入 chunks / chunk_vectors / chunks_fts 三张表
    8. 状态流转     —— 进入待审核状态

设计原则：
- 全流程事务化：中途失败不留半成品数据
- 失败可恢复：错误信息落库，可重试
- 批量不中断：一份失败不影响其余（FR-3.2）
"""

import hashlib  # 计算内容哈希用于去重
import re  # 正则表达式，用于解析交叉引用 [[...]] 语法
import shutil  # 文件复制
import struct  # 把浮点数组打包成二进制存库
import uuid  # 生成唯一文件名
from datetime import datetime, timedelta  # 日期计算（复审周期）
from pathlib import Path  # 路径处理
from typing import Any, Optional  # 类型注解

from .. import audit, config, db  # 项目内部模块
from ..providers import embedding  # 向量适配层
from ..search import tokenizer  # 中文分词
from .chunker import chunk_blocks, make_summary  # 切片与摘要
from .parser import ParseError, parse_file  # 文档解析（parse_file 为统一入口）
# 以下三个是"纯文本 → 结构化块"的解析器，下划线前缀表示内部函数，
# 在线编辑器/版本回滚需要从纯文本重建切片，因此在此直接调用它们（属于本项目内部可信调用）
from .parser import _parse_markdown, _parse_html, _parse_txt  # Markdown/HTML/纯文本 解析
from ..htmlutil import sanitize_html, html_to_text  # 富文本 HTML 净化与转纯文本（零依赖）


class IngestError(Exception):
    """入库失败异常，携带明确的业务原因供界面展示。"""
    pass


# ============================================================
# 一、元数据校验（把规范变成门禁）
# ============================================================

# REQUIRED_FIELDS 是必填元数据字段（《入库规范》3.1 表格中标 ✅ 的字段）
REQUIRED_FIELDS = ["title", "category_l1", "department_id", "security_level", "owner"]

# FIELD_LABELS 把字段名映射成中文，用于生成"说人话"的错误提示（NFR-8 易用）
FIELD_LABELS = {
    "title": "标题",
    "category_l1": "一级分类",
    "department_id": "归属部门",
    "security_level": "密级",
    "owner": "责任人",
    "effective_date": "生效日期",
}


def validate_metadata(meta: dict) -> list[str]:
    """
    校验元数据是否符合《知识分类标准与入库规范》。

    返回：
        错误信息列表。空列表表示校验通过。
        每条错误都写明具体是哪个字段、什么问题、该怎么改——这是易用性的关键。
    """
    errors: list[str] = []  # 收集所有错误，一次性返回，避免用户改一个报一个

    # --- 必填字段检查 ---
    for field in REQUIRED_FIELDS:  # 遍历所有必填字段
        value = meta.get(field)  # 取值
        # 值为 None、空字符串、或纯空白都算缺失
        if value is None or (isinstance(value, str) and not value.strip()):
            errors.append(f"缺少必填项『{FIELD_LABELS.get(field, field)}』")

    # --- 标题规范检查（规范 3.2） ---
    title = (meta.get("title") or "").strip()  # 取标题并去空白
    if title:  # 有标题才检查
        if len(title) < 6:  # 太短的标题必然信息量不足
            errors.append(f"标题过短（当前 {len(title)} 字），至少 6 字。请按『[地域/主体]+[时间]+[主题]+[类型]』命名")
        if len(title) > 120:  # 太长的标题多半是把正文粘进来了
            errors.append(f"标题过长（当前 {len(title)} 字），请精简至 120 字以内")
        # 检查是否是"无信息量"的文件名式标题
        bad_patterns = ["新建", "文档1", "未命名", "副本", "final", "最终版", "无标题"]
        lower_title = title.lower()  # 转小写便于匹配英文
        for bad in bad_patterns:  # 逐个检查
            if bad in lower_title:  # 命中问题模式
                errors.append(f"标题包含无意义词『{bad}』，请改为描述实际内容的标题")
                break  # 报一次就够了，不重复刷屏

    # --- 一级分类必须是六大域之一 ---
    cat_l1 = meta.get("category_l1")  # 取一级分类
    if cat_l1 and cat_l1 not in config.CATEGORIES_L1:  # 不在预定义列表中
        valid = "、".join(f"{k}({v})" for k, v in config.CATEGORIES_L1.items())  # 拼出合法值列表
        errors.append(f"一级分类『{cat_l1}』无效。合法值：{valid}")

    # --- 密级必须是三级之一 ---
    sec = meta.get("security_level")  # 取密级
    if sec and sec not in config.SECURITY_LEVELS:  # 不合法
        errors.append(f"密级『{sec}』无效。合法值：public(公开)、internal(内部)、confidential(机密)")

    # --- 质量等级校验 ---
    quality = meta.get("quality_level")  # 取质量等级
    if quality and quality not in config.QUALITY_LEVELS:  # 不合法
        errors.append(f"质量等级『{quality}』无效。合法值：verified、normal、draft")

    # --- 日期格式校验 ---
    for date_field in ("effective_date", "expire_date"):  # 检查两个日期字段
        date_value = meta.get(date_field)  # 取值
        if date_value:  # 有值才校验（这两个字段非必填）
            try:
                datetime.strptime(str(date_value), "%Y-%m-%d")  # 尝试按标准格式解析
            except ValueError:  # 格式不对
                label = "生效日期" if date_field == "effective_date" else "失效日期"
                errors.append(f"『{label}』格式错误（当前值：{date_value}），应为 YYYY-MM-DD，如 2025-06-12")

    # --- 政策类文档的额外要求（规范 3.1 备注：政策类来源必填） ---
    if cat_l1 == "POLICY" and not (meta.get("source") or "").strip():
        errors.append("政策法规类文档必须填写『来源』（发文机构或原文 URL），以便追溯权威性")

    # --- 责任人不能填机构名（规范反例） ---
    owner = (meta.get("owner") or "").strip()  # 取责任人
    if owner and owner in ("公司", "IT", "技术部", "各部门", "全体", "无"):  # 命中禁用值
        errors.append(f"『责任人』不能填写『{owner}』，必须是具体的人名，以便复审时能找到对接人")

    return errors  # 返回全部错误


# ============================================================
# 二、辅助函数
# ============================================================

def _content_hash(text: str) -> str:
    """
    计算正文内容的 SHA256 哈希，用于重复检测（规范红线 3）。

    注意：先把所有空白字符压缩掉再计算。
    这样"同一份文档因为排版微调导致空格数量不同"不会被误判为两份不同文档。
    """
    normalized = "".join(text.split())  # split() 不带参数会按任意空白切分，再拼起来即去掉全部空白
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()  # 返回 64 位十六进制哈希


def _pack_vector(vec: list[float]) -> bytes:
    """
    把浮点数列表打包成二进制，用于存入 SQLite 的 BLOB 字段。

    为什么用二进制而不是 JSON：
    1024 维向量存 JSON 约 20KB，存 float32 二进制只要 4KB，省 5 倍空间；
    读取时也不需要 JSON 解析，直接内存映射，快一个数量级。
    """
    # "<" 表示小端字节序（跨平台一致），"f" 表示 float32，前面的数字是元素个数
    return struct.pack(f"<{len(vec)}f", *vec)


def _unpack_vector(blob: bytes) -> list[float]:
    """把二进制还原成浮点数列表。"""
    count = len(blob) // 4  # float32 每个占 4 字节，除以 4 得到元素个数
    return list(struct.unpack(f"<{count}f", blob))  # 解包成元组再转列表


def _calc_next_review(category_l1: str, effective_date: Optional[str]) -> Optional[str]:
    """
    计算下次复审日期（规范第八章：周期性复审）。

    政策类 90 天、项目类 180 天、制度类 365 天。
    从生效日期起算；没有生效日期就从今天起算。
    """
    days = config.REVIEW_CYCLE_DAYS.get(category_l1, 365)  # 查询该分类的复审周期，默认 365 天
    try:
        # 有生效日期就从生效日期起算，否则从今天起算
        base = datetime.strptime(effective_date, "%Y-%m-%d") if effective_date else datetime.now()
    except (ValueError, TypeError):  # 日期格式异常时退回用今天
        base = datetime.now()
    return (base + timedelta(days=days)).strftime("%Y-%m-%d")  # 加上周期天数后格式化


def _save_upload_file(src_path: Path, original_name: str) -> tuple[Path, int]:
    """
    把上传的文件保存到数据目录。

    命名策略：按 年-月 分子目录 + UUID 前缀 + 消毒后的原文件名。
    - 分目录：避免单目录下几万个文件导致文件系统性能下降
    - UUID：防止同名文件互相覆盖
    - 消毒：防止路径穿越攻击（如文件名里带 ../../etc/passwd）

    返回：
        (保存后的完整路径, 文件字节数)
    """
    subdir = config.UPLOAD_DIR / datetime.now().strftime("%Y-%m")  # 按年月分目录
    subdir.mkdir(parents=True, exist_ok=True)  # 确保目录存在

    # 文件名消毒：只保留中文、字母、数字和少数安全符号，其余一律替换成下划线
    safe_name = "".join(
        c if (c.isalnum() or c in "._-" or "\u4e00" <= c <= "\u9fff") else "_"
        for c in original_name
    )[-100:]  # 只取末尾 100 字符，防止超长文件名撑爆文件系统

    dest = subdir / f"{uuid.uuid4().hex[:8]}_{safe_name}"  # 拼上 8 位随机前缀
    shutil.copy2(str(src_path), str(dest))  # copy2 会同时复制文件的修改时间等元信息
    return dest, dest.stat().st_size  # 返回路径和文件大小


# ============================================================
# 三、核心入库函数
# ============================================================

def ingest_file(
    file_path: str | Path,
    meta: dict,
    user_id: Optional[int] = None,
    original_name: Optional[str] = None,
    auto_publish: bool = False,
) -> dict:
    """
    单文件入库的主流程。

    参数：
        file_path:     待入库文件的路径
        meta:          元数据字典
        user_id:       上传人 ID
        original_name: 原始文件名，不传则取路径中的文件名
        auto_publish:  是否跳过审核直接发布（批量导入可信来源时使用）
    返回：
        {"ok": bool, "doc_id": int, "message": str, "errors": [...], "stats": {...}}
    """
    src = Path(file_path)  # 转成 Path 对象
    file_name = original_name or src.name  # 原始文件名

    # ---------- 步骤 1：元数据校验（最先做，成本最低，快速失败）----------
    errors = validate_metadata(meta)  # 执行校验
    if errors:  # 有错误就直接拒绝，不浪费后续的解析和向量化成本
        return {
            "ok": False,
            "doc_id": None,
            "message": "元数据不符合入库规范，已拒绝",
            "errors": errors,  # 把所有错误一次性返回，用户可以一次改完
            "stats": {},
        }

    # ---------- 步骤 2：文件基础校验 ----------
    if not src.exists():  # 文件不存在
        return {"ok": False, "doc_id": None, "message": f"文件不存在：{src}", "errors": [], "stats": {}}

    ext = src.suffix.lower()  # 取扩展名
    if ext not in config.ALLOWED_EXTENSIONS:  # 不在白名单（安全措施）
        return {
            "ok": False, "doc_id": None,
            "message": f"不支持的文件格式 {ext}",
            "errors": [f"允许的格式：{'、'.join(sorted(config.ALLOWED_EXTENSIONS))}"],
            "stats": {},
        }

    file_size = src.stat().st_size  # 文件字节数
    max_bytes = config.MAX_FILE_SIZE_MB * 1024 * 1024  # 换算成字节的上限
    if file_size > max_bytes:  # 超过大小限制
        return {
            "ok": False, "doc_id": None,
            "message": f"文件过大（{file_size / 1024 / 1024:.1f} MB），上限 {config.MAX_FILE_SIZE_MB} MB",
            "errors": [], "stats": {},
        }

    # ---------- 步骤 3：解析文档 ----------
    try:
        parsed = parse_file(src)  # 调用解析器
    except ParseError as exc:  # 解析失败
        return {
            "ok": False, "doc_id": None,
            "message": f"文档解析失败：{exc}",
            # 给出可操作的建议，而不是甩个错误就完事
            "errors": ["可尝试：①转换为 docx/文字版 PDF ②使用『人工补充摘要』方式入库"],
            "stats": {},
        }

    plain_text = parsed["plain_text"]  # 提取出的全文
    blocks = parsed["blocks"]  # 结构化块

    # ---------- 步骤 3.5：确定 body_text 权威源 ----------
    # 设计关键：body_text 是"在线编辑器可编辑的正文权威源"，必须与编辑器读写的文本一致，
    # 否则版本 diff / 回滚会出现"存的是纯文本、比的是带标记的源码"的不一致。
    # - 文本类文件（md/html/txt）：直接读原文件原始内容（保留 #、空行等标记），编辑器打开即所见
    # - 二进制文件（pdf/docx 等）：没有可读源码，只能退化存解析出的纯文本
    TEXT_EXTS = {".md", ".markdown", ".html", ".htm", ".txt", ".text"}  # 有原始文本源码的扩展名
    body_text = None  # 先置空，下面尝试读取原文件源码
    if src.suffix.lower() in TEXT_EXTS:  # 是文本类文件
        try:
            body_text = src.read_text(encoding="utf-8", errors="ignore")  # 读原文件原始字节为文本
        except OSError:  # 读不到就退回纯文本（极少见）
            body_text = None
    if not body_text:  # 没有源码（二进制文件或读取失败）
        body_text = plain_text  # 兜底：用解析出的纯文本作为权威源

    # ---------- 步骤 4：质量红线检查（规范第三章）----------
    if len(plain_text.strip()) < config.MIN_CONTENT_LENGTH:  # 正文太短
        return {
            "ok": False, "doc_id": None,
            "message": f"正文有效内容仅 {len(plain_text.strip())} 字，低于最低要求 {config.MIN_CONTENT_LENGTH} 字",
            "errors": ["触碰规范红线 1：内容过短的文档不具备知识价值，不予入库"],
            "stats": {},
        }

    content_hash = _content_hash(plain_text)  # 计算内容哈希
    # 查找是否已存在相同内容的文档（排除已下架的）
    existing = db.query_one(
        "SELECT id, title FROM documents WHERE content_hash = ? AND status != 'archived'",
        (content_hash,),
    )
    if existing:  # 内容重复
        return {
            "ok": False, "doc_id": existing["id"],
            "message": f"内容与已有文档重复：#{existing['id']}《{existing['title']}》",
            "errors": ["触碰规范红线 3：重复内容不予重复入库。如需更新，请使用『更新版本』功能"],
            "stats": {},
        }

    # ---------- 步骤 5：保存原始文件 ----------
    try:
        saved_path, file_size = _save_upload_file(src, file_name)  # 落盘
    except OSError as exc:  # 磁盘满、权限不足等
        return {"ok": False, "doc_id": None, "message": f"文件保存失败：{exc}", "errors": [], "stats": {}}

    # ---------- 步骤 6：写入文档主记录 ----------
    now = audit.now_iso()  # 当前时间
    # 摘要：优先用用户填写的，没填就自动从正文生成
    summary = (meta.get("summary") or "").strip() or make_summary(plain_text)
    # 计算下次复审日期
    next_review = _calc_next_review(meta["category_l1"], meta.get("effective_date"))
    # 状态：auto_publish 为真则直接发布，否则进入待审核
    status = "published" if auto_publish else "pending_review"

    doc_id = db.execute(
        """
        INSERT INTO documents
            (title, category_l1, category_l2, department_id, security_level, quality_level,
             owner, effective_date, expire_date, next_review_at, source, summary, status,
             content_hash, file_path, file_name, file_size, file_ext, content_length,
             body_text, chunk_count, version, created_by, created_at, updated_at)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,0,1,?,?,?)
        """,
        (
            meta["title"].strip(),  # 标题
            meta["category_l1"],  # 一级分类
            meta.get("category_l2"),  # 二级分类
            int(meta["department_id"]),  # 部门 ID
            meta["security_level"],  # 密级
            meta.get("quality_level", "normal"),  # 质量等级，默认 normal
            meta["owner"].strip(),  # 责任人
            meta.get("effective_date"),  # 生效日期
            meta.get("expire_date"),  # 失效日期
            next_review,  # 下次复审日期
            meta.get("source"),  # 来源
            summary,  # 摘要
            status,  # 状态
            content_hash,  # 内容哈希
            str(saved_path),  # 文件路径
            file_name,  # 原始文件名
            file_size,  # 文件大小
            ext,  # 扩展名
            len(plain_text),  # 正文字数
            body_text,  # 正文权威源：文本文件存原始源码（保留标记），编辑器打开即所见；二进制文件存解析全文
            user_id,  # 上传人
            now,  # 创建时间
            now,  # 更新时间
        ),
    )

    # ---------- 步骤 7：切片 ----------
    chunks = chunk_blocks(blocks, doc_title=meta["title"].strip())  # 执行切片
    if not chunks:  # 切片结果为空（理论上不会，因为前面已校验过长度）
        db.execute("UPDATE documents SET status='rejected', parse_error=? WHERE id=?",
                   ("切片结果为空", doc_id))
        return {"ok": False, "doc_id": doc_id, "message": "文档切片失败，未生成任何知识片段", "errors": [], "stats": {}}

    # ---------- 步骤 8：写入切片表与全文索引 ----------
    chunk_ids: list[int] = []  # 记录新插入的切片 ID，后续写向量要用
    for c in chunks:  # 逐条插入（需要拿到每条的自增 ID，所以不能用 executemany）
        cid = db.execute(
            """
            INSERT INTO chunks (doc_id, seq, heading_path, anchor, content, char_count, created_at)
            VALUES (?,?,?,?,?,?,?)
            """,
            (doc_id, c.seq, c.heading_path, c.anchor, c.content, c.char_count, now),
        )
        chunk_ids.append(cid)  # 记录 ID
        # 写入 FTS5 全文索引。存的是 jieba 分词后的文本（详见 tokenizer 模块说明）
        db.execute(
            "INSERT INTO chunks_fts (tokenized, chunk_id, doc_id) VALUES (?,?,?)",
            (tokenizer.tokenize_to_string(c.searchable_text()), cid, doc_id),
        )

    # ---------- 步骤 9：向量化 ----------
    vector_ok = False  # 标记向量化是否成功
    vector_msg = ""  # 向量化的结果说明
    try:
        texts = [c.searchable_text() for c in chunks]  # 取出所有待向量化的文本
        vectors = embedding.embed_texts(texts)  # 批量向量化
        # 组装批量插入的参数
        rows = [
            (chunk_ids[i], doc_id, len(vectors[i]), config.EMBED_MODEL, _pack_vector(vectors[i]), now)
            for i in range(len(chunk_ids))
        ]
        db.execute_many(
            """
            INSERT OR REPLACE INTO chunk_vectors (chunk_id, doc_id, dim, model, vector, created_at)
            VALUES (?,?,?,?,?,?)
            """,
            rows,
        )
        vector_ok = True  # 标记成功
    except Exception as exc:  # 向量化失败（网络问题、密钥错误、限流等）
        # 关键设计：向量化失败不回滚整个入库！
        # 文档仍然可以通过关键词（BM25）检索到，只是暂时没有语义检索能力。
        # 这是 NFR-1"优雅降级"的具体体现——部分能力可用远好过完全不可用。
        vector_msg = f"（向量化失败：{exc}，该文档暂时仅支持关键词检索，可稍后在管理页重建索引）"

    # ---------- 步骤 10：更新统计并完成 ----------
    db.execute("UPDATE documents SET chunk_count = ?, updated_at = ? WHERE id = ?",
               (len(chunks), now, doc_id))

    # ---------- 步骤 11：解析交叉引用（P1）----------
    # 用 body_text（原始源码，含 [[...]] 语法）重建本文档的出链关系
    link_count = extract_doc_links(doc_id, body_text)

    return {
        "ok": True,  # 入库成功
        "doc_id": doc_id,  # 文档 ID
        "message": f"入库成功：生成 {len(chunks)} 个知识片段{vector_msg}",
        "errors": [],
        "stats": {  # 统计信息，供界面展示
            "chunks": len(chunks),  # 切片数
            "chars": len(plain_text),  # 字数
            "vector_ok": vector_ok,  # 向量化是否成功
            "status": status,  # 当前状态
        },
    }


def ingest_batch(
    items: list[dict],
    user_id: Optional[int] = None,
    auto_publish: bool = False,
) -> dict:
    """
    批量入库（SRS FR-3.2）。

    核心设计：**单份失败不中断整批**。
    100 份文档里有 3 份元数据不合规，不能让另外 97 份也白传。
    失败项单独列出，用户修正后重新导入即可（已成功的会被哈希去重自动跳过）。

    参数：
        items: [{"file_path": "...", "meta": {...}, "original_name": "..."}, ...]
    返回：
        {"total": N, "success": N, "failed": N, "results": [...]}
    """
    results: list[dict] = []  # 每份文档的处理结果
    success_count = 0  # 成功计数

    for idx, item in enumerate(items, start=1):  # 逐份处理，序号从 1 开始便于用户对照
        try:
            r = ingest_file(  # 调用单文件入库
                item["file_path"],
                item["meta"],
                user_id=user_id,
                original_name=item.get("original_name"),
                auto_publish=auto_publish,
            )
        except Exception as exc:  # 兜底捕获所有意外异常，绝不让一份文档的问题炸掉整批
            r = {"ok": False, "doc_id": None, "message": f"处理异常：{exc}", "errors": [], "stats": {}}

        # 记录本份的结果
        results.append({
            "index": idx,  # 序号
            "file": item.get("original_name") or Path(item["file_path"]).name,  # 文件名
            "title": item["meta"].get("title", ""),  # 标题
            "ok": r["ok"],  # 是否成功
            "message": r["message"],  # 结果说明
            "errors": r.get("errors", []),  # 详细错误
            "doc_id": r.get("doc_id"),  # 文档 ID
        })
        if r["ok"]:  # 成功则计数
            success_count += 1

    return {
        "total": len(items),  # 总数
        "success": success_count,  # 成功数
        "failed": len(items) - success_count,  # 失败数
        "results": results,  # 明细
    }


def reindex_document(doc_id: int) -> dict:
    """
    重建单个文档的索引（FR-9.3）。

    使用场景：
    1. 切换了 embedding 模型，旧向量作废，需要重新生成
    2. 入库时向量化失败，稍后补做
    3. 调整了切片参数，需要重新切分

    过程：清空旧切片与向量 → 重新解析 → 重新切片 → 重新向量化
    """
    doc = db.query_one("SELECT * FROM documents WHERE id = ?", (doc_id,))  # 查出文档
    if not doc:  # 文档不存在
        return {"ok": False, "message": f"文档 #{doc_id} 不存在"}

    file_path = doc["file_path"]  # 原始文件路径
    if not file_path or not Path(file_path).exists():  # 原件丢失，无法重建
        return {"ok": False, "message": "原始文件已丢失，无法重建索引"}

    try:
        parsed = parse_file(file_path)  # 重新解析
    except ParseError as exc:  # 解析失败
        return {"ok": False, "message": f"重新解析失败：{exc}"}

    # ---- 清理旧数据 ----
    db.execute("DELETE FROM chunk_vectors WHERE doc_id = ?", (doc_id,))  # 删旧向量
    db.execute("DELETE FROM chunks_fts WHERE doc_id = ?", (doc_id,))  # 删旧全文索引
    db.execute("DELETE FROM chunks WHERE doc_id = ?", (doc_id,))  # 删旧切片

    # ---- 重新切片入库 ----
    now = audit.now_iso()  # 当前时间
    chunks = chunk_blocks(parsed["blocks"], doc_title=doc["title"])  # 重新切片
    chunk_ids: list[int] = []  # 新切片 ID 列表
    for c in chunks:  # 逐条插入
        cid = db.execute(
            "INSERT INTO chunks (doc_id, seq, heading_path, content, char_count, created_at) VALUES (?,?,?,?,?,?)",
            (doc_id, c.seq, c.heading_path, c.content, c.char_count, now),
        )
        chunk_ids.append(cid)
        db.execute(
            "INSERT INTO chunks_fts (tokenized, chunk_id, doc_id) VALUES (?,?,?)",
            (tokenizer.tokenize_to_string(c.searchable_text()), cid, doc_id),
        )

    # ---- 重新向量化 ----
    vector_ok = False  # 成功标记
    msg_extra = ""  # 附加说明
    try:
        vectors = embedding.embed_texts([c.searchable_text() for c in chunks])  # 批量向量化
        db.execute_many(
            "INSERT OR REPLACE INTO chunk_vectors (chunk_id, doc_id, dim, model, vector, created_at) VALUES (?,?,?,?,?,?)",
            [
                (chunk_ids[i], doc_id, len(vectors[i]), config.EMBED_MODEL, _pack_vector(vectors[i]), now)
                for i in range(len(chunk_ids))
            ],
        )
        vector_ok = True
    except Exception as exc:  # 向量化失败
        msg_extra = f"（向量化失败：{exc}）"

    # 更新文档的切片计数
    db.execute("UPDATE documents SET chunk_count = ?, updated_at = ? WHERE id = ?",
               (len(chunks), now, doc_id))

    return {
        "ok": True,
        "message": f"索引重建完成：{len(chunks)} 个片段{msg_extra}",
        "chunks": len(chunks),
        "vector_ok": vector_ok,
    }


def reindex_all(only_missing_vectors: bool = False) -> dict:
    """
    全量重建索引（FR-9.3）。
    切换 embedding 模型后必须执行，否则新旧向量混在一起，检索结果会完全错乱。

    参数：
        only_missing_vectors: True 表示只处理缺失向量的文档（用于补做失败项，速度快得多）
    """
    if only_missing_vectors:  # 只补缺失的
        # 找出有切片但没有向量的文档
        rows = db.query(
            """
            SELECT DISTINCT d.id FROM documents d
            JOIN chunks c ON c.doc_id = d.id
            LEFT JOIN chunk_vectors v ON v.chunk_id = c.id
            WHERE v.chunk_id IS NULL AND d.status != 'archived'
            """
        )
    else:  # 全部重建
        rows = db.query("SELECT id FROM documents WHERE status != 'archived'")

    total = len(rows)  # 待处理总数
    ok_count = 0  # 成功数
    failures: list[str] = []  # 失败明细

    for row in rows:  # 逐个处理
        result = reindex_document(row["id"])  # 重建单个文档
        if result["ok"]:  # 成功
            ok_count += 1
        else:  # 失败则记录原因
            failures.append(f"#{row['id']}: {result['message']}")

    return {
        "total": total,  # 总数
        "success": ok_count,  # 成功数
        "failed": total - ok_count,  # 失败数
        "failures": failures[:20],  # 只返回前 20 条失败明细，防止响应过大
    }


# ============================================================
# 五、在线编辑与版本管理（P0-2 文档版本历史 / P0-3 在线编辑器）
# ============================================================

def _snapshot_version(
    doc_id: int,
    version: int,
    title: Optional[str],
    content: Optional[str],
    file_path: Optional[str],
    changed_by: Optional[int],
    change_note: str,
    body_html: Optional[str] = None,
) -> None:
    """
    把某个版本的状态写入 doc_versions 表（只追加，不修改）。

    版本快照的语义：每次"离开"某版本（被编辑或被回滚覆盖）时，
    就把该版本的完整正文与标题存一份留底，保证随时可回溯、可回滚。
    富文本升级后额外存 body_html 快照，回滚时可还原编辑器与展示。
    """
    db.execute(
        """
        INSERT INTO doc_versions
            (doc_id, version, title, file_path, content, body_html, changed_by, change_note, created_at)
        VALUES (?,?,?,?,?,?,?,?,?)
        """,
        (doc_id, version, title, file_path, content, body_html, changed_by, change_note, audit.now_iso()),
    )


def _rebuild_from_text(
    doc_id: int,
    text: str,
    fmt: str,
    title: str,
    summary: Optional[str],
    user_id: Optional[int],
    new_version: int,
    change_note: str,
    body_html: Optional[str] = None,
) -> dict:
    """
    用一段纯文本重建文档的切片与向量索引（核心内部函数）。

    被 update_document 与 rollback_document 共用：二者都"先快照旧版本 → 再调本函数重建新版本"。

    参数：
        doc_id:       文档 ID
        text:         新的正文纯文本（Markdown / HTML / 纯文本，由 fmt 决定如何解析）
        fmt:          文本格式："md" / "html" / "txt"
        title:        新标题
        summary:      新摘要（None 表示自动生成）
        user_id:      操作人 ID（写入版本快照）
        new_version:  将要写入的新版本号
        change_note:  本次变更的说明（写入版本快照）
    返回：
        {"ok": bool, "doc_id": int, "version": int, "message": str, "chunks": int, "vector_ok": bool}
    """
    # ---- 步骤 1：按格式把纯文本解析为结构化块 ----
    try:
        if fmt == "html":  # HTML 格式
            blocks = _parse_html(text)
        elif fmt == "txt":  # 纯文本格式
            blocks = _parse_txt(text)
        else:  # 默认按 Markdown 解析（层级最清晰，最推荐）
            blocks = _parse_markdown(text)
    except Exception as exc:  # 解析异常（理论上三个解析器极少抛错，这里兜底）
        return {"ok": False, "doc_id": doc_id, "message": f"正文解析失败：{exc}", "chunks": 0, "vector_ok": False}

    # 把所有块的文本拼回全文，用于哈希与字数统计
    plain_text = "\n".join(b["text"] for b in blocks)
    if len(plain_text.strip()) < config.MIN_CONTENT_LENGTH:  # 正文过短，拒绝入库
        return {
            "ok": False, "doc_id": doc_id,
            "message": f"正文有效内容仅 {len(plain_text.strip())} 字，低于最低要求 {config.MIN_CONTENT_LENGTH} 字",
            "chunks": 0, "vector_ok": False,
        }

    content_hash = _content_hash(plain_text)  # 计算内容哈希
    now = audit.now_iso()  # 当前时间

    # ---- 步骤 2：清空该文档旧的切片、全文索引与向量（事务上整体替换）----
    db.execute("DELETE FROM chunk_vectors WHERE doc_id = ?", (doc_id,))
    db.execute("DELETE FROM chunks_fts WHERE doc_id = ?", (doc_id,))
    db.execute("DELETE FROM chunks WHERE doc_id = ?", (doc_id,))

    # ---- 步骤 3：重新切片并写入切片表 + 全文索引 ----
    chunks = chunk_blocks(blocks, doc_title=title)  # 重新切片
    if not chunks:  # 切片为空（边界情况）
        return {"ok": False, "doc_id": doc_id, "message": "重新切片失败，未生成任何知识片段", "chunks": 0, "vector_ok": False}
    chunk_ids: list[int] = []  # 新切片 ID 列表
    for c in chunks:  # 逐条插入
        cid = db.execute(
            "INSERT INTO chunks (doc_id, seq, heading_path, anchor, content, char_count, created_at) VALUES (?,?,?,?,?,?,?)",
            (doc_id, c.seq, c.heading_path, c.anchor, c.content, c.char_count, now),
        )
        chunk_ids.append(cid)
        # 写入 FTS5 全文索引（jieba 预分词后的文本）
        db.execute(
            "INSERT INTO chunks_fts (tokenized, chunk_id, doc_id) VALUES (?,?,?)",
            (tokenizer.tokenize_to_string(c.searchable_text()), cid, doc_id),
        )

    # ---- 步骤 4：重新向量化 ----
    vector_ok = False  # 向量化成功标记
    msg_extra = ""  # 附加说明
    try:
        vectors = embedding.embed_texts([c.searchable_text() for c in chunks])  # 批量向量化
        db.execute_many(
            "INSERT OR REPLACE INTO chunk_vectors (chunk_id, doc_id, dim, model, vector, created_at) VALUES (?,?,?,?,?,?)",
            [
                (chunk_ids[i], doc_id, len(vectors[i]), config.EMBED_MODEL, _pack_vector(vectors[i]), now)
                for i in range(len(chunk_ids))
            ],
        )
        vector_ok = True
    except Exception as exc:  # 向量化失败：优雅降级，仅关键词检索仍可用
        msg_extra = f"（向量化失败：{exc}，该文档暂时仅支持关键词检索）"

    # ---- 步骤 5：更新文档主记录（版本号+1、正文、哈希、字数、摘要、时间戳）----
    new_summary = (summary or "").strip() or make_summary(plain_text)  # 没填摘要就自动生成
    db.execute(
        """
        UPDATE documents
        SET version=?, title=?, summary=?, content_hash=?, content_length=?, body_text=?,
            body_html=?, chunk_count=?, updated_at=?
        WHERE id=?
        """,
        (new_version, title, new_summary, content_hash, len(plain_text), text,
         body_html, len(chunks), now, doc_id),
    )

    # 重建交叉引用：用新正文（含 [[...]] 语法）刷新本文档的出链关系（P1）
    extract_doc_links(doc_id, text)

    return {
        "ok": True,  # 重建成功
        "doc_id": doc_id,  # 文档 ID
        "version": new_version,  # 新版本号
        "message": f"已更新至 v{new_version}：生成 {len(chunks)} 个知识片段{msg_extra}",
        "chunks": len(chunks),  # 切片数
        "vector_ok": vector_ok,  # 向量化是否成功
    }


def update_document(
    doc_id: int,
    text: str,
    fmt: str = "md",
    title: Optional[str] = None,
    summary: Optional[str] = None,
    change_note: Optional[str] = None,
    user_id: Optional[int] = None,
    html: Optional[str] = None,
) -> dict:
    """
    在线编辑文档：用新正文替换旧正文，并产生一个版本快照。

    流程：
        1. 校验文档存在、正文长度达标
        2. 把"当前版本"的状态（标题 + 正文 body_text + body_html）快照进 doc_versions
        3. 用新文本重建切片与向量索引，版本号 +1

    富文本升级说明：
        若传入 html（Quill 产出的富文本），则：
          - 用 sanitize_html 净化后作为 body_html 存（编辑器回显/展示用）；
          - 用 html_to_text 剥标签得到纯文本 body_text（切片/检索/diff 用，保持不变）；
          - 切片按纯文本（fmt="txt"）处理，避免 HTML 标签污染检索。
        若只传 text（兼容旧调用方/Markdown 上传），则行为与升级前一致。

    参数：
        doc_id:       要编辑的文档 ID
        text:         新正文纯文本（html 为空时必填）
        fmt:          文本格式 md/html/txt（html 为空时生效）
        title:        新标题（None 表示沿用原标题）
        summary:      新摘要（None 表示自动生成）
        change_note:  变更说明（会写入版本快照，方便回溯时理解"改了啥"）
        user_id:      编辑人 ID
        html:         富文本 HTML（可选，Quill 产出）
    返回：
        {"ok": bool, "doc_id": int, "version": int, "message": str, ...}
    """
    doc = db.query_one("SELECT * FROM documents WHERE id = ?", (doc_id,))  # 查出文档
    if not doc:  # 文档不存在
        return {"ok": False, "doc_id": doc_id, "message": "文档不存在", "chunks": 0, "vector_ok": False}

    # 富文本优先：有 html 时，由 html 派生 body_text 与 body_html，切片按纯文本处理
    if html and html.strip():
        body_html = sanitize_html(html)        # 净化后的富文本 HTML
        text = html_to_text(body_html)         # 剥标签得到纯文本（检索/切片权威源）
        fmt = "txt"                            # 切片按纯文本，避免标签污染
    else:
        body_html = None                       # 非富文本，body_html 置空

    if len((text or "").strip()) < config.MIN_CONTENT_LENGTH:  # 正文过短
        return {
            "ok": False, "doc_id": doc_id,
            "message": f"正文有效内容仅 {len((text or '').strip())} 字，低于最低要求 {config.MIN_CONTENT_LENGTH} 字",
            "chunks": 0, "vector_ok": False,
        }

    # 取出"当前版本"的正文（优先用 body_text 权威源，缺失则尝试拼回），作为离场前的快照
    old_text = doc["body_text"] or _assemble_text_from_chunks(doc_id)
    old_html = doc["body_html"] if "body_html" in doc.keys() else None  # 当前版本富文本快照（老库可能无此列）
    new_version = doc["version"] + 1  # 新版本号
    note = change_note or f"编辑更新至 v{new_version}"  # 变更说明兜底
    # 先快照当前（即将离场的）版本（含 body_html，保证回滚可还原富文本）
    _snapshot_version(
        doc_id, doc["version"], doc["title"], old_text, doc["file_path"],
        user_id, f"【v{doc['version']}→v{new_version}】{note}", body_html=old_html,
    )
    # 再重建为新版本（写入新的 body_html）
    return _rebuild_from_text(
        doc_id, text, fmt, title or doc["title"], summary, user_id, new_version, note, body_html=body_html,
    )


def rollback_document(doc_id: int, version_id: int, user_id: Optional[int] = None) -> dict:
    """
    回滚文档到某个历史版本。

    流程：
        1. 校验文档与版本均存在、且版本属于该文档
        2. 把"当前版本"的状态快照进 doc_versions（回滚本身也是一次变更，需留痕）
        3. 用历史版本存储的正文重建切片与向量，版本号 +1

    参数：
        doc_id:     文档 ID
        version_id: doc_versions 表中的版本记录 ID
        user_id:    操作人 ID
    返回：
        {"ok": bool, "doc_id": int, "version": int, "rollback_to": int, "message": str, ...}
    """
    doc = db.query_one("SELECT * FROM documents WHERE id = ?", (doc_id,))  # 查出文档
    if not doc:  # 文档不存在
        return {"ok": False, "doc_id": doc_id, "message": "文档不存在", "chunks": 0, "vector_ok": False}
    ver = db.query_one("SELECT * FROM doc_versions WHERE id = ? AND doc_id = ?", (version_id, doc_id))
    if not ver:  # 版本不存在或不属于该文档
        return {"ok": False, "doc_id": doc_id, "message": "目标版本不存在", "chunks": 0, "vector_ok": False}

    # 先快照当前（即将被覆盖）的版本
    old_text = doc["body_text"] or _assemble_text_from_chunks(doc_id)
    new_version = doc["version"] + 1
    _snapshot_version(
        doc_id, doc["version"], doc["title"], old_text, doc["file_path"],
        user_id, f"【回滚前 v{doc['version']}】准备回滚到 v{ver['version']}",
    )
    # 用历史版本的正文重建（历史正文是 Markdown/纯文本，按 md 解析最稳）
    res = _rebuild_from_text(
        doc_id, ver["content"] or "", "md", ver["title"] or doc["title"],
        None, user_id, new_version, f"回滚到 v{ver['version']}",
        body_html=ver["body_html"] if "body_html" in ver.keys() else None,  # 历史版本富文本快照（老库可能无此列）
    )
    res["rollback_to"] = ver["version"]  # 附带"回滚到了哪个版本"
    return res


def _assemble_text_from_chunks(doc_id: int) -> str:
    """
    兜底函数：当 body_text 缺失时，从 chunks 表按 seq 顺序拼回正文。

    注意：chunks 表只存切片内容（不含标题路径里的标题文字），拼回的文本会丢失章节标题。
    因此正常情况应以 body_text 为准；此函数仅用于老库升级、body_text 为空的极少数场景。
    """
    rows = db.query(
        "SELECT content FROM chunks WHERE doc_id = ? ORDER BY seq", (doc_id,)
    )
    return "\n\n".join(r["content"] for r in rows)  # 按切片顺序用空行连接


# ============================================================
# 六、交叉引用解析（P1 交叉引用）
# ============================================================

# 匹配正文中的 [[...]] 语法（方括号内不能再有 ]]，避免贪婪越界）
_DOC_LINK_RE = re.compile(r"\[\[([^\]]+)\]\]")


def extract_doc_links(doc_id: int, text: str) -> int:
    """
    解析正文里的交叉引用语法，重建「本文档 → 目标文档」的引用关系。

    支持的语法（灵感来自 BookStack / Wiki 的双向链接）：
        [[文档标题]]       —— 按标题精确定位目标文档
        [[doc:123]]        —— 按文档 ID 定位（最稳，标题改名也不失效）
        [[目标|显示文本]]   —— 带自定义显示文本（显示文本仅用于展示，不影响定位）

    行为：
        - 每次调用先清空该文档的旧出链，再按当前正文重建，保证引用关系与正文严格一致
        - 失效引用（目标不存在 / 已归档 / 自引用 / 重复）被安静忽略，不影响入库成功
    返回：
        成功建立的引用条数。
    """
    # 先清空旧出链，再重建。这是「以正文为准」的关键：删掉正文里的链接，
    # 对应的引用关系也会随之消失，不会出现悬挂引用。
    db.execute("DELETE FROM doc_links WHERE from_doc_id = ?", (doc_id,))
    if not text:  # 正文为空（如纯二进制无源码）直接返回
        return 0

    now = audit.now_iso()  # 当前时间
    count = 0  # 已建立的引用数
    seen: set[int] = set()  # 去重集合：同一目标文档只记录一次

    for m in _DOC_LINK_RE.finditer(text):  # 逐个匹配 [[...]]
        inner = m.group(1).strip()  # 取出方括号里的内容并去空白
        if not inner:  # 空内容跳过
            continue
        # 拆分显示文本：[[目标|显示文本]]
        if "|" in inner:  # 含竖线表示带自定义显示文本
            target_part, link_text = inner.split("|", 1)  # 只按第一个竖线切
            target_part = target_part.strip()  # 目标部分去空白
            link_text = link_text.strip()  # 显示文本去空白
        else:  # 没有竖线，显示文本留空
            target_part, link_text = inner, ""

        to_doc_id = None  # 目标文档 ID，先置空
        # 情况1：[[doc:123]] 或 [[123]] —— 按 ID 定位，最稳定
        id_str = target_part[4:].strip() if target_part.lower().startswith("doc:") else target_part
        if id_str.isdigit():  # 是纯数字
            cand = db.query_one(
                "SELECT id FROM documents WHERE id = ? AND status != 'archived'",
                (int(id_str),),
            )
            if cand:  # 找到且未归档
                to_doc_id = cand["id"]
        else:
            # 情况2：[[文档标题]] —— 按标题精确定位（忽略大小写）
            cand = db.query_one(
                "SELECT id FROM documents WHERE LOWER(title) = LOWER(?) AND status != 'archived' LIMIT 1",
                (target_part,),
            )
            if cand:  # 找到且未归档
                to_doc_id = cand["id"]

        # 目标不存在、指向自己、或已经记录过的，统统跳过
        if not to_doc_id or to_doc_id == doc_id or to_doc_id in seen:
            continue
        seen.add(to_doc_id)  # 标记已处理，去重
        # 写入一条出链记录
        db.execute(
            "INSERT INTO doc_links (from_doc_id, to_doc_id, link_text, created_at) VALUES (?,?,?,?)",
            (doc_id, to_doc_id, link_text or None, now),
        )
        count += 1  # 计数 +1

    return count  # 返回建立的引用数
