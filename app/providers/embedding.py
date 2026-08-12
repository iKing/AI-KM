# -*- coding: utf-8 -*-
"""
向量（Embedding）模型适配器
===========================
职责：把文本转换成向量，供语义检索使用。

支持的服务商：
- siliconflow : 硅基流动（默认，bge-m3 中文效果好、有免费额度）
- openai      : 任何 OpenAI 兼容的 embeddings 接口
- ollama      : 本地私有化部署（SG2 10 月底私有化目标）
- hash        : 本地哈希降级方案（无外网/无密钥时保证系统可跑，效果有限但不阻塞流程）

关键设计：
"hash" 降级模式是 NFR-1"优雅降级"的具体落地。
没有它，一旦外网不通整个系统就瘫痪；有了它，至少关键词检索和基础语义检索还能工作。
"""

import hashlib  # 哈希算法库，用于本地降级向量方案
import math  # 数学函数库，用于向量归一化计算
import re  # 正则表达式库，用于降级方案的文本切分
from typing import Optional  # 类型注解

import requests  # HTTP 客户端

from .. import config  # 项目配置


class EmbeddingError(Exception):
    """向量服务调用异常，供上层捕获后走降级逻辑。"""
    pass


# _cache 是进程内的向量缓存，键为"模型名+文本哈希"，值为向量。
# 作用：同一段文本重复向量化时直接返回缓存，既省钱又提速（NFR-5 成本可控）。
_cache: dict[str, list[float]] = {}

# _CACHE_MAX 限制缓存条目上限，防止长期运行时内存无限增长
_CACHE_MAX = 20000


def _cache_key(text: str) -> str:
    """
    生成缓存键。
    把模型名也纳入键中，这样切换模型后不会误用旧模型的向量（避免索引污染）。
    """
    # 用 MD5 对"模型名 + 文本"求哈希，得到定长的键，避免超长文本当键占内存
    raw = f"{config.EMBED_MODEL}::{text}".encode("utf-8")  # 拼接后转字节
    return hashlib.md5(raw).hexdigest()  # 返回 32 位十六进制哈希字符串


def _hash_embedding(text: str, dim: int) -> list[float]:
    """
    本地哈希向量降级方案（无需外网、无需密钥）。

    原理：
    把文本切成词，每个词用哈希函数映射到向量的某几个维度上累加。
    这本质上是"随机投影版的词袋模型"，能捕捉词汇重合度，
    但捕捉不到真正的语义相似（"集采"和"带量采购"仍算不同）。

    定位：这是保命方案，不是生产方案。生产环境必须配置真实向量服务。
    """
    vec = [0.0] * dim  # 初始化一个全零向量
    # 用正则把文本切成"连续的中文字符块"或"连续的英文数字块"
    tokens = re.findall(r"[\u4e00-\u9fff]+|[a-zA-Z0-9]+", text.lower())
    grams: list[str] = []  # 存放最终参与哈希的语义单元
    for tok in tokens:  # 遍历每个切出来的块
        if re.match(r"^[\u4e00-\u9fff]+$", tok):  # 如果是中文块
            if len(tok) == 1:  # 单字直接作为一个单元
                grams.append(tok)
            else:
                # 中文用 2-gram 切分（"带量采购" → "带量","量采","采购"），近似词的效果
                for i in range(len(tok) - 1):
                    grams.append(tok[i:i + 2])
        else:  # 英文或数字块直接整体作为一个单元
            grams.append(tok)
    if not grams:  # 空文本返回零向量，避免后续除零错误
        return vec
    for g in grams:  # 遍历每个语义单元
        h = hashlib.md5(g.encode("utf-8")).digest()  # 求 MD5 得到 16 字节
        # 用哈希的不同片段决定要影响哪几个维度，模拟随机投影
        for k in range(4):  # 每个单元影响 4 个维度，增加区分度
            # 从哈希字节中取 4 个字节转成整数，再对维度取模得到目标下标
            idx = int.from_bytes(h[k * 4:(k + 1) * 4], "big") % dim
            # 用哈希的奇偶性决定正负号，让不同词的贡献可以相互抵消（提升区分度）
            sign = 1.0 if h[k] % 2 == 0 else -1.0
            vec[idx] += sign  # 累加到对应维度
    # L2 归一化：把向量长度缩放到 1，这样余弦相似度可以直接用点积计算
    norm = math.sqrt(sum(v * v for v in vec))  # 计算向量的欧几里得长度
    if norm > 0:  # 长度大于 0 才能归一化，否则会除零
        vec = [v / norm for v in vec]  # 每个分量除以长度
    return vec


