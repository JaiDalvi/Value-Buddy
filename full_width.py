import html
import streamlit as st
import os
import re
import fitz
import openai
import base64

from langchain_core.documents import Document
from langchain.text_splitter import RecursiveCharacterTextSplitter

from langchain_community.embeddings import CohereEmbeddings
from langchain_community.vectorstores import FAISS
from langchain.retrievers.document_compressors import CohereRerank
from langchain.retrievers import ContextualCompressionRetriever

from langchain_openai import ChatOpenAI
from langchain_core.prompts import PromptTemplate
from dotenv import load_dotenv
load_dotenv()

import time
import io
import base64
from PIL import Image
import tempfile

openai.api_key = os.environ["OPENAI_API_KEY"]

st.set_page_config(page_title="Valuation RAG Chatbot", layout="wide")
st.title("Underwriting Agent")

from report_store import get_catalog, load_report

try:
    catalog = get_catalog()
except Exception:
    st.error("The report library is unavailable. Please contact the demo administrator.")
    st.stop()
reports = catalog.get("reports", [])
if not reports:
    st.info("No processed reports are available yet.")
    st.stop()
by_id = {report["id"]: report for report in reports}
selected = st.selectbox("Choose a processed valuation report", list(by_id),
                        format_func=lambda key: by_id[key]["title"])
report = by_id[selected]
identity = (report["id"], report["revision"])
if st.session_state.get("active_report") != identity:
    for key in ("initialized", "messages", "retriever", "reranker", "pdf_path"):
        st.session_state.pop(key, None)
    st.session_state.active_report = identity

# === CSS Styling for chat bubbles ===
st.markdown("""
<style>
html, body {
    font-size: 16px !important;
    line-height: 1.6;
    font-family: "Segoe UI", sans-serif;
}
.user-bubble {
    background-color: #007bff;
    color: white;
    padding: 10px;
    border-radius: 12px;
    max-width: 60%;
    float: right;
    margin: 5px 0 10px auto;
    text-align: right;
    font-size: 18px;
}
.assistant-bubble {
    background-color: #1e1e1e;
    color: white;
    padding: 10px;
    border-radius: 12px;
    max-width: 60%;
    float: left;
    margin: 5px auto 10px 0;
    text-align: left;
    font-size: 18px;
}
.system-bubble {
    background-color: #1e1e1e;
    color: white;
    padding: 10px;
    border-radius: 12px;
    max-width: 60%;
    margin: 5px auto 10px auto;
    text-align: left;
    font-size: 18px;
}
.clearfix::after {
    content: "";
    display: block;
    clear: both;
}
</style>
""", unsafe_allow_html=True)


# Load saved vectors; this performs no parsing or document embedding.
if "initialized" not in st.session_state:
    try:
        with st.spinner("Loading saved report..."):
            index, pdf_path = load_report(report)
            embedding = CohereEmbeddings(model=index["model"], user_agent="langchain")
            records = index["records"]
            vectorstore = FAISS.from_embeddings(
                [(r["text"], r["embedding"]) for r in records], embedding,
                metadatas=[r["metadata"] for r in records])
            retriever = vectorstore.as_retriever(search_type="mmr", search_kwargs={
                "k": 50, "fetch_k": 100, "lambda_mult": 0.9})
            reranker = CohereRerank(model="rerank-english-v3.0", user_agent="langchain", top_n=20)
            st.session_state.retriever = ContextualCompressionRetriever(
                base_retriever=retriever, base_compressor=reranker)
            st.session_state.reranker = reranker
            st.session_state.pdf_path = str(pdf_path)
            st.session_state.initialized = True
    except Exception:
        st.error("Could not load this report. Please contact the demo administrator.")
        st.stop()
PDF_PATH = st.session_state.pdf_path

