"""BentoML service: async /ask and token-streaming /ask_stream (same logic as generation/answer.py)."""

import asyncio
import os
from collections.abc import AsyncGenerator

import bentoml
from bentoml.exceptions import InvalidArgument
from openai import AsyncOpenAI

from arabic_legal_rag import rag
from arabic_legal_rag.config import load_params
from arabic_legal_rag.generation.citations import sources_for
from arabic_legal_rag.generation.llm_client import _enable_langfuse
from arabic_legal_rag.generation.prompts import NOT_FOUND, REPEALED, build_messages, detect_lang
from arabic_legal_rag.observability import tracing


def _ids(ctx: bentoml.Context) -> tuple[str | None, str | None]:
    """Same headers the Streamlit UI sends to FastAPI."""
    h = ctx.request.headers
    return h.get("x-session-id"), h.get("x-user-id")


@bentoml.service(traffic={"timeout": 120, "concurrency": 32})
class RagService:
    def __init__(self) -> None:
        self.params = load_params()
        rag.documents_indexed()  # fail fast if the index is missing
        _enable_langfuse()  # must run before the first model call so it is traced
        self.llm = AsyncOpenAI(
            base_url=os.environ["LLM_BASE_URL"], api_key=os.environ.get("LLM_API_KEY", "none")
        )
        print(
            "LANGFUSE enabled:",
            tracing.enabled(),
            "| host:",
            os.environ.get("LANGFUSE_HOST"),
            flush=True,
        )

    @bentoml.on_shutdown
    def flush_traces(self) -> None:
        tracing.flush()  # send buffered traces before the process exits

    @bentoml.api
    async def ask(self, question: str, ctx: bentoml.Context) -> dict:
        if not question.strip():
            raise InvalidArgument("question must not be empty")
        session_id, user_id = _ids(ctx)
        # the whole traced call runs in one worker thread, so the trace context stays together
        result, _trace_id = await asyncio.to_thread(
            tracing.traced_query, question, rag.query, session_id=session_id, user_id=user_id
        )
        return result

    @bentoml.api
    async def ask_stream(self, question: str, ctx: bentoml.Context) -> AsyncGenerator[str, None]:
        if not question.strip():
            raise InvalidArgument("question must not be empty")
        session_id, user_id = _ids(ctx)
        async for token in self._stream(question, session_id, user_id):
            yield token

    async def _stream(self, question, session_id, user_id) -> AsyncGenerator[str, None]:
        p = self.params
        lang = detect_lang(question)
        with tracing.stream_trace(question, lang, p, session_id, user_id) as trace:
            hits = await asyncio.to_thread(rag.retrieve, question, p["top_k"])

            lookup = [h for h in hits if h["via"] == "lookup"]
            for h in lookup:
                if h["is_repealed"]:
                    trace.finish("", [], refused=True)
                    yield REPEALED[lang].format(n=h["article_number"])
                    return
            context = lookup or [h for h in hits if h["score"] >= p["min_score"]]
            context = context[: p["max_context_articles"]]
            if not context:
                trace.finish("", [], refused=True)
                yield NOT_FOUND[lang]
                return

            stream = await self.llm.chat.completions.create(
                model=os.environ["LLM_MODEL"],
                messages=build_messages(question, context, lang),
                stream=True,
                stream_options={
                    "include_usage": True
                },  # without it, streamed calls report no tokens
            )
            parts: list[str] = []
            async for chunk in stream:
                if chunk.choices and chunk.choices[0].delta.content:
                    token = chunk.choices[0].delta.content
                    parts.append(token)
                    yield token
            answer = "".join(parts)
            sources = sources_for(answer, context)
            trace.finish(answer, sources, refused=not sources)
            yield ("\n\nSources: " + "; ".join(sources)) if sources else ("\n\n" + NOT_FOUND[lang])
