from datetime import datetime, timedelta, timezone
from dataclasses import replace
import os
import time
import traceback
import uuid

from sqlalchemy import delete, or_, select

from ..database import SessionLocal
from ..db_models import Document, DocumentChunk
from ..ingestion import chunk_text
from ..pdf_ingestion import extract_pdf_structure
from ..retrieval import tokenize
from ..storage import LocalDocumentStorage


storage = LocalDocumentStorage()
POLL_SECONDS = float(os.environ.get("WORKER_POLL_SECONDS", "2"))
STALE_AFTER_SECONDS = int(os.environ.get("WORKER_STALE_AFTER_SECONDS", "600"))


def _claim_document() -> tuple[uuid.UUID, str, str, str] | None:
    now = datetime.now(timezone.utc)
    stale_before = now - timedelta(seconds=STALE_AFTER_SECONDS)
    with SessionLocal.begin() as session:
        statement = (
            select(Document)
            .where(
                or_(
                    Document.status == "queued",
                    (Document.status == "processing") & (Document.processing_started_at < stale_before),
                )
            )
            .order_by(Document.created_at)
            .with_for_update(skip_locked=True)
            .limit(1)
        )
        document = session.scalar(statement)
        if document is None:
            return None
        document.status = "processing"
        document.processing_started_at = now
        document.attempt_count += 1
        return document.id, document.storage_key, document.filename, document.extension


def _parsed_structure(text: str, extension: str) -> dict:
    lines = text.splitlines()
    blocks: list[dict] = []
    current: list[str] = []
    start = 0

    def flush(end_line: int) -> None:
        nonlocal current, start
        content = "\n".join(current).strip()
        if content:
            blocks.append({"type": "text", "line_start": start, "line_end": end_line, "text": content})
        current = []
        start = 0

    for number, line in enumerate(lines, start=1):
        if not line.strip():
            flush(number - 1)
            continue
        if not current:
            start = number
        current.append(line)
    flush(len(lines))
    return {
        "schema_version": 1,
        "format": "markdown" if extension == ".md" else "plain_text",
        "line_count": len(lines),
        "character_count": len(text),
        "blocks": blocks,
    }


def _process_document(document_id: uuid.UUID, storage_key: str, filename: str, extension: str) -> None:
    raw_content = storage.get(storage_key)
    if extension == ".pdf":
        structure = extract_pdf_structure(raw_content)
        pieces = []
        for page in structure["pages"]:
            for piece in chunk_text(page["text"], filename):
                pieces.append(replace(piece, page_start=page["page_number"], page_end=page["page_number"]))
        stored_structure = {
            **structure,
            "pages": [
                {key: page[key] for key in ("page_number", "width", "height", "method")}
                for page in structure["pages"]
            ],
        }
        final_status = "needs_review"
    else:
        content = raw_content.decode("utf-8-sig")
        structure = _parsed_structure(content, extension)
        stored_structure = structure
        pieces = chunk_text(content, filename)
        final_status = "ready"

    chunk_rows = [
        DocumentChunk(
            document_id=document_id,
            ordinal=ordinal,
            content=piece.text,
            search_text=" ".join(tokenize(piece.text)),
            line_start=piece.line_start,
            line_end=piece.line_end,
            page_start=piece.page_start,
            page_end=piece.page_end,
        )
        for ordinal, piece in enumerate(pieces, start=1)
    ]

    with SessionLocal.begin() as session:
        document = session.get(Document, document_id)
        if document is None:
            return
        session.execute(delete(DocumentChunk).where(DocumentChunk.document_id == document_id))
        session.add_all(chunk_rows)
        document.parsed_structure = stored_structure
        document.approved_fields = None
        document.reviewed_at = None
        document.status = final_status
        document.error_message = None
        document.processing_started_at = None
        document.updated_at = datetime.now(timezone.utc)


def _mark_failed(document_id: uuid.UUID, error: Exception) -> None:
    with SessionLocal.begin() as session:
        document = session.get(Document, document_id)
        if document is None:
            return
        document.status = "failed"
        document.error_message = str(error)[:1000]
        document.processing_started_at = None
        document.updated_at = datetime.now(timezone.utc)


def run_forever() -> None:
    print("Indexador de documentos iniciado; esperando trabajos en PostgreSQL.", flush=True)
    while True:
        try:
            claimed = _claim_document()
            if claimed is None:
                time.sleep(POLL_SECONDS)
                continue
            document_id, storage_key, filename, extension = claimed
            try:
                _process_document(document_id, storage_key, filename, extension)
                print(f"Documento indexado: {filename} ({document_id})", flush=True)
            except Exception as exc:
                _mark_failed(document_id, exc)
                print(f"Falló la indexación de {filename}: {exc}", flush=True)
                traceback.print_exc()
        except Exception:
            traceback.print_exc()
            time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    run_forever()