# === Prompt ===
prompt = PromptTemplate(
        template = """
        You are a financial-data extraction assistant.
    
    **Use ONLY what appears under “Context”.**
    
    ### How to answer
    1. **Single value questions**  
       • Find the row + column that match the user's words.  
       • Return the answer in a **short, clear sentence** using the exact number from the context.  
         Example: “The Income (DCF) approach value is $1,150,000.”  
       • **Do NOT repeat the metric name or company name** unless the user asks.
    
    2. **Table questions**  
       • Return the full table **with its header row** in GitHub-flavoured markdown.
    
    3. **Valuation method / theory / reasoning questions**
       • If the question involves **valuation methods**, **concluded value**, or topics like **Income Approach**, **Market Approach**, or **Valuation Summary**, do the following:
         - Combine and synthesize relevant information across all chunks.
         - Pay special attention to how **weights are distributed** (e.g., “50% DCF, 25% EBITDA, 25% SDE”).
         - Avoid oversimplifying if more detailed breakdowns (like subcomponents of market approach) are available.
         - If a table gives a simplified view (e.g., "50% Market Approach"), but other parts break it down (e.g., 25% EBITDA + 25% SDE), **prefer the detailed breakdown with percent value**.   
         - When describing weights, also mention the **corresponding dollar values** used in the context (e.g., “50% DCF = $3,712,000, 25% EBITDA = $4,087,000...”)
         - **If Market approach is composed of sub-methods like EBITDA and SDE, then explicitly extract and show their individual weights and values, even if not listed together in a single table.**
    
    
    4. **Theory/textual question**  
       • Try to return an explanation **based on the context**.
    
       
    If you still cannot see the answer, reply **“I don't know.”**
    
    ---
    Context:
    {context}
    
    ---
    Question: {question}
    Answer:""",
            input_variables=["context", "question"]
        )

def typewriter_output(answer):
    if answer.strip().startswith("```markdown"):
        markdown_table = answer.strip().removeprefix("```markdown").removesuffix("```" ).strip()
        st.markdown(markdown_table)
    else:
        container = st.empty()
        typed = ""
        for char in answer:
            typed += char
            container.markdown(f"<div class='assistant-bubble clearfix'>{html.escape(typed)}</div>", unsafe_allow_html=True)
            time.sleep(0.008)

# === Chat memory setup ===
if "messages" not in st.session_state:
    st.session_state.messages = [
        {"role": "assistant", "content": "Hi! I am here to answer any questions you may have about your valuation report."},
        {"role": "assistant", "content": "What can I help you with?"}
    ]

user_question = st.chat_input("Message")

# === Chat history display ===
for msg in st.session_state.messages:
    role_class = "user-bubble" if msg["role"]=="user" else "assistant-bubble"
    st.markdown(
        f"<div class='{role_class} clearfix'>{html.escape(msg['content'])}</div>",
        unsafe_allow_html=True
    )

    if msg["role"]=="assistant" and msg.get("source_img"):
        # a tiny click‑to‑expand box, no full‑width expander
       with st.popover(f"📘 Source Info (Page {msg['source']})"):
            # show only the snippet
            st.image(
                Image.open(io.BytesIO(base64.b64decode(msg["source_img"]))),
                caption=msg["source"],
                use_container_width=True
            )

# === Chat input and logic ===

if user_question:
    st.session_state.messages.append({"role": "user", "content": user_question})
    st.markdown(f"<div class='user-bubble clearfix'>{html.escape(user_question)}</div>", unsafe_allow_html=True)

    with st.spinner("Thinking..."):
        retrieved_docs = st.session_state.retriever.invoke(user_question)
        context_text = "\n\n".join(doc.page_content for doc in retrieved_docs)
        final_prompt = prompt.invoke({"context": context_text, "question": user_question})
        llm = ChatOpenAI(model="gpt-4o", temperature=0)
        response = llm.invoke(final_prompt)

        def find_best_matching_doc_reranker(answer_text, retrieved_docs, reranker):
            reranked = reranker.compress_documents(retrieved_docs, query=answer_text)
            return reranked[0] if reranked else None

        matched_doc = find_best_matching_doc_reranker(response.content, retrieved_docs, st.session_state.reranker)

        page = matched_doc.metadata.get("page_number") if matched_doc else None
        # Generate only the cited page preview, at display resolution.
        b64_img = None
        if page is not None:
            with fitz.open(PDF_PATH) as preview_doc:
                page_index = int(page) - 1
                if 0 <= page_index < len(preview_doc):
                    pixmap = preview_doc[page_index].get_pixmap(dpi=120, alpha=False)
                    b64_img = base64.b64encode(pixmap.tobytes("png")).decode("ascii")
                    del pixmap

        # append the assistant reply with source_img=b64_img
        st.session_state.messages.append({
            "role":       "assistant",
            "content":    response.content,
            "source":     f"Page {page}" if page else None,
            "source_img": b64_img
        })

        typewriter_output(response.content)

        if b64_img:
            with st.popover(f"📘 Source Info Page: {page}"):
                # decode base64 back into a PIL.Image
                img = Image.open(io.BytesIO(base64.b64decode(b64_img)))
                st.image(img, caption=f"Page {page}", use_container_width=True)




       