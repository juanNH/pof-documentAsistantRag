from datetime import datetime, timezone
import hashlib
import os
from pathlib import Path
import uuid
from urllib.parse import quote

from fastapi import Depends, FastAPI, File, HTTPException, Response, UploadFile, status
from pydantic import BaseModel, Field
from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..database import get_db
from ..db_models import Conversation, Document, DocumentChunk, Message, Workspace
from ..db_retrieval import PostgresRetriever
from ..providers import MockProvider
from ..retrieval import tokenize
from ..storage import LocalDocumentStorage


app = FastAPI(title="Document RAG API", version="0.2.0")
storage = LocalDocumentStorage()
retriever = PostgresRetriever()
provider = MockProvider()
DEFAULT_WORKSPACE_ID = uuid.UUID("84db1948-613e-5d55-b98f-46a6b304fd0e")
MAX_UPLOAD_BYTES = int(os.environ.get("MAX_UPLOAD_BYTES", str(15 * 1024 * 1024)))


class ConversationCreate(BaseModel):
    title: str = Field(default="Nueva conversación", min_length=1, max_length=180)


class MessageCreate(BaseModel):
    content: str = Field(min_length=1, max_length=5000)


class ReviewFieldInput(BaseModel):
    candidate_id: str | None = Field(default=None, max_length=64)
    key: str = Field(min_length=1, max_length=64, pattern=r"^[a-zA-Z0-9_]+$")
    label: str = Field(min_length=1, max_length=120)
    value: str = Field(max_length=2000)
    page: int | None = Field(default=None, ge=1, le=1000)
    evidence: str = Field(default="", max_length=800)


class DocumentReviewSubmit(BaseModel):
    fields: list[ReviewFieldInput] = Field(max_length=100)


def _workspace(session: Session) -> Workspace:
    workspace = session.get(Workspace, DEFAULT_WORKSPACE_ID)
    if workspace is None:
        workspace = Workspace(id=DEFAULT_WORKSPACE_ID, name="Espacio local")
        session.add(workspace)
        try:
            session.commit()
        except IntegrityError:
            session.rollback()
            workspace = session.get(Workspace, DEFAULT_WORKSPACE_ID)
            if workspace is None:
                raise
    return workspace


def _document_json(document: Document) -> dict:
    return {
        "id": str(document.id),
        "filename": document.filename,
        "size_bytes": document.size_bytes,
        "status": document.status,
        "error_message": document.error_message,
        "created_at": document.created_at,
        "reviewed_at": document.reviewed_at,
        "approved_field_count": len(document.approved_fields or []),
    }


def _message_json(message: Message) -> dict:
    return {
        "id": str(message.id),
        "role": message.role,
        "content": message.content,
        "citations": message.citations,
        "retrieved": message.retrieved,
        "abstained": message.abstained,
        "created_at": message.created_at,
    }


@app.get("/api/health")
def health(session: Session = Depends(get_db)) -> dict:
    session.execute(text("SELECT 1"))
    return {"status": "ok", "database": "connected"}


@app.get("/api/documents")
def list_documents(session: Session = Depends(get_db)) -> list[dict]:
    workspace = _workspace(session)
    documents = session.execute(
        select(
            Document.id,
            Document.filename,
            Document.size_bytes,
            Document.status,
            Document.error_message,
            Document.created_at,
        )
        .where(Document.workspace_id == workspace.id)
        .order_by(Document.created_at.desc())
    ).mappings()
    return [
        {**document, "id": str(document["id"])}
        for document in documents
    ]


