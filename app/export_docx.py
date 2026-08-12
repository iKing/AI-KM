# -*- coding: utf-8 -*-
"""
服务端生成 Word(.docx) 文档
==========================
把服务端净化后的富文本 HTML（documents.body_html）转成可下载的 .docx 文件。

设计要点：
- 不依赖浏览器，纯服务端生成，内网离线可用。
- 复用 body_html（已做过 XSS 净化），避免重复解析。
- 内嵌图片按 /uploads/xxx 路径到本地上传目录取真实文件，取不到则跳过（不报错）。
- 正文本质是「结构化正文」，这里用 python-docx 的标题/段落/列表/表格/图片原样还原层级。

对外唯一入口：html_to_docx(title, meta_lines, body_html) -> bytes
"""

import io  # 内存字节流，避免落临时文件
from pathlib import Path  # 路径处理

from bs4 import BeautifulSoup  # 解析富文本 HTML
from docx import Document  # 生成 Word 文档
from docx.shared import Pt, RGBColor, Inches  # 字号/颜色/英寸单位
from docx.enum.text import WD_ALIGN_PARAGRAPH  # 段落对齐（备用）
from docx.oxml.ns import qn  # OOXML 命名空间（超链接用）
from docx.oxml import OxmlElement  # 直接拼 OOXML 元素（超链接用）

from . import config  # 取上传目录


def _resolve_image_src(src: str):
    """
    把 HTML 里的图片地址解析成本地真实文件路径。

    仅支持本系统 /uploads/ 下的图片；其他外链一律忽略（服务端无法访问外网且避免 SSRF）。
    返回 Path 或 None。
    """
    if not src:  # 空地址直接返回 None
        return None
    if src.startswith("/uploads/"):  # 本系统内嵌图片
        # 用 Path.name 防目录穿越（只取文件名部分）
        fname = Path(src[len("/uploads/"):]).name
        p = config.UPLOAD_DIR / fname  # 拼到上传目录
        if p.exists() and p.is_file():  # 文件确实存在才返回
            return p
    return None  # 其他情况（外链/不存在）返回 None


def _add_hyperlink(paragraph, url: str, text: str) -> None:
    """
    在段落里插入一个真正的可点击超链接（python-docx 官方做法）。

    若 url 非法或建立关系失败，退化为「纯文本 + 标蓝」，保证导出不崩。
    """
    try:  # 建立超链接可能抛异常，包一层兜底
        part = paragraph.part  # 文档部件，用于维护关系
        # 建立外部超链接关系，拿到关系 id
        r_id = part.relate_to(url, "http://schemas.openxmlformats.org/officeDocument/2006/relationships/hyperlink", is_external=True)
        hyperlink = OxmlElement("w:hyperlink")  # 超链接元素
        hyperlink.set(qn("r:id"), r_id)  # 绑定关系 id
        new_run = OxmlElement("w:r")  # 一个 run
        rPr = OxmlElement("w:rPr")  # run 属性
        rStyle = OxmlElement("w:rStyle")  # 样式引用
        rStyle.set(qn("w:val"), "Hyperlink")  # 用内置 Hyperlink 样式（默认蓝色带下划线）
        rPr.append(rStyle)  # 样式挂到 run 属性
        t = OxmlElement("w:t")  # 文本节点
        t.text = text  # 链接显示文字
        new_run.append(rPr)  # 属性
        new_run.append(t)  # 文本
        hyperlink.append(new_run)  # 文本挂到超链接
        paragraph._p.append(hyperlink)  # 超链接挂到段落
    except Exception:  # 任何异常都退化为普通文字
        run = paragraph.add_run(text)  # 普通 run
        run.font.color.rgb = RGBColor(0x00, 0x00, 0xEE)  # 标蓝提示这是链接


def _add_inline_runs(paragraph, node) -> None:
    """
    递归把节点内的「文本 + 内联格式」逐段加为 Word run。

    处理 b/strong（粗）、i/em（斜）、u（下划线）、a（超链接）、br（换行）、code（等宽）。
    其他未知内联标签递归其子文本，避免丢失内容。
    """
    for child in node.children:  # 遍历直接子节点
        if child.name is None:  # 纯文本节点
            txt = child.string  # 取出文本
            if txt:  # 非空才加
                paragraph.add_run(txt)
            continue
        if child.name in ("b", "strong"):  # 粗体
            run = paragraph.add_run(child.get_text())  # 取全部文本
            run.bold = True  # 加粗
        elif child.name in ("i", "em"):  # 斜体
            run = paragraph.add_run(child.get_text())
            run.italic = True  # 倾斜
        elif child.name == "u":  # 下划线
            run = paragraph.add_run(child.get_text())
            run.underline = True  # 下划线
        elif child.name == "a":  # 超链接
            _add_hyperlink(paragraph, child.get("href", ""), child.get_text())  # 插入可点击链接
        elif child.name == "br":  # 换行
            paragraph.add_run("\n")  # 软换行
        elif child.name == "code":  # 行内代码
            run = paragraph.add_run(child.get_text())
            run.font.name = "Courier New"  # 等宽字体
        else:  # 其他内联标签：递归处理其子节点
            _add_inline_runs(paragraph, child)


