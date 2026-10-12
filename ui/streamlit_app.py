"""Streamlit chat UI for the Arabic Legal RAG API.

A thin client: it only calls the running API over HTTP, so it needs `streamlit` and `httpx`
and nothing from the RAG package. Two backends are supported:

* FastAPI   POST {url}/ask         -> {"answer": ..., "sources": [...]}   (default, port 8000)
* BentoML   POST {url}/ask_stream  -> plain-text token stream             (port 3001)

Run:  uv run --no-sync streamlit run ui/streamlit_app.py
"""

import os
import time
import uuid

import httpx
import streamlit as st

FASTAPI_URL = os.environ.get("RAG_API_URL", "http://localhost:8000")
BENTO_URL = os.environ.get("RAG_BENTO_URL", "http://localhost:3001")
TIMEOUT = 120

EXAMPLES = [
    "ما نص المادة 147؟",
    "What does article 147 say?",
    "ما هي مدة التقادم؟",
    "What is the rule on unjust enrichment?",
]

st.set_page_config(page_title="Egyptian Civil Code Q&A", page_icon="⚖️", layout="centered")

# Each paragraph picks its own direction, so Arabic answers read right-to-left and English left-to-right.
st.markdown(
    """
    <style>
      [data-testid="stChatMessage"] p, [data-testid="stChatMessage"] li {
        unicode-bidi: plaintext; text-align: start; line-height: 1.8;
      }
    </style>
    """,
    unsafe_allow_html=True,
)


def tracking_headers() -> dict[str, str]:
    """Ids the API forwards to Langfuse: one session per browser tab, plus an optional user name."""
    headers = {"X-Session-Id": st.session_state["session_id"]}
    user = st.session_state.get("user_name", "").strip()
    if user:
        headers["X-User-Id"] = user
    return headers


def ask_fastapi(base_url: str, question: str) -> dict:
    r = httpx.post(
        f"{base_url}/ask", json={"question": question}, headers=tracking_headers(), timeout=TIMEOUT
    )
    if r.status_code == 503:
        raise RuntimeError("The LLM or embedding backend is not configured or not reachable (503).")
    if r.status_code == 422:
        raise RuntimeError("The question was rejected as invalid (422).")
    r.raise_for_status()
    return r.json()


def stream_bento(base_url: str, question: str):
    """Yield text chunks as the server produces them."""
    with httpx.stream(
        "POST",
        f"{base_url}/ask_stream",
        json={"question": question},
        headers=tracking_headers(),
        timeout=TIMEOUT,
    ) as r:
        if r.status_code >= 400:
            r.read()
            raise RuntimeError(f"BentoML returned {r.status_code}: {r.text[:200]}")
        yield from r.iter_text()


def check_health(base_url: str) -> str:
    try:
        r = httpx.get(f"{base_url}/health", timeout=5)
        r.raise_for_status()
        return f"✅ online, {r.json().get('documents_indexed', '?')} articles indexed"
    except httpx.HTTPError as exc:
        return f"❌ not reachable ({type(exc).__name__})"


# ---------- sidebar ----------
st.session_state.setdefault("session_id", uuid.uuid4().hex)

with st.sidebar:
    st.header("Settings")
    st.text_input("Your name (optional, shown in Langfuse)", key="user_name")
    backend = st.radio("Backend", ["FastAPI (/ask)", "BentoML (streaming)"])
    streaming = backend.startswith("BentoML")
    base_url = st.text_input("API address", BENTO_URL if streaming else FASTAPI_URL).rstrip("/")
    if not streaming and st.button("Check connection"):
        st.write(check_health(base_url))

    st.divider()
    st.subheader("Try an example")
    for i, example in enumerate(EXAMPLES):
        if st.button(example, key=f"ex{i}", use_container_width=True):
            st.session_state["pending"] = example

    st.divider()
    if st.button("Clear conversation", use_container_width=True):
        st.session_state["messages"] = []
        st.session_state["session_id"] = (
            uuid.uuid4().hex
        )  # a new conversation = a new Langfuse session
        st.rerun()

# ---------- conversation ----------
st.title("⚖️ Egyptian Civil Code Q&A")
st.caption("Ask in Arabic or English. Answers cite the articles they are based on.")

messages = st.session_state.setdefault("messages", [])

for m in messages:
    with st.chat_message(m["role"]):
        st.markdown(m["content"])
        if m.get("sources"):
            with st.expander(f"Sources ({len(m['sources'])})"):
                for s in m["sources"]:
                    st.markdown(f"- {s}")
        if m.get("seconds") is not None:
            st.caption(f"{m['seconds']:.1f} s")

question = st.chat_input("Type your question…") or st.session_state.pop("pending", None)

if question:
    messages.append({"role": "user", "content": question})
    with st.chat_message("user"):
        st.markdown(question)

    with st.chat_message("assistant"):
        start = time.perf_counter()
        sources: list[str] = []
        try:
            if streaming:
                answer = st.write_stream(stream_bento(base_url, question))
            else:
                with st.spinner("Searching the Civil Code…"):
                    data = ask_fastapi(base_url, question)
                answer, sources = data["answer"], data.get("sources", [])
                st.markdown(answer)
        except httpx.ConnectError:
            answer = f"Cannot reach the API at `{base_url}`. Is it running?"
            st.error(answer)
        except httpx.TimeoutException:
            answer = "The request timed out. Please try again."
            st.error(answer)
        except (RuntimeError, httpx.HTTPError) as exc:
            answer = str(exc)
            st.error(answer)
        seconds = time.perf_counter() - start

        if sources:
            with st.expander(f"Sources ({len(sources)})", expanded=True):
                for s in sources:
                    st.markdown(f"- {s}")
        st.caption(f"{seconds:.1f} s")

    messages.append(
        {"role": "assistant", "content": answer, "sources": sources, "seconds": seconds}
    )