@app.post("/api/documents", status_code=status.HTTP_202_ACCEPTED)
def upload_document(file: UploadFile = File(...), session: Session = Depends(get_db)) -> dict:
    raw_name = (file.filename or "").replace("\\", "/").rsplit("/", 1)[-1].strip()
    extension = Path(raw_name).suffix.lower()
    if not raw_name or extension not in {".txt", ".md", ".pdf"}:
        raise HTTPException(status_code=415, detail="Solo se aceptan archivos .txt, .md y .pdf")
    if len(raw_name) > 255:
        raise HTTPException(status_code=413, detail="El nombre del archivo supera 255 caracteres")

    content = file.file.read(MAX_UPLOAD_BYTES + 1)
    if len(content) > MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=413, detail=f"El tamaño máximo es {MAX_UPLOAD_BYTES // (1024 * 1024)} MB")
    if extension == ".pdf":
        if b"%PDF-" not in content[:1024]:
            raise HTTPException(status_code=422, detail="El archivo no parece ser un PDF válido")
    else:
        try:
            decoded = content.decode("utf-8-sig")
        except UnicodeDecodeError as exc:
            raise HTTPException(status_code=422, detail="El archivo debe estar codificado en UTF-8") from exc
        if "\x00" in decoded:
            raise HTTPException(status_code=422, detail="El archivo contiene bytes nulos y no parece texto")

    workspace = _workspace(session)
    digest = hashlib.sha256(content).hexdigest()
    duplicate = session.scalar(
        select(Document).where(Document.workspace_id == workspace.id, Document.sha256 == digest)
    )
    if duplicate:
        raise HTTPException(status_code=409, detail=f"El contenido ya está cargado como {duplicate.filename}")

    document_id = uuid.uuid4()
    try:
        storage_key = storage.put(document_id, extension, content)
    except OSError as exc:
        raise HTTPException(status_code=500, detail="No se pudo guardar el archivo en el almacenamiento local") from exc

    document = Document(
        id=document_id,
        workspace_id=workspace.id,
        filename=raw_name,
        extension=extension,
        storage_key=storage_key,
        sha256=digest,
        size_bytes=len(content),
        status="queued",
    )
    session.add(document)
    try:
        session.commit()
    except IntegrityError as exc:
        session.rollback()
        storage.delete(storage_key)
        raise HTTPException(status_code=409, detail="Ese documento ya está cargado") from exc
    except Exception:
        session.rollback()
        storage.delete(storage_key)
        raise
    session.refresh(document)
    return _document_json(document)


def _document_in_workspace(document_id: uuid.UUID, session: Session) -> Document:
    workspace = _workspace(session)
    document = session.scalar(
        select(Document).where(Document.id == document_id, Document.workspace_id == workspace.id)
    )
    if document is None:
        raise HTTPException(status_code=404, detail="No encontré ese documento")
    return document


@app.get("/api/documents/{document_id}/file")
def get_document_file(document_id: uuid.UUID, session: Session = Depends(get_db)) -> Response:
    document = _document_in_workspace(document_id, session)
    if document.extension != ".pdf":
        raise HTTPException(status_code=415, detail="La vista previa solo está disponible para PDF")
    try:
        content = storage.get(document.storage_key)
    except OSError as exc:
        raise HTTPException(status_code=404, detail="No encontré el archivo original") from exc
    return Response(
        content=content,
        media_type="application/pdf",
        headers={
            "Content-Disposition": f"inline; filename*=UTF-8''{quote(document.filename)}",
            "Cache-Control": "private, no-store",
            "X-Content-Type-Options": "nosniff",
        },
    )


@app.get("/api/documents/{document_id}/review")
def get_document_review(document_id: uuid.UUID, session: Session = Depends(get_db)) -> dict:
    document = _document_in_workspace(document_id, session)
    if document.extension != ".pdf":
        raise HTTPException(status_code=415, detail="La revisión de campos solo aplica a PDF")
    if document.status != "needs_review" or not document.parsed_structure:
        raise HTTPException(status_code=409, detail="El PDF todavía no está listo para revisión")
    return {
        "id": str(document.id),
        "filename": document.filename,
        "page_count": document.parsed_structure.get("page_count", 0),
        "fields": document.parsed_structure.get("fields", []),
    }


