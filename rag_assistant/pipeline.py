from pathlib import Path

from .ingestion import load_chunks
from .models import Answer
from .providers.base import ModelProvider
from .retrieval import LexicalRetriever


class RAGAssistant:
    def __init__(self, documents: Path, provider: ModelProvider, top_k: int = 3):
        self.chunks = load_chunks(documents)
        self.retriever = LexicalRetriever(self.chunks)
        self.provider = provider
        self.top_k = top_k

    def ask(self, question: str) -> Answer:
        evidence = self.retriever.search(question, limit=self.top_k)
        generation = self.provider.generate(question, evidence)
        by_id = {item.chunk.chunk_id: item.chunk for item in evidence}
        # A provider may cite only fragments it received. This keeps citations
        # traceable if another provider is added in a future stage.
        cited_chunks = [by_id[chunk_id] for chunk_id in generation.cited_chunk_ids if chunk_id in by_id]
        citations = tuple(dict.fromkeys(chunk.citation for chunk in cited_chunks))
        text = generation.text
        if not generation.abstained and not citations:
            text = "La respuesta no incluyó una cita válida entre los fragmentos recuperados; no puedo mostrarla como respaldada."
            abstained = True
            reason = "missing_valid_citation"
        else:
            abstained = generation.abstained
            reason = generation.reason
        return Answer(
            question=question,
            text=text,
            citations=citations,
            abstained=abstained,
            retrieved=tuple(evidence),
            reason=reason,
        )
