# -*- coding: utf-8 -*-
"""
HTML 工具模块：富文本（Quill 产出）的「净化」与「转纯文本」。

为什么需要它：
    富文本编辑器让用户用按钮排版、粘贴截图，产出的是 HTML 字符串。
    这份 HTML 会被存进数据库、再用 `|safe` 渲染到页面，因此存在两类风险：
      1. XSS 注入：用户可能塞入 <script>、onerror= 之类的恶意内容；
      2. 检索污染：HTML 标签混进正文，会让 BM25 / 向量检索召回噪声。
    本模块提供两个零依赖（只用标准库 html.parser）的函数来化解：
      - sanitize_html()：白名单净化，去掉危险标签与属性，仅保留安全的排版标签；
      - html_to_text()：把 HTML 剥成纯文本，供文档切片、搜索、版本 diff 复用。

设计原则：
    - 白名单而非黑名单：只允许明确安全的标签/属性，其余一律丢弃（更安全）；
    - 不引入第三方库：内网离线部署也能用；
    - 图片只放行 http(s) 与本站 /uploads、/static 路径，杜绝 javascript:/data: 等伪协议。
"""


from html.parser import HTMLParser  # 标准库 HTML 解析器，零依赖


# 允许保留的标签白名单（排版相关，无脚本执行能力）
_ALLOWED_TAGS = {
    "p", "br", "div", "span", "section", "article",
    "strong", "b", "em", "i", "u", "s", "strike", "sub", "sup", "mark",
    "a", "ul", "ol", "li", "blockquote", "pre", "code",
    "h1", "h2", "h3", "h4", "h5", "h6", "hr",
    "img", "table", "thead", "tbody", "tr", "td", "th",
}

# 各标签允许的属性白名单（None 表示该标签不允许任何属性）
_ALLOWED_ATTRS = {
    "a": {"href", "title", "target", "rel"},
    "img": {"src", "alt", "title", "width", "height"},
    "td": {"colspan", "rowspan"},
    "th": {"colspan", "rowspan"},
    "code": {"class"},
    "pre": {"class"},
    "span": {"class"},
    "div": {"class"},
}

# 直接丢弃（连同其内部文本）的标签：脚本/样式/框架等危险或不可见内容
_DROP_TAGS = {"script", "style", "head", "iframe", "object", "embed", "link", "meta", "noscript", "base"}

# 块级标签：转纯文本时在前后补换行，保证段落结构
_BLOCK_TAGS = {
    "p", "div", "section", "article", "blockquote", "pre", "li",
    "h1", "h2", "h3", "h4", "h5", "h6", "tr", "table", "ul", "ol", "hr",
}


def _safe_url(value: str) -> bool:
    """
    判断一个 URL 是否安全可放行。

    只接受：
        - http:// 与 https:// 绝对地址；
        - 以 /uploads/ 或 /static/ 开头的站内相对地址（图片上传产物）；
    拒绝：
        - javascript:、data:、vbscript: 等可执行/伪协议（XSS 高危）；
        - 其它不合规写法。
    """
    v = (value or "").strip().lower()
    if v.startswith(("http://", "https://")):  # 普通外链
        return True
    if v.startswith(("/uploads/", "/static/")):  # 站内上传/静态资源
        return True
    return False  # 其余一律拒绝


