# -*- coding: utf-8 -*-
"""
文档解析器
==========
职责：把不同格式的文档统一解析成"带层级结构的文本块"列表。

为什么要保留层级结构（SRS FR-3.3）：
如果只是把文档拍平成一大坨文本再按字数硬切，切片会丢失上下文。
比如切到"其中甲类占比 60%"这一段，AI 根本不知道这是在讲哪一章的什么内容。
保留标题层级后，每个切片都能带上"第二章 采购规则 > 2.1 报价规则"这样的路径，
检索命中率和答案准确度都会显著提升。

统一输出格式：
    [{"type": "heading"/"para"/"table", "level": 1-6, "text": "..."}]
"""

import re  # 正则表达式
from pathlib import Path  # 路径处理
from typing import Any  # 类型注解


class ParseError(Exception):
    """文档解析失败异常。上层捕获后可走"人工补摘要"降级路径（FR-3.6）。"""
    pass


# SUPPORTED_EXTS 是本解析器支持的文件扩展名集合
SUPPORTED_EXTS = {".docx", ".pdf", ".xlsx", ".xls", ".md", ".txt", ".html", ".htm"}


# ============================================================
# 一、Word 文档解析
# ============================================================

def _parse_docx(path: Path) -> list[dict]:
    """
    解析 .docx 文件。

    关键点：通过段落的样式名判断标题层级。
    Word 中"标题1/标题2"（英文版 Heading 1/2）是有样式标记的，
    而手工加粗放大的假标题没有标记——这也是为什么《入库规范》第四章
    强制要求使用标题样式，那不是形式主义，是技术必需。
    """
    try:
        import docx  # python-docx 库，延迟导入避免不用 docx 时也要加载
    except ImportError as exc:  # 依赖缺失
        raise ParseError("缺少 python-docx 依赖，请执行 pip install python-docx") from exc

    try:
        document = docx.Document(str(path))  # 打开 docx 文件
    except Exception as exc:  # 文件损坏或格式不对
        raise ParseError(f"无法打开 Word 文档：{exc}") from exc

    blocks: list[dict] = []  # 存放解析出的结构化块

    for para in document.paragraphs:  # 遍历文档中的所有段落
        text = para.text.strip()  # 取出文本并去除首尾空白
        if not text:  # 跳过空段落
            continue
        style_name = (para.style.name or "") if para.style else ""  # 取样式名，可能为空
        # 判断是否为标题样式。中文版 Word 样式名是"标题 1"，英文版是"Heading 1"
        heading_match = re.match(r"^(?:Heading|标题)\s*(\d+)", style_name, re.IGNORECASE)
        if heading_match:  # 是标题
            level = int(heading_match.group(1))  # 提取层级数字
            level = min(max(level, 1), 6)  # 限制在 1-6 之间，防止异常值
            blocks.append({"type": "heading", "level": level, "text": text})  # 记为标题块
        elif style_name in ("Title", "标题"):  # 文档主标题样式
            blocks.append({"type": "heading", "level": 1, "text": text})  # 视为一级标题
        else:  # 普通正文段落
            blocks.append({"type": "para", "level": 0, "text": text})

    # 解析文档中的表格。表格常常承载关键数据（如价格表、时间表），不能丢
    for table in document.tables:
        rows_text: list[str] = []  # 存放每行的文本
        for row in table.rows:  # 遍历表格行
            # 取出每个单元格的文本并去空白
            cells = [cell.text.strip().replace("\n", " ") for cell in row.cells]
            if any(cells):  # 整行都是空则跳过
                rows_text.append(" | ".join(cells))  # 用竖线分隔单元格，还原表格感
        if rows_text:  # 表格非空才记录
            blocks.append({"type": "table", "level": 0, "text": "\n".join(rows_text)})

    return blocks


# ============================================================
# 二、PDF 解析
# ============================================================

