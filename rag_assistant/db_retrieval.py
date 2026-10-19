from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.orm import Session

from .models import DocumentChunk as TextChunk
from .models import RetrievedChunk
from .retrieval import tokenize


@dataclass(frozen=True)
class StoredChunkHit:
    retrieved: RetrievedChunk
    document_id: UUID


_SEARCH_SQL = text(
    """
    SELECT c.id, c.document_id, d.filename, c.content, c.line_start, c.line_end,
           c.page_start, c.page_end,
           ts_rank_cd(to_tsvector('simple', c.search_text), q.query) AS score
    FROM document_chunks AS c
    JOIN documents AS d ON d.id = c.document_id
    CROSS JOIN to_tsquery('simple', :query_text) AS q(query)
    WHERE d.workspace_id = :workspace_id
      AND d.status = 'ready'
      AND to_tsvector('simple', c.search_text) @@ q.query
    ORDER BY score DESC, c.document_id, c.ordinal
    LIMIT :limit
    """
)


class PostgresRetriever:
    """PostgreSQL full-text retrieval over independently stored text chunks."""

    def search(self, session: Session, workspace_id: UUID, question: str, limit: int = 4) -> list[StoredChunkHit]:
        terms = tuple(dict.fromkeys(tokenize(question)))
        if not terms or limit <= 0:
            return []

        # tokenize() emits only safe word characters; OR allows partial matches,
        # and prefix matching handles simple suffix variations without embeddings.
        query_text = " | ".join(f"{term}:*" for term in terms)
        rows = session.execute(
            _SEARCH_SQL,
            {"query_text": query_text, "workspace_id": workspace_id, "limit": limit},
        ).mappings()
        hits: list[StoredChunkHit] = []
        for row in rows:
            chunk = TextChunk(
                chunk_id=str(row["id"]),
                source=row["filename"],
                line_start=row["line_start"],
                line_end=row["line_end"],
                text=row["content"],
                page_start=row["page_start"],
                page_end=row["page_end"],
            )
            present = set(tokenize(row["content"]))
            matched = tuple(term for term in terms if term in present)
            hits.append(
                StoredChunkHit(
                    retrieved=RetrievedChunk(
                        chunk=chunk,
                        score=float(row["score"] or 0),
                        matched_terms=matched,
                        coverage=len(matched) / len(terms),
                    ),
                    document_id=row["document_id"],
                )
            )
        return hits
