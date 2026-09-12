"""
LLM 只在这里初始化一次，其他地方都不应该直接 new 一个 ChatOpenAI。
这样以后想从 OpenAI 切换到智谱 GLM / DeepSeek 等任何 OpenAI 兼容接口，
只需要改 .env 里的 BASE_URL 和 MODEL_NAME，不用动任何业务代码。

V7 增强：
- 从 config.py 读取配置（类型安全、启动时校验）
- 增加 timeout 和 max_retries 参数，防止 LLM 调用无限挂起

为什么用 ChatOpenAI 而不是各家专属 SDK：
绝大多数国产模型（智谱GLM、DeepSeek、Moonshot等）都提供了 OpenAI 兼容的
/chat/completions 接口，所以复用 langchain-openai 的 ChatOpenAI 客户端，
换 base_url 就能接入，不需要为每个厂商单独写一套 LLM 封装。
"""

from langchain_openai import ChatOpenAI

from config import settings


def get_llm():
    """
    从 config.py 读取配置，返回一个已绑定好的 ChatOpenAI 实例。

    .env 里需要配置：
        LLM_API_KEY=你的key
        LLM_BASE_URL=https://open.bigmodel.cn/api/paas/v4/   # 智谱GLM举例，换厂商就换这行
        LLM_MODEL_NAME=glm-4-plus                              # 换成你要用的模型名

    V7：增加了 timeout（默认 60s）和 max_retries（默认 2），
    防止 LLM 服务抖动时整个请求无限挂起。
    """
    if not settings.llm_api_key:
        raise RuntimeError("没有找到 LLM_API_KEY，请复制 .env.example 为 .env 并填入你的真实 key")

    return ChatOpenAI(
        api_key=settings.llm_api_key,
        base_url=settings.llm_base_url or None,
        model=settings.llm_model_name,
        temperature=0,
        timeout=settings.llm_timeout,
        max_retries=settings.llm_max_retries,
    )
