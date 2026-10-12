"""Langfuse tracing for /ask. Does nothing when the keys are missing or langfuse is not installed."""

import logging
import os
import re
from contextlib import contextmanager

log = logging.getLogger("rag.tracing")
_ARABIC = re.compile(r"[؀-ۿ]")


def enabled() -> bool:
    if not (os.environ.get("LANGFUSE_PUBLIC_KEY") and os.environ.get("LANGFUSE_SECRET_KEY")):
        return False
    try:
        import langfuse  # noqa: F401
    except ImportError:
        return False
    return True


def _short(value: str | None, limit: int = 200) -> str | None:
    """Langfuse accepts user and session ids up to 200 characters."""
    return str(value)[:limit] if value else None


@contextmanager
def _own_trace_context():
    """Start Langfuse traces from a clean OpenTelemetry context.

    BentoML wraps each request in its own span and, by default, marks it "not sampled".
    Langfuse spans created inside it inherit that decision and are silently dropped.
    """
    from opentelemetry import context as otel_context

    token = otel_context.attach(otel_context.Context())
    try:
        yield
    finally:
        otel_context.detach(token)


def _record_answer(lf, root, result: dict) -> None:
    """Attach the answer's details and two cheap scores to the current trace."""
    try:
        answer = result.get("answer", "")
        sources = result.get("sources") or []
        refused = not sources  # a refusal ("not found" / "repealed") carries no citations
        root.update(
            output=result,
            metadata={
                "answer_chars": len(answer),
                "source_count": len(sources),
                "sources": sources,
                "refused": refused,
            },
        )
        lf.score_current_trace(name="refused", value=float(refused), data_type="BOOLEAN")
        lf.score_current_trace(name="source_count", value=float(len(sources)), data_type="NUMERIC")
    except Exception:
        log.exception("could not record answer details in Langfuse")  # never break /ask


def traced_query(question: str, fn, *, session_id: str | None = None, user_id: str | None = None):
    """Run fn(question) inside a Langfuse trace. Returns (result, trace_id).

    trace_id is None when tracing is off. Token usage and cost come from the LLM call itself
    (see the langfuse.openai client in generation/llm_client.py), which nests under this trace.
    """
    if not enabled():
        return fn(question), None

    from langfuse import get_client, propagate_attributes

    from ..config import load_params

    lf = get_client()
    lang = "ar" if _ARABIC.search(question) else "en"
    try:
        p = load_params()
    except Exception:
        p = {}
    config = {
        k: p.get(k)
        for k in ("embedding_model", "top_k", "min_score", "max_context_articles", "chunk_text")
    }
    config["llm_model"] = os.environ.get("LLM_MODEL", "")

    # propagate_attributes must wrap the root observation so session, user and tags land on the trace
    with (
        _own_trace_context(),
        propagate_attributes(
            session_id=_short(session_id),
            user_id=_short(user_id),
            tags=["rag", lang],
            metadata={"lang": lang},
        ),
    ):
        with lf.start_as_current_observation(
            as_type="span",
            name="ask",
            input={"question": question},
            metadata={"lang": lang, **config},
        ) as root:
            trace_id = lf.get_current_trace_id()
            try:
                result = fn(question)
            except Exception as exc:
                root.update(level="ERROR", status_message=f"{type(exc).__name__}: {exc}"[:500])
                raise
            _record_answer(lf, root, result)
    return result, trace_id


def flush() -> None:
    if not enabled():
        return
    try:
        from langfuse import get_client

        get_client().flush()
    except Exception:
        pass


class _StreamTrace:
    """Handle for the span of one streamed answer."""

    def __init__(self, root):
        self.root = root

    def finish(self, answer: str, sources: list[str], refused: bool) -> None:
        if self.root is None:
            return
        try:
            self.root.update(
                output=answer,
                metadata={
                    "answer_chars": len(answer),
                    "source_count": len(sources),
                    "sources": sources,
                    "refused": refused,
                },
            )
            self.root.score_trace(name="refused", value=float(refused), data_type="BOOLEAN")
            self.root.score_trace(
                name="source_count", value=float(len(sources)), data_type="NUMERIC"
            )
        except Exception:
            pass


@contextmanager
def stream_trace(question, lang, params, session_id=None, user_id=None):
    """Like traced_query, but for generators: the span stays open until the stream ends."""
    if not enabled():
        yield _StreamTrace(None)
        return
    from langfuse import get_client, propagate_attributes

    with (
        _own_trace_context(),
        propagate_attributes(
            session_id=_short(session_id),
            user_id=_short(user_id),
            tags=["rag", "bento", lang],
            metadata={"lang": lang},
        ),
    ):

        with get_client().start_as_current_observation(
            as_type="span",
            name="ask_stream",
            input=question,
            metadata={
                "lang": lang,
                "top_k": params["top_k"],
                "min_score": params["min_score"],
                "max_context_articles": params["max_context_articles"],
            },
        ) as root:
            yield _StreamTrace(root)
