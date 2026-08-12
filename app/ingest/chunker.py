# -*- coding: utf-8 -*-
"""
知识切片器
==========
职责：把解析出的结构化块，切成适合检索的知识片段。

切片是 RAG 系统里最容易被低估、但对效果影响最大的一环。
切得太大 → 一个片段混杂多个主题，检索命中后噪音多，AI 抓不住重点
切得太小 → 语义不完整，"该产品适用于上述场景"这种句子脱离上下文毫无意义

本模块的策略（SRS FR-3.3）：
1. 以标题为天然边界，不跨章节切分
2. 同一章节内按目标字数聚合段落，在句子边界断开
3. 相邻切片保留重叠区，防止关键信息正好被切断
4. 每个切片携带完整的章节路径，让片段自解释
"""

import re  # 正则表达式
from dataclasses import dataclass, field  # 数据类，用于定义结构化数据
from typing import Any  # 类型注解

from .. import config  # 项目配置


@dataclass
class Chunk:
    """
    知识切片数据类。
    用 dataclass 而非字典，是为了让字段명确、IDE 有提示、拼写错误能在开发期发现。
    """
    seq: int  # 在文档中的顺序号，从 0 开始
    heading_path: str  # 章节路径，如"第二章 采购规则 > 2.1 报价规则"
    content: str  # 切片正文
    char_count: int = 0  # 字数
    meta: dict[str, Any] = field(default_factory=dict)  # 附加元信息（如页码），default_factory 避免可变默认值陷阱
    anchor: str = ""  # 段落锚点：基于章节路径生成的稳定 id，支持 #anchor 直达（P1 段落锚点）

    def searchable_text(self) -> str:
        """
        返回用于建立索引的完整文本。
        把章节路径也拼进去，这样搜"报价规则"时，即使正文没出现这四个字，
        只要它在章节标题里，也能被检索到——这对政策文档特别有用。
        """
        if self.heading_path:  # 有章节路径
            return f"{self.heading_path}\n{self.content}"  # 路径 + 正文
        return self.content  # 无路径则只用正文


def _split_sentences(text: str) -> list[str]:
    """
    把文本按句子切分。

    中文的句子边界是。！？；，英文是 .!?
    在句子边界断开而非硬切字数，能最大限度保持语义完整。
    """
    # 正则说明：在中文句末标点或英文句末标点+空白之后插入分割点
    # (?<=...) 是"后行断言"，表示"匹配位置的前面是这些字符"，但不消耗这些字符
    parts = re.split(r"(?<=[。！？；!?;])\s*|(?<=\.)\s+", text)
    # 过滤掉切分产生的空串
    return [p.strip() for p in parts if p and p.strip()]


def _build_heading_path(stack: list[tuple[int, str]]) -> str:
    """
    根据当前的标题栈，构造章节路径字符串。

    参数：
        stack: [(层级, 标题文本), ...] 形式的栈
    返回：
        "第二章 采购规则 > 2.1 报价规则" 这样的路径
    """
    return " > ".join(title for _, title in stack)  # 用 > 连接各级标题


def make_anchor(heading_path: str, seq: int) -> str:
    """
    根据章节路径生成稳定的 URL 锚点 id（P1 段落锚点）。

    设计要点：
    - 取章节路径的「最后一级标题」做 slug，可读性最好（如"2.1 报价规则"→"21-报价规则"）
    - 非字母数字（含中文）统一替换为短横线，保证能用在 URL 的 #fragment 里
    - 末尾追加切片序号 seq，保证同一文档内即使标题重复也能得到唯一锚点
    - 锚点只依赖标题文本与序号，正文微调不会改变锚点，因此可长期稳定引用
    """
    leaf = heading_path.split(" > ")[-1].strip() if heading_path else ""  # 取最后一级标题
    if not leaf:  # 没有标题（纯正文切片）兜底
        leaf = "section"
    # Unicode 模式下 \w 已包含中文，这里把非「单词字符」的字符统一替换成短横线
    slug = re.sub(r"[^\w]+", "-", leaf).strip("-").lower()  # 清洗成 slug
    if not slug:  # 极端情况（标题全是符号）兜底
        slug = "section"
    return f"{slug}-{seq}"  # 拼接序号，保证文档内唯一