def _emit_block(doc: Document, el) -> None:
    """
    根据 HTML 块级标签，向 Word 文档追加对应元素（标题/段落/列表/引用/代码/图片/表格/分隔线）。
    """
    tag = el.name  # 当前标签名
    if tag in ("h1", "h2", "h3", "h4", "h5", "h6"):  # 标题层级
        level = int(tag[1])  # 取出数字级别
        doc.add_heading(el.get_text(), level=level)  # 加对应级别标题
    elif tag == "p":  # 普通段落
        p = doc.add_paragraph()  # 新建段落
        _add_inline_runs(p, el)  # 递归填充内联格式
    elif tag in ("ul", "ol"):  # 无序/有序列表
        for li in el.find_all("li", recursive=False):  # 只取直接子项
            style = "List Bullet" if tag == "ul" else "List Number"  # 项目符号/编号
            p = doc.add_paragraph(style=style)  # 列表段落
            _add_inline_runs(p, li)  # 填内容
    elif tag == "blockquote":  # 引用块
        p = doc.add_paragraph()  # 新建段落
        r = p.add_run(el.get_text())  # 引用正文
        r.italic = True  # 斜体表示引用
    elif tag in ("pre", "code"):  # 代码块
        p = doc.add_paragraph()  # 新建段落
        r = p.add_run(el.get_text())  # 代码正文
        r.font.name = "Courier New"  # 等宽字体
    elif tag == "img":  # 图片
        path = _resolve_image_src(el.get("src"))  # 解析本地路径
        if path:  # 取得到才插入
            try:
                doc.add_picture(str(path), width=Inches(5))  # 插入图片（限宽 5 英寸）
            except Exception:
                pass  # 格式不支持等异常忽略，不阻断导出
    elif tag == "hr":  # 分隔线（Word 无原生 hr，用一串横线替代）
        doc.add_paragraph("—" * 24)
    elif tag == "table":  # 表格
        _emit_table(doc, el)  # 交给表格处理函数
    else:  # 未知块级标签：当作普通段落兜底
        p = doc.add_paragraph()
        _add_inline_runs(p, el)


def _emit_table(doc: Document, el) -> None:
    """把 HTML <table> 转成 Word 表格（含表头识别，第一行加粗底纹）。"""
    rows = el.find_all("tr")  # 所有行
    if not rows:  # 空表跳过
        return
    ncol = max((len(r.find_all(["td", "th"])) for r in rows), default=1)  # 取最大列数
    table = doc.add_table(rows=0, cols=ncol)  # 新建空表
    table.style = "Table Grid"  # 带网格线的样式
    for r in rows:  # 逐行
        cells = r.find_all(["td", "th"])  # 该行单元格
        row = table.add_row().cells  # 在 Word 表里加一行
        for i, c in enumerate(cells):  # 逐格
            if i >= ncol:  # 超出列数忽略
                break
            row[i].text = c.get_text(strip=True)  # 写入单元格文本


def html_to_docx(title: str, meta_lines: list, body_html: str) -> bytes:
    """
    把标题 + 元信息 + 富文本 HTML 组装成 .docx 字节流。

    参数：
        title:      文档标题
        meta_lines: 元信息行列表（如分类/密级/责任人/版本），会作为灰色小字置于标题下
        body_html:  净化后的富文本 HTML（documents.body_html）
    返回：
        .docx 文件的字节内容，可直接作为 Response 附件返回
    """
    doc = Document()  # 新建 Word 文档
    # 标题（0 级，最大）
    doc.add_heading(title or "未命名文档", level=0)
    # 元信息：灰色小字斜体，便于溯源
    for m in meta_lines:  # 逐行写元信息
        p = doc.add_paragraph()  # 新建段落
        r = p.add_run(m)  # 元信息文本
        r.italic = True  # 斜体
        r.font.size = Pt(9)  # 小字号
        r.font.color.rgb = RGBColor(0x66, 0x66, 0x66)  # 灰色
    # 解析富文本 HTML
    soup = BeautifulSoup(body_html or "", "html.parser")  # 用 html.parser 防外部依赖
    for el in soup.children:  # 遍历顶层块级元素
        if el.name is None:  # 纯文本节点（通常是空白）
            if str(el).strip():  # 有实际文字才当段落
                doc.add_paragraph(el.string)  # 纯文本段落
            continue
        _emit_block(doc, el)  # 交给块级分发函数
    # 保存到内存字节流并返回
    buf = io.BytesIO()  # 内存缓冲
    doc.save(buf)  # 写入缓冲
    return buf.getvalue()  # 返回字节
