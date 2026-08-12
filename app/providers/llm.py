# -*- coding: utf-8 -*-
"""
大语言模型（LLM）适配器
=======================
统一封装不同厂商的对话接口，对上层提供一致的调用方式。

支持的服务商：
- deepseek : DeepSeek 官方 API（当前默认，公司已有密钥）
- openai   : 任何 OpenAI 兼容接口（硅基流动、智谱、通义等大多兼容）
- ollama   : 本地私有化部署（对应 SG2"10月底私有化部署启动"）

三者的接口格式高度一致（都是 OpenAI Chat Completions 风格），
差异仅在 base_url、鉴权方式和模型名，因此可以用同一套代码处理。
"""

import json  # 用于解析和构造 JSON 数据
from typing import Generator, Optional  # 类型注解：生成器、可选值

import requests  # HTTP 客户端库，用于调用模型 API

from .. import config  # 导入项目配置


class LLMError(Exception):
    """
    大模型调用异常。
    单独定义异常类型，让上层可以精确捕获模型故障并走降级逻辑，
    而不会把模型故障和程序 bug 混为一谈。
    """
    pass


def _build_headers() -> dict:
    """
    构造 HTTP 请求头。
    不同服务商的鉴权方式略有差异，本函数负责屏蔽这些差异。
    """
    headers = {"Content-Type": "application/json"}  # 所有服务商都要求 JSON 格式
    # Ollama 本地部署不需要密钥，其余服务商需要 Bearer Token 鉴权
    if config.LLM_PROVIDER != "ollama" and config.LLM_API_KEY:
        headers["Authorization"] = f"Bearer {config.LLM_API_KEY}"  # 标准 Bearer 鉴权头
    return headers


def _build_url() -> str:
    """
    构造对话接口的完整 URL。
    DeepSeek 与 OpenAI 兼容接口都是 /chat/completions，
    Ollama 从 0.1.x 起也提供了 /v1/chat/completions 兼容端点，因此可以统一处理。
    """
    base = config.LLM_BASE_URL.rstrip("/")  # 去掉配置中可能多写的结尾斜杠，避免出现双斜杠
    return f"{base}/chat/completions"  # 拼接标准的对话接口路径


def chat_completion(
    messages: list[dict],
    temperature: Optional[float] = None,
    max_tokens: int = 2048,
) -> str:
    """
    非流式对话：一次性拿到完整回答。
    适用于评测、批量摘要生成等不需要打字机效果的场景。

    参数：
        messages:    对话消息列表，格式 [{"role": "system"/"user"/"assistant", "content": "..."}]
        temperature: 随机性参数，不传则用配置中的默认值（知识问答场景要求低温度保证准确）
        max_tokens:  最大生成长度
    返回：
        模型生成的文本内容
    """
    # 组装请求体，遵循 OpenAI Chat Completions 规范
    payload = {
        "model": config.LLM_MODEL,  # 使用配置中指定的模型
        "messages": messages,  # 对话历史
        # 如果调用方没指定温度，就用配置里的默认值
        "temperature": config.LLM_TEMPERATURE if temperature is None else temperature,
        "max_tokens": max_tokens,  # 限制生成长度，防止失控
        "stream": False,  # 明确声明非流式
    }
    try:
        # 发起 POST 请求，超时时间取自配置
        resp = requests.post(
            _build_url(),  # 接口地址
            headers=_build_headers(),  # 鉴权头
            json=payload,  # 请求体自动序列化为 JSON
            timeout=config.LLM_TIMEOUT,  # 超时保护，防止请求挂死
        )
    except requests.RequestException as exc:  # 捕获网络层异常（连接失败、超时等）
        raise LLMError(f"模型服务连接失败：{exc}") from exc  # 转换成业务异常抛出

    if resp.status_code != 200:  # HTTP 状态码非 200 表示调用出错
        # 截取前 300 个字符的错误信息，避免超长错误刷屏
        raise LLMError(f"模型返回错误 HTTP {resp.status_code}：{resp.text[:300]}")

    try:
        data = resp.json()  # 解析 JSON 响应
        # 按 OpenAI 规范逐层取出生成的文本内容
        return data["choices"][0]["message"]["content"]
    except (ValueError, KeyError, IndexError) as exc:  # 响应结构不符合预期
        raise LLMError(f"模型响应格式异常：{resp.text[:300]}") from exc