def sanitize_html(html: str) -> str:
    """
    白名单净化富文本 HTML，去掉一切危险内容，返回可安全 `|safe` 渲染的字符串。

    参数：
        html: 原始 HTML（来自 Quill 或用户粘贴）
    返回：
        净化后的 HTML 字符串；输入为空或非字符串时返回空串
    """
    if not html or not isinstance(html, str):  # 空值直接返回空串
        return ""
    out = []          # 输出片段列表
    drop_depth = 0    # 当前处于「丢弃标签」内部的层数（处理嵌套）

    class _Sanitizer(HTMLParser):
        """内部解析器：边解析边构造净化后的 HTML。"""

        def handle_starttag(self, tag, attrs):
            nonlocal drop_depth
            if drop_depth > 0:  # 正在丢弃区，忽略内部标签（仅计数匹配层）
                if tag in _DROP_TAGS:
                    drop_depth += 1
                return
            if tag in _DROP_TAGS:  # 进入丢弃标签，开始忽略其内容
                drop_depth += 1
                return
            if tag not in _ALLOWED_TAGS:  # 不在白名单，直接丢弃该标签（保留文本）
                return
            # 过滤属性：只保留白名单内、且 URL 安全的属性
            allowed = _ALLOWED_ATTRS.get(tag, set())
            kept = []
            for k, v in attrs:
                if k not in allowed:  # 属性不在白名单
                    continue
                if k in ("href", "src") and not _safe_url(v or ""):  # 危险 URL
                    continue
                kept.append(f' {k}="{_escape_attr(v or "")}"')  # 保留并转义属性值
            out.append(f"<{tag}{''.join(kept)}>")  # 输出开始标签

        def handle_endtag(self, tag):
            nonlocal drop_depth
            if drop_depth > 0:  # 处于丢弃区
                if tag in _DROP_TAGS:
                    drop_depth = max(0, drop_depth - 1)  # 离开一层丢弃
                return
            if tag not in _ALLOWED_TAGS:  # 非白名单标签的结束，忽略
                return
            out.append(f"</{tag}>")  # 输出结束标签

        def handle_startendtag(self, tag, attrs):
            """自闭合标签（如 <br/> <img/>）的处理。"""
            self.handle_starttag(tag, attrs)  # 当作开始标签处理（img 等无需结束）

        def handle_data(self, data):
            if drop_depth > 0:  # 丢弃区内不输出文本
                return
            out.append(_escape_text(data))  # 转义文本内容，防 < > & 破坏结构

        def handle_entityref(self, name):
            if drop_depth == 0:
                out.append(f"&{name};")  # 保留实体引用（如 &amp;）

        def handle_charref(self, name):
            if drop_depth == 0:
                out.append(f"&#{name};")  # 保留字符引用（如 &#160;）

    p = _Sanitizer(convert_charrefs=False)  # 不自动合并实体，便于逐个处理
    p.feed(html)  # 解析
    p.close()
    return "".join(out)


def html_to_text(html: str) -> str:
    """
    把富文本 HTML 转为纯文本，供切片、搜索、版本 diff 复用。

    规则：
        - 块级标签前后补换行，保留段落结构；
        - 图片转成 [图片] 占位（或 alt 文本），避免丢失"有图"信号；
        - 去掉所有标签与样式，结果即为安全、干净的检索正文。

    参数：
        html: 原始或已净化的 HTML
    返回：
        纯文本；输入为空返回空串
    """
    if not html or not isinstance(html, str):
        return ""
    out = []  # 文本片段
    drop_depth = 0  # 丢弃区层数

    class _TextExtractor(HTMLParser):
        """内部解析器：抽取可见文本。"""

        def handle_starttag(self, tag, attrs):
            nonlocal drop_depth
            if tag in _DROP_TAGS:
                drop_depth += 1
                return
            if tag in _BLOCK_TAGS:  # 块级标签前补换行
                out.append("\n")

        def handle_endtag(self, tag):
            nonlocal drop_depth
            if tag in _DROP_TAGS and drop_depth > 0:
                drop_depth -= 1
                return
            if tag in _BLOCK_TAGS:  # 块级标签后补换行
                out.append("\n")

        def handle_startendtag(self, tag, attrs):
            if tag == "br":  # 换行符
                out.append("\n")
            elif tag == "img":  # 图片占位
                alt = dict(attrs).get("alt", "")
                out.append(f"[图片{('：' + alt) if alt else ''}]")

        def handle_data(self, data):
            if drop_depth == 0:
                out.append(data)

    p = _TextExtractor(convert_charrefs=True)
    p.feed(html)
    p.close()
    # 合并多余空行（3 个及以上换行压成 2 个），让纯文本整洁
    text = "".join(out)
    lines = [ln.rstrip() for ln in text.split("\n")]
    cleaned = "\n".join(lines)
    while "\n\n\n" in cleaned:  # 反复压缩连续空行
        cleaned = cleaned.replace("\n\n\n", "\n\n")
    return cleaned.strip()


def _escape_text(s: str) -> str:
    """转义文本内容中的特殊字符，防止破坏 HTML 结构。"""
    return (s or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _escape_attr(s: str) -> str:
    """转义属性值中的特殊字符（含引号，避免属性注入）。"""
    return (s or "").replace("&", "&amp;").replace('"', "&quot;").replace("<", "&lt;").replace(">", "&gt;")
