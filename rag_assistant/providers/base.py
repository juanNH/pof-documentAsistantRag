from typing import Protocol, Sequence

from ..models import Generation, RetrievedChunk


class ModelProvider(Protocol):
    """Contract for a generator that answers from the supplied evidence only."""

    name: str

    def generate(self, question: str, evidence: Sequence[RetrievedChunk]) -> Generation:
        """Return answer text, cited chunk IDs, and an explicit abstention state."""
        ...
