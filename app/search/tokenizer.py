# -*- coding: utf-8 -*-
"""
中文分词模块
============
职责：把中文文本切成词，供 SQLite FTS5 建立全文索引。

为什么必须自己分词：
SQLite 的 FTS5 内置分词器（unicode61）是按"空白字符"切词的。
对英文没问题（"medical device policy" 天然带空格），
但对中文来说，"医用耗材集中带量采购"会被当成一个完整的词，
用户搜"带量采购"根本匹配不上——因为它不是那个"词"的前缀。

解决方案：入库时用 jieba 把中文切成词并用空格连接，
存成 "医用 耗材 集中 带量 采购" 这样的形式，FTS5 就能正常工作了。
检索时对查询词做同样处理，两边对齐。

降级设计：
jieba 未安装时自动降级为"单字 + 二元组"切分。
效果不如 jieba，但保证系统在任何环境下都能跑起来（NFR-1 优雅降级）。
"""

import re  # 正则表达式
from typing import Optional  # 类型注解

# _jieba 保存 jieba 模块的引用，None 表示不可用
_jieba = None
# _jieba_checked 标记是否已经尝试过导入，避免每次调用都重复尝试导入失败的模块
_jieba_checked = False

# STOPWORDS 是停用词表：这些词出现频率极高但几乎不携带检索价值。
# 过滤掉它们可以显著提升 BM25 的排序质量——否则搜"集采的通知"会被"的"字干扰。
STOPWORDS = {
    # 中文虚词
    "的", "了", "和", "是", "在", "有", "与", "及", "或", "等", "为", "对", "于", "被",
    "把", "从", "到", "由", "以", "其", "此", "该", "这", "那", "之", "也", "就", "都",
    "而", "并", "但", "却", "不", "无", "非", "所", "者", "个", "们", "着", "过", "地",
    "上", "下", "中", "内", "外", "前", "后", "时", "年", "月", "日",
    # 英文虚词
    "the", "a", "an", "and", "or", "of", "to", "in", "on", "at", "for", "with",
    "is", "are", "was", "were", "be", "been", "by", "as", "it", "this", "that",
}


def _ensure_jieba():
    """
    延迟加载 jieba。

    为什么延迟：jieba 首次导入需要加载几十兆的词典，耗时约 1 秒。
    如果在模块顶部导入，会拖慢整个应用的启动速度。
    改为首次真正使用时才加载，启动更快。
    """
    global _jieba, _jieba_checked  # 声明修改全局变量
    if _jieba_checked:  # 已经尝试过了，直接返回结果（无论成功失败）
        return _jieba
    _jieba_checked = True  # 标记为已尝试
    try:
        import jieba  # 尝试导入
        jieba.setLogLevel(60)  # 把日志级别调到最高，屏蔽 "Building prefix dict..." 的启动噪音
        _jieba = jieba  # 保存引用
    except ImportError:  # 没安装
        _jieba = None  # 标记不可用，后续走降级方案
    return _jieba


def _fallback_tokenize(text: str) -> list[str]:
    """
    降级分词方案（jieba 不可用时使用）。

    策略：中文按"单字 + 相邻二字组合"切分，英文数字整体保留。
    例如"带量采购" → ["带", "量", "采", "购", "带量", "量采", "采购"]
    这样无论用户搜"带量"还是"采购"都能命中，代价是索引体积变大约 2 倍。
    """
    tokens: list[str] = []  # 结果列表
    # 把文本切成"连续中文块"和"连续英文数字块"
    for segment in re.findall(r"[\u4e00-\u9fff]+|[a-zA-Z0-9_]+", text):
        if re.match(r"^[\u4e00-\u9fff]+$", segment):  # 中文块
            tokens.extend(list(segment))  # 每个单字作为一个 token
            # 再加上所有相邻的二字组合
            for i in range(len(segment) - 1):
                tokens.append(segment[i:i + 2])
        else:  # 英文或数字块
            tokens.append(segment.lower())  # 转小写后整体保留，保证大小写不敏感
    return tokens


def tokenize(text: str, for_query: bool = False) -> list[str]:
    """
    对文本分词。

    参数：
        text:      待分词文本
        for_query: 是否用于查询。查询时使用搜索引擎模式，会切出更多细粒度的词，
                   提高召回率（宁可多召回再排序，也不要漏掉）
    返回：
        词列表
    """
    if not text:  # 空文本返回空列表
        return []

    jieba = _ensure_jieba()  # 获取 jieba 模块

    if jieba is None:  # jieba 不可用，走降级方案
        raw_tokens = _fallback_tokenize(text)
    elif for_query:  # 查询模式：用搜索引擎模式，对长词做二次切分提高召回
        raw_tokens = list(jieba.cut_for_search(text))
    else:  # 索引模式：用精确模式，切分结果更准确、索引更紧凑
        raw_tokens = list(jieba.cut(text))

    result: list[str] = []  # 清洗后的结果
    for tok in raw_tokens:  # 遍历每个词
        t = tok.strip().lower()  # 去空白并转小写（统一大小写便于匹配）
        if not t:  # 空词跳过
            continue
        # 跳过纯标点符号和特殊字符（不含任何中文、字母、数字的词）
        if not re.search(r"[\u4e00-\u9fffa-z0-9]", t):
            continue
        if t in STOPWORDS:  # 跳过停用词
            continue
        if len(t) == 1 and not re.match(r"[\u4e00-\u9fff0-9]", t):  # 跳过单个英文字母（如 a、b）
            continue
        result.append(t)  # 保留这个词

    return result