def _parse_pdf(path: Path) -> list[dict]:
    """
    解析 .pdf 文件。

    局限说明：
    PDF 是"打印格式"，本身不保留语义结构，无法可靠地识别标题层级。
    这里采用启发式规则：短行且符合章节编号特征的，推断为标题。
    如果是扫描件（图片型 PDF），提取不出文字，会抛出 ParseError 走降级路径。
    """
    try:
        from pypdf import PdfReader  # pypdf 库
    except ImportError as exc:
        raise ParseError("缺少 pypdf 依赖，请执行 pip install pypdf") from exc

    try:
        reader = PdfReader(str(path))  # 打开 PDF
    except Exception as exc:
        raise ParseError(f"无法打开 PDF：{exc}") from exc

    blocks: list[dict] = []  # 结果块列表
    total_chars = 0  # 累计提取出的字符数，用于判断是否为扫描件

    for page_no, page in enumerate(reader.pages, start=1):  # 逐页遍历，页码从 1 开始
        try:
            text = page.extract_text() or ""  # 提取本页文本，失败返回空串
        except Exception:  # 单页解析失败不影响其他页
            continue
        total_chars += len(text)  # 累加字符数
        # 按换行拆分成行，逐行判断
        for raw_line in text.split("\n"):
            line = raw_line.strip()  # 去空白
            if not line:  # 跳过空行
                continue
            # 启发式标题识别：行长小于 40 字，且以典型章节编号开头
            is_heading = (
                len(line) < 40  # 标题通常较短
                and bool(re.match(
                    r"^(第[一二三四五六七八九十百]+[章节条部分]"  # 中文章节，如"第三章"
                    r"|[一二三四五六七八九十]+[、.．]"              # 中文序号，如"三、"
                    r"|\d+(\.\d+)*[\s、.．]"                        # 阿拉伯数字编号，如"3.2 "
                    r"|附录|前言|概述|摘要|目录)",                   # 常见特殊标题
                    line,
                ))
            )
            if is_heading:  # 判定为标题
                # 根据编号中点号的数量粗略推断层级："3.2.1" 有 2 个点 → 3 级
                dots = line.count(".")
                level = min(max(1, dots + 1), 4)  # 限制在 1-4 级
                blocks.append({"type": "heading", "level": level, "text": line})
            else:  # 判定为正文
                blocks.append({"type": "para", "level": 0, "text": line, "page": page_no})

    # 平均每页提取不到 20 个字符，基本可以断定是扫描件或图片型 PDF
    if total_chars < 20 * max(len(reader.pages), 1):
        raise ParseError(
            "PDF 中未提取到有效文字，可能是扫描件或图片型 PDF。"
            "请转换为可复制文本的版本，或使用『人工补充摘要』方式入库"
        )

    return blocks


# ============================================================
# 三、Excel 解析
# ============================================================

def _parse_xlsx(path: Path) -> list[dict]:
    """
    解析 .xlsx / .xls 文件。

    处理策略：每个工作表作为一个二级标题，表内每行拼成一段文本。
    第一行视为表头，后续每行以"列名：值"的形式展开，
    这样切片后每一行都是自解释的，AI 能理解"这个 3200 是单价不是数量"。
    """
    try:
        import openpyxl  # Excel 处理库
    except ImportError as exc:
        raise ParseError("缺少 openpyxl 依赖，请执行 pip install openpyxl") from exc

    try:
        # read_only=True 用流式读取，大文件不会撑爆内存；data_only=True 取公式计算结果而非公式本身
        wb = openpyxl.load_workbook(str(path), read_only=True, data_only=True)
    except Exception as exc:
        raise ParseError(f"无法打开 Excel：{exc}") from exc

    blocks: list[dict] = []  # 结果块列表

    for sheet_name in wb.sheetnames:  # 遍历所有工作表
        ws = wb[sheet_name]  # 取出工作表对象
        blocks.append({"type": "heading", "level": 1, "text": f"工作表：{sheet_name}"})  # 表名作为标题

        headers: list[str] = []  # 存放表头
        row_count = 0  # 已处理的数据行数

        for row in ws.iter_rows(values_only=True):  # 逐行迭代，values_only 只取值不取样式对象
            # 把每个单元格转成字符串，None 转空串
            values = ["" if v is None else str(v).strip() for v in row]
            if not any(values):  # 整行为空则跳过
                continue
            if not headers:  # 还没取到表头，第一行非空行即为表头
                headers = values
                blocks.append({"type": "para", "level": 0, "text": "表头：" + " | ".join(v for v in values if v)})
                continue
            # 把数据行拼成"列名：值"的形式
            pairs: list[str] = []
            for i, v in enumerate(values):  # 遍历本行每个单元格
                if not v:  # 空单元格跳过，避免噪音
                    continue
                # 有对应表头就用表头名，否则用"列N"
                col_name = headers[i] if i < len(headers) and headers[i] else f"列{i + 1}"
                pairs.append(f"{col_name}：{v}")
            if pairs:  # 有内容才记录
                blocks.append({"type": "para", "level": 0, "text": "；".join(pairs)})
            row_count += 1
            if row_count >= 3000:  # 超大表格截断保护，防止单个 Excel 撑爆索引
                blocks.append({"type": "para", "level": 0, "text": f"（该工作表数据超过 3000 行，已截断）"})
                break

    wb.close()  # 关闭工作簿释放文件句柄
    return blocks