def _call_remote_embedding(texts: list[str]) -> list[list[float]]:
    """
    调用远程向量服务的底层函数。
    siliconflow / openai / ollama 三家的 embeddings 接口格式一致，可统一处理。
    """
    base = config.EMBED_BASE_URL.rstrip("/")  # 去掉结尾斜杠
    url = f"{base}/embeddings"  # 拼接标准 embeddings 接口路径
    headers = {"Content-Type": "application/json"}  # JSON 请求头
    # Ollama 本地服务不需要密钥
    if config.EMBED_PROVIDER != "ollama" and config.EMBED_API_KEY:
        headers["Authorization"] = f"Bearer {config.EMBED_API_KEY}"  # Bearer 鉴权
    payload = {
        "model": config.EMBED_MODEL,  # 向量模型名
        "input": texts,  # 待向量化的文本列表（支持批量，效率更高）
    }
    try:
        # 发起请求
        resp = requests.post(url, headers=headers, json=payload, timeout=config.EMBED_TIMEOUT)
    except requests.RequestException as exc:  # 网络异常
        raise EmbeddingError(f"向量服务连接失败：{exc}") from exc

    if resp.status_code != 200:  # HTTP 错误
        raise EmbeddingError(f"向量服务返回错误 HTTP {resp.status_code}：{resp.text[:300]}")

    try:
        data = resp.json()  # 解析响应
        # 响应格式为 {"data": [{"embedding": [...], "index": 0}, ...]}
        items = data["data"]
        # 按 index 排序，确保返回顺序与输入顺序严格一致（有些服务商不保证顺序）
        items = sorted(items, key=lambda x: x.get("index", 0))
        return [item["embedding"] for item in items]  # 提取出向量列表
    except (ValueError, KeyError, TypeError) as exc:  # 响应结构异常
        raise EmbeddingError(f"向量服务响应格式异常：{resp.text[:300]}") from exc


def embed_texts(texts: list[str], use_cache: bool = True) -> list[list[float]]:
    """
    批量把文本转成向量。入库流水线的核心依赖。

    参数：
        texts:     文本列表
        use_cache: 是否使用进程内缓存（重复内容不重复计费）
    返回：
        与输入等长的向量列表，顺序一一对应
    """
    if not texts:  # 空输入直接返回空列表
        return []

    dim = config.EMBED_DIM  # 目标向量维度

    # ---- 分支一：本地哈希降级模式 ----
    if config.EMBED_PROVIDER == "hash":
        return [_hash_embedding(t, dim) for t in texts]  # 逐条本地计算，无网络调用

    # ---- 分支二：远程向量服务 ----
    results: list[Optional[list[float]]] = [None] * len(texts)  # 预分配结果槽位
    pending_idx: list[int] = []  # 记录哪些位置需要真正调接口
    pending_txt: list[str] = []  # 对应的待处理文本

    for i, t in enumerate(texts):  # 遍历所有输入文本
        if use_cache:  # 启用缓存时先查缓存
            ck = _cache_key(t)  # 计算缓存键
            if ck in _cache:  # 缓存命中
                results[i] = _cache[ck]  # 直接填入结果
                continue  # 跳过，不需要调接口
        pending_idx.append(i)  # 缓存未命中，记录索引
        pending_txt.append(t)  # 记录文本

    # 分批调用接口，避免单次请求体过大导致超时
    batch_size = config.EMBED_BATCH  # 每批条数
    for start in range(0, len(pending_txt), batch_size):  # 按批次步进
        batch = pending_txt[start:start + batch_size]  # 取出当前批次的文本
        vectors = _call_remote_embedding(batch)  # 调用远程服务
        for j, vec in enumerate(vectors):  # 遍历返回的向量
            global_idx = pending_idx[start + j]  # 换算回原始输入的位置
            results[global_idx] = vec  # 填入结果
            if use_cache and len(_cache) < _CACHE_MAX:  # 缓存未满则写入
                _cache[_cache_key(texts[global_idx])] = vec

    # 兜底：万一某个槽位仍为空（理论上不会），用零向量填充，保证返回长度一致不崩溃
    return [r if r is not None else [0.0] * dim for r in results]


def embed_query(text: str) -> list[float]:
    """
    把单条查询文本转成向量。检索时调用。
    单独提供这个函数是因为查询侧有可能需要加特定前缀（某些模型区分 query/passage）。
    """
    vectors = embed_texts([text])  # 复用批量函数
    return vectors[0] if vectors else [0.0] * config.EMBED_DIM  # 取第一条，异常时返回零向量


def embedding_health() -> dict:
    """
    向量服务健康检查，供管理界面调用。

    返回：
        {"ok": 是否正常, "provider": 服务商, "model": 模型, "dim": 实际维度, "message": 说明}
    """
    try:
        vec = embed_query("健康检查测试文本")  # 用一句简单文本测试
        actual_dim = len(vec)  # 实际返回的维度
        # 校验实际维度与配置是否一致。不一致会导致索引全乱，必须明确报警
        if actual_dim != config.EMBED_DIM:
            return {
                "ok": False,
                "provider": config.EMBED_PROVIDER,
                "model": config.EMBED_MODEL,
                "dim": actual_dim,
                "message": (
                    f"维度不匹配！配置为 {config.EMBED_DIM}，实际返回 {actual_dim}。"
                    f"请修改 AIKM_EMBED_DIM 为 {actual_dim} 并重建索引"
                ),
            }
        return {
            "ok": True,
            "provider": config.EMBED_PROVIDER,
            "model": config.EMBED_MODEL,
            "dim": actual_dim,
            "message": f"连通正常，向量维度 {actual_dim}",
        }
    except EmbeddingError as exc:  # 捕获调用异常返回结构化结果
        return {
            "ok": False,
            "provider": config.EMBED_PROVIDER,
            "model": config.EMBED_MODEL,
            "dim": 0,
            "message": str(exc),
        }


def clear_cache() -> int:
    """
    清空向量缓存。切换模型后必须调用，否则会拿到旧模型的向量导致检索错乱。
    返回：被清空的条目数
    """
    count = len(_cache)  # 记录清空前的数量
    _cache.clear()  # 清空字典
    return count  # 返回清空条数，便于日志记录