def tokenize_to_string(text: str, for_query: bool = False) -> str:
    """
    分词后用空格连接成字符串，直接存入 FTS5 索引字段。
    这是 tokenize 最常用的形式。
    """
    return " ".join(tokenize(text, for_query=for_query))  # 用空格连接所有词


def build_fts_query(query_text: str) -> str:
    """
    把用户的自然语言查询转成 FTS5 的 MATCH 查询表达式。

    关键处理：
    1. 分词后用 OR 连接。用 OR 而非 AND 是因为知识检索场景下
       "宁可多召回再排序"——用 AND 的话，用户多打一个词就可能零结果。
    2. 每个词用双引号包裹，防止词中的特殊字符（如 -、*）被 FTS5 当作语法符号解析报错。

    返回：
        FTS5 查询字符串，如 '"集采" OR "政策" OR "湖北"'
        无有效词时返回空字符串，调用方应据此跳过 BM25 检索
    """
    tokens = tokenize(query_text, for_query=True)  # 对查询分词
    if not tokens:  # 没切出有效词
        return ""
    # 去重但保持原有顺序（dict.fromkeys 在 Python 3.7+ 保证插入顺序）
    unique_tokens = list(dict.fromkeys(tokens))
    # 限制词数上限，防止用户粘贴一整段文字导致查询表达式过长、FTS5 性能骤降
    unique_tokens = unique_tokens[:32]
    # 每个词转义内部双引号后用双引号包裹，再用 OR 连接
    escaped = [f'"{t.replace(chr(34), "")}"' for t in unique_tokens]
    return " OR ".join(escaped)


def highlight_terms(text: str, query: str, max_len: int = 300) -> str:
    """
    生成检索结果的高亮摘要片段（SRS FR-4.5）。

    做两件事：
    1. 定位查询词在正文中第一次出现的位置，截取其周围的一段文字作为预览
       （而不是永远显示开头，那样用户看不出为什么这条会被搜到）
    2. 用 <mark> 标签包裹命中的词

    返回：
        含 <mark> 标签的 HTML 片段
    """
    if not text:  # 空文本
        return ""
    tokens = [t for t in tokenize(query, for_query=True) if len(t) >= 2]  # 只高亮 2 字以上的词，单字噪音太大
    if not tokens:  # 没有可高亮的词
        return text[:max_len] + ("…" if len(text) > max_len else "")  # 直接返回开头

    # 找出第一个命中词的位置
    lower_text = text.lower()  # 转小写用于不区分大小写的查找
    first_pos = -1  # 初始化为未找到
    for t in tokens:  # 遍历每个查询词
        pos = lower_text.find(t)  # 查找位置
        if pos >= 0 and (first_pos < 0 or pos < first_pos):  # 找到了且比之前记录的更靠前
            first_pos = pos  # 更新位置

    if first_pos < 0:  # 所有词都没在正文中出现（可能是语义检索命中的）
        snippet = text[:max_len]  # 就取开头
        prefix = ""  # 不需要省略号前缀
    else:
        # 以命中位置为中心，前后各取一段
        start = max(0, first_pos - max_len // 3)  # 往前取 1/3 长度作为上文
        snippet = text[start:start + max_len]  # 截取片段
        prefix = "…" if start > 0 else ""  # 不是从头开始就加省略号

    suffix = "…" if (first_pos < 0 and len(text) > max_len) or (first_pos >= 0 and len(text) > len(snippet)) else ""

    # 先做 HTML 转义，防止文档正文中的 < > 被浏览器当成标签解析（XSS 防护，NFR-7）
    snippet = snippet.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

    # 逐个词做高亮替换。按长度从长到短排序，避免短词先替换导致长词被拆散
    for t in sorted(set(tokens), key=len, reverse=True):
        # re.escape 转义正则特殊字符；re.IGNORECASE 不区分大小写
        pattern = re.compile(re.escape(t), re.IGNORECASE)
        snippet = pattern.sub(lambda m: f"<mark>{m.group(0)}</mark>", snippet)

    return prefix + snippet + suffix  # 拼上前后省略号返回
