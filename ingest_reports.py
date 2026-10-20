"""Run explicitly by an operator; never invoked by the chat application."""
import argparse, base64, hashlib, json, os, re, tempfile
from pathlib import Path
import fitz, openai
from dotenv import load_dotenv
from llama_cloud_services import LlamaParse
from langchain_core.documents import Document
from langchain.text_splitter import RecursiveCharacterTextSplitter
from langchain_community.embeddings import CohereEmbeddings
from google.cloud import storage
from report_store import PIPELINE_VERSION, sha256
load_dotenv()

def process_pdf(path):
    PDF_PATH = str(path)
    with tempfile.TemporaryDirectory() as work:
        EXTRACTED_FOLDER = work
        # === Step 1: Extract pages of interest as images ===
        target_headings = {
            "income_approach": {"text": "INCOME APPROACH", "take": 1},
            "market_approach": {"text": "MARKET APPROACH", "take": 2},
        }
        valuation_summary_text = "VALUATION SUMMARY"
        valuation_summary_page = None

        doc = fitz.open(PDF_PATH)
        heading_pages = {key: [] for key in target_headings}

        for i in range(len(doc)):
            if i < 5:
                continue
            text = doc[i].get_text().upper()
            for key, config in target_headings.items():
                if config["text"] in text:
                    heading_pages[key].append(i)
            if valuation_summary_page is None and valuation_summary_text in text:
                valuation_summary_page = i
        doc.close()

        final_selections = []
        for key, pages in heading_pages.items():
            take_index = target_headings[key]["take"] - 1
            if take_index < len(pages):
                selected_page = pages[take_index]
                final_selections.append((selected_page, key))
                if key == "market_approach":
                    with fitz.open(PDF_PATH) as count_doc:
                        total_pages = len(count_doc)
                    if selected_page + 1 < total_pages:
                        final_selections.append((selected_page + 1, f"{key}_continued"))
        if valuation_summary_page is not None:
            final_selections.append((valuation_summary_page, "valuation_summary"))

        # Render selected pages one at a time, retaining no full-document images.
        with fitz.open(PDF_PATH) as render_doc:
            for idx, label in final_selections:
                output_path = os.path.join(
                    EXTRACTED_FOLDER, f"{label}_page_{idx+1}.png"
                )
                pixmap = render_doc[idx].get_pixmap(dpi=300, alpha=False)
                pixmap.save(output_path)
                del pixmap

        for filename in sorted(os.listdir(EXTRACTED_FOLDER)):
            if filename.endswith(".png"):
                image_path = os.path.join(EXTRACTED_FOLDER, filename)
                match = re.search(r'page_(\d+)', filename)
                if not match:
                    continue
                page_num = match.group(1)
                with open(image_path, "rb") as image_file:
                    image_bytes = image_file.read()
                    base64_image = base64.b64encode(image_bytes).decode("utf-8")
                response = openai.chat.completions.create(
                    model="gpt-4o",
                    messages=[
                        {
                            "role": "user",
                            "content": [
                                {"type": "text", "text": "Extract all values from this image."},
                                {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{base64_image}"}},
                            ],
                        },
                    ],
                    max_tokens=512,
                )
                output_text = response.choices[0].message.content
                with open(os.path.join(EXTRACTED_FOLDER, f"page_{page_num}.txt"), "w") as f:
                    f.write(output_text)

        def parse_pdf():
            parser = LlamaParse(api_key=os.environ["LLAMA_CLOUD_API_KEY"], num_workers=4)
            result = parser.parse(PDF_PATH)
            pages = []
            for page in result.pages:
                page_num = page.page
                replacement_path = os.path.join(EXTRACTED_FOLDER, f"page_{page_num}.txt")
                if os.path.exists(replacement_path):
                    with open(replacement_path) as f:
                        content = f.read().strip()
                else:
                    content = page.md.strip()
                    cleaned = [line for line in content.splitlines() if line.strip() and line.strip().lower() != "null"]
                    content = "\n".join(cleaned)
                if content:
                    pages.append(Document(page_content=content, metadata={"page_number": page_num}))
            return pages

        docs = parse_pdf()
        splitter = RecursiveCharacterTextSplitter(chunk_size=3300, chunk_overlap=0)
        split_docs = splitter.split_documents(docs)
        for i, doc in enumerate(split_docs):
            doc.metadata["chunk_id"] = i + 1


        if not split_docs:
            raise ValueError("No text extracted; report was not published")
        embedding = CohereEmbeddings(model="embed-english-v3.0", user_agent="langchain")
        vectors = embedding.embed_documents([d.page_content for d in split_docs])
        if len(vectors) != len(split_docs):
            raise ValueError("Embedding count mismatch")
        return {"model": "embed-english-v3.0", "records": [
            {"text": d.page_content, "metadata": d.metadata, "embedding": v}
            for d, v in zip(split_docs, vectors)]}

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--bucket", required=True)
    parser.add_argument("--reports", default="reports")
    parser.add_argument("--project", default="valuebuddy-demo")
    args = parser.parse_args()
    bucket = storage.Client(project=args.project).bucket(args.bucket)
    manifest_blob = bucket.blob("valuebuddy/catalog.json")
    if manifest_blob.exists():
        manifest_blob.reload()
        generation = int(manifest_blob.generation)
        catalog = json.loads(manifest_blob.download_as_bytes(if_generation_match=generation))
    else:
        generation, catalog = 0, {"reports": []}
    pdfs = sorted(Path(args.reports).glob("*.pdf"))
    if not pdfs:
        raise ValueError("No PDF files found")
    for path in pdfs:
        report_id = path.stem
        pdf_bytes = path.read_bytes()
        revision = sha256(pdf_bytes + PIPELINE_VERSION.encode())
        existing = next((r for r in catalog["reports"] if r["id"] == report_id), None)
        prefix = f"valuebuddy/reports/{report_id}/{revision}"
        if existing and existing["revision"] == revision:
            print(f"Skipping already processed report: {report_id}", flush=True)
            continue
        index_blob = bucket.blob(prefix + "/index.json")
        pdf_blob = bucket.blob(prefix + "/report.pdf")
        # Checkpoint completed reports locally so a failed upload needn't repeat API calls.
        checkpoint = Path(".ingestion-cache") / revision
        checkpoint.mkdir(parents=True, exist_ok=True)
        index_path = checkpoint / "index.json"
        if index_path.exists():
            index_bytes = index_path.read_bytes()
        elif index_blob.exists() and pdf_blob.exists():
            index_bytes = index_blob.download_as_bytes()
        else:
            print(f"Processing {path.name} (paid parsing/embedding/vision calls)", flush=True)
            index_bytes = json.dumps(process_pdf(path)).encode()
            temporary = checkpoint / "index.tmp"
            temporary.write_bytes(index_bytes)
            temporary.replace(index_path)
        pdf_blob.upload_from_string(pdf_bytes, content_type="application/pdf")
        index_blob.upload_from_string(index_bytes, content_type="application/json")
        entry = {"id": report_id, "title": path.stem.replace("-", " ").title(),
                 "revision": revision, "pdf": pdf_blob.name, "index": index_blob.name,
                 "pdf_sha256": sha256(pdf_bytes), "index_sha256": sha256(index_bytes)}
        catalog["reports"] = [r for r in catalog["reports"] if r["id"] != report_id] + [entry]
        # Publish only after all artifacts exist; detect concurrent catalog edits.
        manifest_blob.upload_from_string(json.dumps(catalog), content_type="application/json",
                                         if_generation_match=generation)
        manifest_blob.reload()
        generation = int(manifest_blob.generation)
        print(f"Published {report_id}", flush=True)
    print("Done. Reports are ready in the dropdown.", flush=True)

if __name__ == "__main__":
    main()