# ============================================================
# 四、Markdown 解析
# ============================================================

def _parse_markdown(text: str) -> list[dict]:
    """
    解析 Markdown 文本。
    Markdown 的层级结构最清晰（# 的个数就是层级），是最理想的入库格式。
    """
    blocks: list[dict] = []  # 结果列表
    buffer: list[str] = []  # 累积连续的正文行，遇到标题或空行时打包成一段
    in_code_block = False  # 是否处于代码块内部（``` 包裹的部分）

    def flush() -> None:
        """内部函数：把缓冲区中累积的正文行打包成一个段落块。"""
        if buffer:  # 缓冲区非空才处理
            content = "\n".join(buffer).strip()  # 合并成一段并去首尾空白
            if content:  # 内容非空
                blocks.append({"type": "para", "level": 0, "text": content})
            buffer.clear()  # 清空缓冲区

    for raw_line in text.split("\n"):  # 逐行处理
        line = raw_line.rstrip()  # 去掉行尾空白（行首空白可能是缩进，有意义）
        if line.strip().startswith("```"):  # 代码块的起止标记
            in_code_block = not in_code_block  # 切换代码块状态
            buffer.append(line)  # 标记行本身也保留
            continue
        if in_code_block:  # 代码块内部原样保留，不做标题解析
            buffer.append(line)
            continue
        heading_match = re.match(r"^(#{1,6})\s+(.+)$", line)  # 匹配 # 开头的标题
        if heading_match:  # 是标题行
            flush()  # 先把之前累积的正文打包
            level = len(heading_match.group(1))  # # 的个数就是层级
            blocks.append({"type": "heading", "level": level, "text": heading_match.group(2).strip()})
            continue
        if not line.strip():  # 空行表示段落结束
            flush()  # 打包当前段落
            continue
        buffer.append(line)  # 普通行累积到缓冲区

    flush()  # 处理末尾剩余的内容
    return blocks


# ============================================================
# 五、HTML 解析
# ============================================================

def _parse_html(text: str) -> list[dict]:
    """
    解析 HTML 文本。
    利用 h1-h6 标签天然的层级语义，并剔除 script/style 等无意义内容。
    """
    try:
        from bs4 import BeautifulSoup  # HTML 解析库
    except ImportError:
        # 没装 bs4 时降级：用正则粗暴地去掉标签，保证功能不中断
        plain = re.sub(r"<script[^>]*>.*?</script>", " ", text, flags=re.S | re.I)  # 去脚本
        plain = re.sub(r"<style[^>]*>.*?</style>", " ", plain, flags=re.S | re.I)  # 去样式
        plain = re.sub(r"<[^>]+>", " ", plain)  # 去所有标签
        plain = re.sub(r"\s+", " ", plain).strip()  # 压缩连续空白
        return [{"type": "para", "level": 0, "text": plain}] if plain else []

    soup = BeautifulSoup(text, "html.parser")  # 用标准库解析器，避免额外依赖 lxml
    # 移除不含正文信息的标签
    for tag in soup(["script", "style", "nav", "footer", "noscript"]):
        tag.decompose()  # 从文档树中彻底删除该节点

    blocks: list[dict] = []  # 结果列表
    # 按文档顺序遍历所有关心的标签
    for elem in soup.find_all(["h1", "h2", "h3", "h4", "h5", "h6", "p", "li", "td", "pre"]):
        content = elem.get_text(" ", strip=True)  # 提取纯文本，标签间用空格分隔
        if not content:  # 空内容跳过
            continue
        if elem.name.startswith("h"):  # h1-h6 标题标签
            level = int(elem.name[1])  # 取标签名中的数字作为层级
            blocks.append({"type": "heading", "level": level, "text": content})
        else:  # 其他标签视为正文
            blocks.append({"type": "para", "level": 0, "text": content})

    return blocks