@app.post("/api/documents/{document_id}/review")
def approve_document_review(
    document_id: uuid.UUID,
    payload: DocumentReviewSubmit,
    session: Session = Depends(get_db),
) -> dict:
    document = _document_in_workspace(document_id, session)
    if document.status != "needs_review" or not document.parsed_structure:
        raise HTTPException(status_code=409, detail="Este PDF no está esperando revisión")

    candidates = {
        candidate["id"]: candidate
        for candidate in document.parsed_structure.get("fields", [])
        if isinstance(candidate, dict) and candidate.get("id")
    }
    page_count = int(document.parsed_structure.get("page_count", 0))
    approved_fields = []
    for field in payload.fields:
        label = field.label.strip()
        value = field.value.strip()
        key = field.key.strip().lower()
        if not label or not value:
            continue
        if field.candidate_id:
            candidate = candidates.get(field.candidate_id)
            if candidate is None:
                raise HTTPException(status_code=422, detail="Un campo proviene de una extracción desconocida")
            page = candidate.get("page")
            evidence = candidate.get("evidence", "")
            bbox = candidate.get("bbox")
            kind = candidate.get("kind", "canonical")
        else:
            page = field.page
            if page is not None and page > page_count:
                raise HTTPException(status_code=422, detail="La página indicada no existe en este PDF")
            evidence = field.evidence.strip()
            bbox = None
            kind = "custom"
        approved_fields.append(
            {
                "key": key,
                "label": label,
                "value": value,
                "page": page,
                "evidence": evidence,
                "bbox": bbox,
                "kind": kind,
            }
        )

    next_ordinal = session.scalar(
        select(func.max(DocumentChunk.ordinal)).where(DocumentChunk.document_id == document.id)
    ) or 0
    reviewed_at = datetime.now(timezone.utc)
    for field in approved_fields:
        page = field["page"]
        source = f". Evidencia: {field['evidence']}" if field["evidence"] else ""
        content = f"Campo confirmado por revisión: {field['label']}: {field['value']}{source}"
        next_ordinal += 1
        session.add(
            DocumentChunk(
                document_id=document.id,
                ordinal=next_ordinal,
                content=content,
                search_text=" ".join(tokenize(content)),
                line_start=0,
                line_end=0,
                page_start=page,
                page_end=page,
            )
        )

    document.approved_fields = approved_fields
    document.reviewed_at = reviewed_at
    document.status = "ready"
    document.error_message = None
    document.updated_at = reviewed_at
    session.commit()
    session.refresh(document)
    return _document_json(document)


