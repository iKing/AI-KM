# -*- coding: utf-8 -*-
"""
服务端生成 PDF 文档
===================
把服务端净化后的富文本 HTML（documents.body_html）转成可下载的 .pdf 文件。

设计要点：
- 不依赖浏览器（优于「打印→另存为 PDF」），纯服务端生成，内网离线可用。
- 用 reportlab 的 platypus 流式排版，自动分页、支持中文字体。
- 中文字体：注册内置 CID 字体 STSong-Light（无需任何外部字体文件，开箱即用）。
- 内嵌图片按 /uploads/ 解析本地文件，取不到则跳过。
- 正文本质是「结构化正文」，还原标题/段落/列表/引用/代码/图片/表格层级。

对外唯一入口：html_to_pdf(title, meta_lines, body_html) -> bytes
"""

import io  # 内存字节流
from pathlib import Path  # 路径处理

from bs4 import BeautifulSoup  # 解析富文本 HTML
from reportlab.lib.pagesizes import A4  # A4 纸张
from reportlab.lib.units import cm  # 厘米单位
from reportlab.lib import colors  # 颜色
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle  # 样式
from reportlab.lib.enums import TA_LEFT  # 对齐枚举
from reportlab.platypus import (
    SimpleDocTemplate, Paragraph, Spacer, ListFlowable, ListItem,
    Table, TableStyle, Image, HRFlowable, Preformatted,
)  # 流式排版各类元素
from reportlab.pdfbase import pdfmetrics  # 字体注册
from reportlab.pdfbase.cidfonts import UnicodeCIDFont  # 内置中文字体（CID）

from . import config  # 取上传目录

# 注册一次中文字体（STSong-Light 是 Adobe 标准 CJK 字体，reportlab 内置，无需外部文件）
# 放在模块加载期执行，仅注册一次。若失败（极老版本 reportlab）则降级用默认字体。
try:
    pdfmetrics.registerFont(UnicodeCIDFont("STSong-Light"))  # 注册宋体 CID 字体
    _CJK_FONT = "STSong-Light"  # 注册成功，后续样式都用它
except Exception:
    _CJK_FONT = "Helvetica"  # 注册失败降级（中文可能显示为方块，但不影响导出流程）


def _resolve_image_src(src: str):
    """把 HTML 图片地址解析成本地真实文件路径（仅本系统 /uploads/，防 SSRF）。"""
    if not src:  # 空地址
        return None
    if src.startswith("/uploads/"):  # 本系统内嵌图片
        fname = Path(src[len("/uploads/"):]).name  # 只取文件名防穿越
        p = config.UPLOAD_DIR / fname  # 拼上传目录
        if p.exists() and p.is_file():  # 存在才返回
            return p
    return None  # 外链/不存在返回 None


def _inline_to_rml(node) -> str:
    """
    递归把节点转成 reportlab Paragraph 支持的「行内标记字符串」。

    reportlab 的 Paragraph 接受一段类 XML：<b>/<i>/<u>/<a href>/<br/> 等。
    这里把 b/strong→<b>、i/em→<i>、u→<u>、a→<a href>、code→<font face=Courier>，
    其余未知标签递归其子文本。文本里的 & < > 转义，避免破坏 XML 解析。
    """
    out = []  # 收集片段
    for child in node.children:  # 遍历直接子节点
        if child.name is None:  # 纯文本
            txt = child.string or ""  # 取文本
            # 转义 XML 特殊字符，否则 reportlab 解析报错
            txt = txt.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            out.append(txt)  # 追加
        elif child.name in ("b", "strong"):  # 粗体
            out.append("<b>" + _inline_to_rml(child) + "</b>")
        elif child.name in ("i", "em"):  # 斜体
            out.append("<i>" + _inline_to_rml(child) + "</i>")
        elif child.name == "u":  # 下划线
            out.append("<u>" + _inline_to_rml(child) + "</u>")
        elif child.name == "a":  # 超链接
            href = child.get("href", "") or ""  # 链接地址
            out.append(f'<a href="{href}">' + _inline_to_rml(child) + "</a>")
        elif child.name == "br":  # 换行
            out.append("<br/>")
        elif child.name == "code":  # 行内代码
            out.append("<font face='Courier'>" + _inline_to_rml(child) + "</font>")
        else:  # 其他内联标签递归
            out.append(_inline_to_rml(child))
    return "".join(out)  # 拼成字符串


def _build_styles():
    """构造一套中文样式表：所有样式统一使用已注册的中文字体，避免中文乱码。"""
    base = getSampleStyleSheet()  # 取默认样式表（用于继承基础属性）
    styles = {}  # 自定义样式字典
    # 正文样式
    styles["body"] = ParagraphStyle(
        "CJKBody", parent=base["Normal"], fontName=_CJK_FONT,
        fontSize=11, leading=18, alignment=TA_LEFT,
    )
    # 各级标题
    for lv in range(1, 7):  # h1~h6
        styles[f"h{lv}"] = ParagraphStyle(
            f"CJKH{lv}", parent=base["Heading%d" % lv], fontName=_CJK_FONT,
            fontSize=max(14, 20 - lv * 1.2), leading=max(18, 26 - lv * 1.2),
        )
    # 引用样式（灰色、缩进）
    styles["quote"] = ParagraphStyle(
        "CJKQuote", parent=base["Normal"], fontName=_CJK_FONT,
        fontSize=10, leading=16, leftIndent=16, textColor=colors.HexColor("#555555"),
    )
    # 代码样式（等宽、灰底）
    styles["code"] = ParagraphStyle(
        "CJKCode", parent=base["Code"], fontName="Courier", fontSize=9, leading=13,
        backColor=colors.HexColor("#f4f4f4"), leftIndent=8,
    )
    return styles  # 返回样式表