# ============================================================
# 六、纯文本解析
# ============================================================

def _parse_txt(text: str) -> list[dict]:
    """
    解析纯文本。
    没有任何格式标记可用，只能靠启发式规则识别章节标题。
    """
    blocks: list[dict] = []  # 结果列表
    buffer: list[str] = []  # 正文缓冲区

    def flush() -> None:
        """把缓冲区打包成段落。"""
        if buffer:
            content = "\n".join(buffer).strip()
            if content:
                blocks.append({"type": "para", "level": 0, "text": content})
            buffer.clear()

    for raw_line in text.split("\n"):  # 逐行处理
        line = raw_line.strip()  # 去空白
        if not line:  # 空行分段
            flush()
            continue
        # 启发式：短行 + 章节编号特征 = 标题
        if len(line) < 40 and re.match(
            r"^(第[一二三四五六七八九十百]+[章节条]|[一二三四五六七八九十]+[、.．]|\d+(\.\d+)*[\s、.．])",
            line,
        ):
            flush()  # 先打包之前的正文
            dots = line.count(".")  # 用点号数量推断层级
            blocks.append({"type": "heading", "level": min(max(1, dots + 1), 4), "text": line})
        else:
            buffer.append(line)  # 普通行入缓冲区

    flush()  # 处理末尾内容
    return blocks


# ============================================================
# 七、统一入口
# ============================================================

def parse_file(path: str | Path) -> dict[str, Any]:
    """
    解析文件的统一入口。根据扩展名分派到对应的解析器。

    参数：
        path: 文件路径
    返回：
        {
            "blocks": [结构化块列表],
            "plain_text": "全文纯文本",
            "char_count": 总字数,
            "ext": "扩展名"
        }
    异常：
        ParseError —— 格式不支持、文件损坏、无有效文本
    """
    p = Path(path)  # 统一转成 Path 对象
    if not p.exists():  # 文件不存在
        raise ParseError(f"文件不存在：{p}")

    ext = p.suffix.lower()  # 取扩展名并转小写
    if ext not in SUPPORTED_EXTS:  # 不在白名单中
        raise ParseError(f"不支持的文件格式：{ext}。支持的格式：{'、'.join(sorted(SUPPORTED_EXTS))}")

    # 按扩展名分派到对应解析器
    if ext == ".docx":
        blocks = _parse_docx(p)
    elif ext == ".pdf":
        blocks = _parse_pdf(p)
    elif ext in (".xlsx", ".xls"):
        blocks = _parse_xlsx(p)
    elif ext == ".md":
        # 文本类文件需要先读出内容。errors="ignore" 容忍个别乱码字符，不让整个文件解析失败
        blocks = _parse_markdown(p.read_text(encoding="utf-8", errors="ignore"))
    elif ext in (".html", ".htm"):
        blocks = _parse_html(p.read_text(encoding="utf-8", errors="ignore"))
    else:  # .txt
        blocks = _parse_txt(_read_text_with_fallback(p))

    # 把所有块的文本拼成全文，用于内容哈希、字数统计和摘要生成
    plain_text = "\n".join(b["text"] for b in blocks)

    if not plain_text.strip():  # 解析结果为空
        raise ParseError("文档中未提取到任何文字内容")

    return {
        "blocks": blocks,  # 结构化块
        "plain_text": plain_text,  # 全文
        "char_count": len(plain_text),  # 字数
        "ext": ext,  # 扩展名
    }


def _read_text_with_fallback(p: Path) -> str:
    """
    读取文本文件，自动尝试多种编码。

    背景：国内的 txt 文件很多是 GBK/GB18030 编码（Windows 记事本默认），
    直接用 UTF-8 读会全是乱码。这里按常见顺序依次尝试。
    """
    # 按优先级排列的编码列表
    for encoding in ("utf-8", "utf-8-sig", "gb18030", "gbk", "big5"):
        try:
            return p.read_text(encoding=encoding)  # 尝试用该编码读取
        except (UnicodeDecodeError, LookupError):  # 解码失败就试下一个
            continue
    # 所有编码都失败时，用 utf-8 强行读取并忽略错误字符，保证至少能拿到部分内容
    return p.read_text(encoding="utf-8", errors="ignore")