@app.delete("/api/documents/{document_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_document(document_id: uuid.UUID, session: Session = Depends(get_db)) -> None:
    workspace = _workspace(session)
    document = session.scalar(
        select(Document).where(Document.id == document_id, Document.workspace_id == workspace.id)
    )
    if document is None:
        raise HTTPException(status_code=404, detail="No encontré ese documento")
    storage_key = document.storage_key
    session.delete(document)
    session.commit()
    storage.delete(storage_key)


@app.post("/api/documents/{document_id}/retry", status_code=status.HTTP_202_ACCEPTED)
def retry_document(document_id: uuid.UUID, session: Session = Depends(get_db)) -> dict:
    workspace = _workspace(session)
    document = session.scalar(
        select(Document).where(Document.id == document_id, Document.workspace_id == workspace.id)
    )
    if document is None:
        raise HTTPException(status_code=404, detail="No encontré ese documento")
    if document.status != "failed":
        raise HTTPException(status_code=409, detail="Solo se pueden reintentar documentos con error")
    document.status = "queued"
    document.error_message = None
    document.processing_started_at = None
    document.updated_at = datetime.now(timezone.utc)
    session.commit()
    session.refresh(document)
    return _document_json(document)


@app.get("/api/conversations")
def list_conversations(session: Session = Depends(get_db)) -> list[dict]:
    workspace = _workspace(session)
    conversations = session.scalars(
        select(Conversation)
        .where(Conversation.workspace_id == workspace.id)
        .order_by(Conversation.updated_at.desc())
    )
    return [
        {"id": str(conversation.id), "title": conversation.title, "updated_at": conversation.updated_at}
        for conversation in conversations
    ]


@app.post("/api/conversations", status_code=status.HTTP_201_CREATED)
def create_conversation(payload: ConversationCreate, session: Session = Depends(get_db)) -> dict:
    workspace = _workspace(session)
    conversation = Conversation(workspace_id=workspace.id, title=payload.title.strip() or "Nueva conversación")
    session.add(conversation)
    session.commit()
    session.refresh(conversation)
    return {"id": str(conversation.id), "title": conversation.title, "updated_at": conversation.updated_at}


def _get_conversation(conversation_id: uuid.UUID, session: Session) -> Conversation:
    workspace = _workspace(session)
    conversation = session.scalar(
        select(Conversation).where(
            Conversation.id == conversation_id,
            Conversation.workspace_id == workspace.id,
        )
    )
    if conversation is None:
        raise HTTPException(status_code=404, detail="No encontré esa conversación")
    return conversation


@app.get("/api/conversations/{conversation_id}/messages")
def list_messages(conversation_id: uuid.UUID, session: Session = Depends(get_db)) -> list[dict]:
    _get_conversation(conversation_id, session)
    messages = session.scalars(
        select(Message).where(Message.conversation_id == conversation_id).order_by(Message.created_at, Message.id)
    )
    return [_message_json(message) for message in messages]


@app.post("/api/conversations/{conversation_id}/messages", status_code=status.HTTP_201_CREATED)
def send_message(conversation_id: uuid.UUID, payload: MessageCreate, session: Session = Depends(get_db)) -> dict:
    conversation = _get_conversation(conversation_id, session)
    question = payload.content.strip()
    if not question:
        raise HTTPException(status_code=422, detail="La pregunta no puede estar vacía")

    user_message = Message(conversation_id=conversation_id, role="user", content=question, citations=[], retrieved=[])
    session.add(user_message)
    if conversation.title == "Nueva conversación":
        conversation.title = question[:72]
    conversation.updated_at = datetime.now(timezone.utc)
    session.commit()
    session.refresh(user_message)

    hits = retriever.search(session, conversation.workspace_id, question, limit=4)
    evidence = [hit.retrieved for hit in hits]
    generation = provider.generate(question, evidence)
    hits_by_id = {str(hit.retrieved.chunk.chunk_id): hit for hit in hits}

    citations = []
    for chunk_id in generation.cited_chunk_ids:
        hit = hits_by_id.get(chunk_id)
        if hit is None:
            continue
        chunk = hit.retrieved.chunk
        citations.append(
            {
                "document_id": str(hit.document_id),
                "filename": chunk.source,
                "line_start": chunk.line_start,
                "line_end": chunk.line_end,
                "page_start": chunk.page_start,
                "page_end": chunk.page_end,
                "label": chunk.citation,
                "excerpt": chunk.text[:1000],
            }
        )

    retrieved = [
        {
            "document_id": str(hit.document_id),
            "filename": hit.retrieved.chunk.source,
            "page_start": hit.retrieved.chunk.page_start,
            "page_end": hit.retrieved.chunk.page_end,
            "label": hit.retrieved.chunk.citation,
            "score": hit.retrieved.score,
            "coverage": hit.retrieved.coverage,
            "matched_terms": list(hit.retrieved.matched_terms),
            "excerpt": hit.retrieved.chunk.text[:400],
        }
        for hit in hits
    ]
    answer_text = generation.text
    answer_abstained = generation.abstained
    if not answer_abstained and not citations:
        answer_text = "La respuesta no incluyó una cita válida entre los fragmentos recuperados; no puedo mostrarla como respaldada."
        answer_abstained = True
    assistant_message = Message(
        conversation_id=conversation_id,
        role="assistant",
        content=answer_text,
        citations=citations,
        retrieved=retrieved,
        abstained=answer_abstained,
    )
    session.add(assistant_message)
    conversation.updated_at = datetime.now(timezone.utc)
    session.commit()
    session.refresh(assistant_message)
    return {"user_message": _message_json(user_message), "assistant_message": _message_json(assistant_message)}