def _emit_block(flowables: list, el, styles) -> None:
    """
    根据 HTML 块级标签，向 flowables 列表追加对应 reportlab 流式元素。
    """
    tag = el.name  # 标签名
    if tag in ("h1", "h2", "h3", "h4", "h5", "h6"):  # 标题
        flowables.append(Paragraph(_inline_to_rml(el), styles[f"h{tag[1]}"]))
    elif tag == "p":  # 段落
        flowables.append(Paragraph(_inline_to_rml(el), styles["body"]))
    elif tag in ("ul", "ol"):  # 列表
        items = []  # 列表项
        for li in el.find_all("li", recursive=False):  # 直接子项
            items.append(ListItem(Paragraph(_inline_to_rml(li), styles["body"])))  # 每项一个段落
        flowables.append(ListFlowable(
            items, bulletType="bullet" if tag == "ul" else "1", leftIndent=18,
        ))  # 无序/有序列表
    elif tag == "blockquote":  # 引用
        flowables.append(Paragraph(_inline_to_rml(el), styles["quote"]))
    elif tag in ("pre", "code"):  # 代码块
        flowables.append(Preformatted(el.get_text(), styles["code"]))
    elif tag == "img":  # 图片
        path = _resolve_image_src(el.get("src"))  # 解析本地路径
        if path:  # 取得到才插入
            try:
                # 等比缩放到最大宽 14cm，避免溢出页面
                img = Image(str(path), width=14 * cm, height=None, kind="proportional")
                flowables.append(img)  # 加入排版
            except Exception:
                pass  # 格式不支持忽略
    elif tag == "hr":  # 分隔线
        flowables.append(HRFlowable(width="100%", color=colors.grey, thickness=0.6))
    elif tag == "table":  # 表格
        _emit_table(flowables, el)  # 交给表格函数
    else:  # 未知块级：兜底当段落
        flowables.append(Paragraph(_inline_to_rml(el), styles["body"]))


def _emit_table(flowables: list, el) -> None:
    """把 HTML <table> 转成 reportlab Table（含网格线、表头底色）。"""
    rows = el.find_all("tr")  # 所有行
    if not rows:  # 空表跳过
        return
    data = []  # 二维数据
    for r in rows:  # 逐行
        cells = r.find_all(["td", "th"])  # 单元格
        data.append([c.get_text(strip=True) for c in cells])  # 取纯文本
    if not data:  # 仍为空则跳过
        return
    table = Table(data, hAlign="LEFT")  # 左对齐表格
    table.setStyle(TableStyle([  # 表格样式
        ("GRID", (0, 0), (-1, -1), 0.5, colors.grey),  # 全网格线
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#eef2f7")),  # 表头底色
        ("FONTSIZE", (0, 0), (-1, -1), 9),  # 字号
        ("VALIGN", (0, 0), (-1, -1), "TOP"),  # 顶对齐
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#fafafa")]),  # 隔行底色
    ]))
    flowables.append(table)  # 加入排版


def html_to_pdf(title: str, meta_lines: list, body_html: str) -> bytes:
    """
    把标题 + 元信息 + 富文本 HTML 组装成 .pdf 字节流。

    参数：
        title:      文档标题
        meta_lines: 元信息行（分类/密级/责任人/版本等），作为灰色小字置于顶部
        body_html:  净化后的富文本 HTML（documents.body_html）
    返回：
        .pdf 文件字节内容，可直接作为 Response 附件返回
    """
    styles = _build_styles()  # 构造中文样式表
    buf = io.BytesIO()  # 内存缓冲
    # 用 SimpleDocTemplate 生成 A4 文档，指定中文字体（标题/正文都会继承样式里的字体）
    doc = SimpleDocTemplate(
        buf, pagesize=A4, title=title or "未命名文档",
        leftMargin=2 * cm, rightMargin=2 * cm, topMargin=2 * cm, bottomMargin=2 * cm,
    )
    flowables = []  # 流式元素列表
    # 标题
    flowables.append(Paragraph((title or "未命名文档").replace("&", "&amp;").replace("<", "&lt;"), styles["h1"]))
    # 元信息：灰色小字
    for m in meta_lines:  # 逐行
        flowables.append(Paragraph(
            m.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"), styles["quote"]
        ))
    flowables.append(Spacer(1, 0.4 * cm))  # 标题与正文间留白
    # 解析富文本 HTML 并逐块转换为流式元素
    soup = BeautifulSoup(body_html or "", "html.parser")  # html.parser 防外部依赖
    for el in soup.children:  # 遍历顶层块级
        if el.name is None:  # 纯文本节点
            if str(el).strip():  # 有文字才加
                flowables.append(Paragraph(str(el).strip().replace("&", "&amp;"), styles["body"]))
            continue
        _emit_block(flowables, el, styles)  # 块级分发
    doc.build(flowables)  # 排版生成 PDF
    return buf.getvalue()  # 返回字节