def chat_completion_stream(
    messages: list[dict],
    temperature: Optional[float] = None,
    max_tokens: int = 2048,
) -> Generator[str, None, None]:
    """
    流式对话：逐字返回，实现打字机效果。
    这是满足"问答首字节 < 3 秒"体验要求的关键（SRS NFR-1）。

    用法：
        for piece in chat_completion_stream(msgs):
            print(piece, end="")

    返回：
        字符串生成器，每次产出一小段新增文本
    """
    # 请求体与非流式基本相同，区别在 stream=True
    payload = {
        "model": config.LLM_MODEL,
        "messages": messages,
        "temperature": config.LLM_TEMPERATURE if temperature is None else temperature,
        "max_tokens": max_tokens,
        "stream": True,  # 开启流式返回
    }
    try:
        # stream=True 告诉 requests 不要一次性下载完整响应体，而是边收边处理
        resp = requests.post(
            _build_url(),
            headers=_build_headers(),
            json=payload,
            timeout=config.LLM_TIMEOUT,
            stream=True,  # 关键参数：启用流式接收
        )
    except requests.RequestException as exc:
        raise LLMError(f"模型服务连接失败：{exc}") from exc

    if resp.status_code != 200:
        raise LLMError(f"模型返回错误 HTTP {resp.status_code}：{resp.text[:300]}")

    # 按行迭代响应流。SSE（Server-Sent Events）协议规定每条消息占一行，以 "data: " 开头
    for raw_line in resp.iter_lines(decode_unicode=True):
        if not raw_line:  # 空行是 SSE 的消息分隔符，跳过
            continue
        line = raw_line.strip()  # 去除首尾空白
        if not line.startswith("data:"):  # 不是数据行则忽略（可能是注释或心跳）
            continue
        data_str = line[5:].strip()  # 去掉 "data:" 前缀，剩下的是 JSON 内容
        if data_str == "[DONE]":  # 这是流结束的约定标记
            break  # 结束循环
        try:
            chunk = json.loads(data_str)  # 解析这一小块 JSON
        except json.JSONDecodeError:  # 偶发的不完整数据行，跳过即可，不影响整体
            continue
        try:
            # delta 里装的是本次新增的增量内容
            delta = chunk["choices"][0].get("delta", {})
            piece = delta.get("content")  # 取出文本增量
            if piece:  # 有实际内容才产出（有些 chunk 只含角色信息，content 为空）
                yield piece  # 用 yield 把这段文本交给调用方
        except (KeyError, IndexError):  # 结构异常的 chunk 直接跳过，保证流不中断
            continue


def llm_health() -> dict:
    """
    大模型服务健康检查。
    用于管理界面的"连通性测试"按钮，让运维一眼看出模型是否可用。

    返回：
        {"ok": 是否正常, "provider": 服务商, "model": 模型名, "message": 说明}
    """
    try:
        # 发一个极简的测试请求，max_tokens 设为 10 以节省费用和时间
        reply = chat_completion(
            [{"role": "user", "content": "回复两个字：正常"}],
            max_tokens=10,
        )
        return {
            "ok": True,  # 调用成功
            "provider": config.LLM_PROVIDER,  # 当前服务商
            "model": config.LLM_MODEL,  # 当前模型
            "message": f"连通正常，模型回复：{reply.strip()[:50]}",  # 附上实际回复以证明真的通了
        }
    except LLMError as exc:  # 捕获模型异常，转成结构化的失败结果而非抛出
        return {
            "ok": False,  # 调用失败
            "provider": config.LLM_PROVIDER,
            "model": config.LLM_MODEL,
            "message": str(exc),  # 把具体错误原因带给运维
        }
