import os

from openai import OpenAI


def _enable_langfuse() -> None:
    if os.environ.get("LANGFUSE_PUBLIC_KEY") and os.environ.get("LANGFUSE_SECRET_KEY"):
        try:
            import langfuse.openai  # noqa: F401
        except ImportError:
            pass


def make_llm(params: dict):
    """Return a function messages -> answer text. Reads the endpoint from the environment."""
    _enable_langfuse()
    client = OpenAI(
        base_url=os.environ["LLM_BASE_URL"],
        api_key=os.environ["LLM_API_KEY"],
    )
    model = os.environ["LLM_MODEL"]

    def complete(messages: list[dict]) -> str:
        res = client.chat.completions.create(
            model=model,
            messages=messages,
            temperature=params["llm_temperature"],
            max_tokens=params["llm_max_tokens"],
        )
        return (res.choices[0].message.content or "").strip()

    return complete