def chunk_blocks(blocks: list[dict], doc_title: str = "") -> list[Chunk]:
    """
    把结构化块列表切成知识片段。

    参数：
        blocks:    parser 输出的结构化块列表
        doc_title: 文档标题，作为章节路径的根节点
    返回：
        Chunk 对象列表
    """
    target_size = config.CHUNK_SIZE  # 目标切片字数
    overlap = config.CHUNK_OVERLAP  # 相邻切片的重叠字数

    chunks: list[Chunk] = []  # 最终结果
    heading_stack: list[tuple[int, str]] = []  # 标题栈，维护当前所处的章节层级
    buffer: list[str] = []  # 正文缓冲区，累积待切片的文本
    buffer_len = 0  # 缓冲区当前字数
    seq = 0  # 切片序号计数器

    def current_path() -> str:
        """构造当前位置的完整章节路径（含文档标题作为根）。"""
        path = _build_heading_path(heading_stack)  # 先取标题栈的路径
        if doc_title and path:  # 既有文档标题又有章节路径
            return f"{doc_title} > {path}"  # 拼成完整路径
        if doc_title:  # 只有文档标题（还没遇到任何标题）
            return doc_title
        return path  # 只有章节路径

    def flush(force: bool = False) -> None:
        """
        把缓冲区中的内容打包成切片。

        参数：
            force: True 表示强制打包（如遇到新标题时），即使内容很少
        """
        nonlocal buffer, buffer_len, seq  # 声明要修改外层函数的变量
        if not buffer:  # 缓冲区为空无需处理
            return
        text = "\n".join(buffer).strip()  # 合并缓冲区内容
        if not text:  # 合并后为空
            buffer, buffer_len = [], 0  # 重置缓冲区
            return
        # 内容太短且不是强制打包时，先不切，继续累积
        if len(text) < 50 and not force:
            return
        # 创建切片对象
        chunks.append(Chunk(
            seq=seq,  # 当前序号
            heading_path=current_path(),  # 章节路径
            content=text,  # 正文
            char_count=len(text),  # 字数
            anchor=make_anchor(current_path(), seq),  # 段落锚点：基于章节路径生成稳定 id
        ))
        seq += 1  # 序号递增

        # 处理重叠：把当前切片的尾部一小段留到下一个切片的开头
        # 这样即使关键信息正好在切片边界上，也能在某一片中保持完整
        if overlap > 0 and len(text) > overlap:
            tail = text[-overlap:]  # 取尾部 overlap 个字符
            buffer = [tail]  # 作为下一个切片的开头
            buffer_len = len(tail)  # 更新字数
        else:  # 不需要重叠或内容太短
            buffer, buffer_len = [], 0  # 直接清空

    for block in blocks:  # 遍历所有结构化块
        btype = block.get("type", "para")  # 块类型，默认为正文
        text = (block.get("text") or "").strip()  # 块文本
        if not text:  # 空块跳过
            continue

        if btype == "heading":  # ---- 标题块 ----
            flush(force=True)  # 遇到新标题，先把之前的内容强制打包（不跨章节切片）
            buffer, buffer_len = [], 0  # 标题处不保留重叠，因为跨章节的重叠没有意义
            level = block.get("level", 1)  # 标题层级
            # 弹出栈中所有层级 >= 当前标题的项。
            # 例如当前是 2 级标题，那么栈里的 2 级、3 级标题都要弹出，只保留 1 级
            while heading_stack and heading_stack[-1][0] >= level:
                heading_stack.pop()
            heading_stack.append((level, text))  # 把当前标题压栈
            continue  # 标题本身不作为切片内容（它已经在 heading_path 里了）

        if btype == "table":  # ---- 表格块 ----
            flush(force=True)  # 表格独立成片，不与正文混在一起
            # 表格可能很大，需要按行拆分成多个切片
            lines = text.split("\n")  # 按行拆
            header = lines[0] if lines else ""  # 第一行作为表头
            current: list[str] = []  # 当前累积的行
            current_len = 0  # 当前字数
            for line in lines:  # 遍历每一行
                # 如果加上这一行会超出目标大小，且已有内容，就先打包
                if current_len + len(line) > target_size and current:
                    chunks.append(Chunk(
                        seq=seq,
                        heading_path=current_path(),
                        # 如果不是第一批，在开头补上表头，保证每片表格都能看懂列含义
                        content=("\n".join(current) if current[0] == header else header + "\n" + "\n".join(current)),
                        char_count=current_len,
                        anchor=make_anchor(current_path(), seq),  # 表格切片也带锚点
                        meta={"type": "table"},  # 标记为表格类型
                    ))
                    seq += 1
                    current, current_len = [], 0  # 重置
                current.append(line)  # 累积当前行
                current_len += len(line)  # 累加字数
            if current:  # 处理剩余的行
                chunks.append(Chunk(
                    seq=seq,
                    heading_path=current_path(),
                    content=("\n".join(current) if current[0] == header else header + "\n" + "\n".join(current)),
                    char_count=current_len,
                    anchor=make_anchor(current_path(), seq),  # 表格切片也带锚点
                    meta={"type": "table"},
                ))
                seq += 1
            continue

        # ---- 正文块 ----
        # 如果单个段落本身就超过目标大小，需要按句子进一步拆分
        if len(text) > target_size:
            flush(force=True)  # 先把缓冲区已有内容打包
            sentences = _split_sentences(text)  # 按句子切分
            for sentence in sentences:  # 逐句累积
                # 加上这句会超出目标大小，且缓冲区已有内容 → 先打包
                if buffer_len + len(sentence) > target_size and buffer_len > 0:
                    flush(force=True)
                buffer.append(sentence)  # 累积这一句
                buffer_len += len(sentence)  # 累加字数
            continue

        # 普通长度的段落：累积到缓冲区
        if buffer_len + len(text) > target_size and buffer_len > 0:  # 会超出目标大小
            flush(force=True)  # 先打包
        buffer.append(text)  # 累积段落
        buffer_len += len(text)  # 累加字数

    flush(force=True)  # 循环结束后打包剩余内容

    # 过滤掉过短的无意义切片（如只剩重叠区的残片）
    return [c for c in chunks if len(c.content.strip()) >= 20]


def make_summary(plain_text: str, max_len: int = 200) -> str:
    """
    从全文生成简单摘要（取开头若干字）。

    这是不调用大模型的零成本方案，用于批量入库时快速填充摘要字段。
    如果需要高质量摘要，可以在入库后异步调用 LLM 重新生成。
    """
    # 把连续的空白字符（含换行）压缩成单个空格，让摘要读起来连贯
    text = re.sub(r"\s+", " ", plain_text).strip()
    if len(text) <= max_len:  # 全文本身就很短
        return text
    truncated = text[:max_len]  # 截取前 max_len 个字符
    # 尝试在最后一个句号处截断，让摘要以完整句子结尾，更好读
    last_period = max(truncated.rfind("。"), truncated.rfind("！"), truncated.rfind("？"))
    if last_period > max_len * 0.5:  # 句号位置不能太靠前，否则摘要太短
        return truncated[:last_period + 1]  # 包含句号本身
    return truncated + "…"  # 找不到合适的句号就直接截断加省略号
