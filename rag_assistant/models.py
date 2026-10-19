from dataclasses import dataclass


@dataclass(frozen=True)
class DocumentChunk:
    """A piece of source text with a stable, human-readable location."""

    chunk_id: str
    source: str
    line_start: int
    line_end: int
    text: str

    @property
    def citation(self) -> str:
        if self.line_start == self.line_end:
            return f"{self.source}:L{self.line_start}"
        return f"{self.source}:L{self.line_start}-L{self.line_end}"


@dataclass(frozen=True)
class RetrievedChunk:
    chunk: DocumentChunk
    score: float
    matched_terms: tuple[str, ...]
    coverage: float


@dataclass(frozen=True)
class Generation:
    text: str
    cited_chunk_ids: tuple[str, ...]
    abstained: bool
    reason: str | None = None


@dataclass(frozen=True)
class Answer:
    question: str
    text: str
    citations: tuple[str, ...]
    abstained: bool
    retrieved: tuple[RetrievedChunk, ...]
    reason: str | None = None
