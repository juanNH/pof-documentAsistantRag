import re
from typing import Sequence

from ..models import Generation, RetrievedChunk
from ..retrieval import tokenize


class MockProvider:
    """Deterministic local provider that quotes retrieved evidence or abstains."""

    name = "mock"

    def __init__(self, min_matched_terms: int = 2, min_coverage: float = 0.55):
        self.min_matched_terms = min_matched_terms
        self.min_coverage = min_coverage

    def generate(self, question: str, evidence: Sequence[RetrievedChunk]) -> Generation:
        del question  # The mock deliberately does not synthesize facts from the question.
        if not evidence:
            return Generation(
                text="No encontré fragmentos que aporten evidencia para responder.",
                cited_chunk_ids=(),
                abstained=True,
                reason="no_retrieved_evidence",
            )

        best = evidence[0]
        if len(best.matched_terms) < self.min_matched_terms or best.coverage < self.min_coverage:
            return Generation(
                text="La evidencia recuperada no alcanza para responder con confianza.",
                cited_chunk_ids=(),
                abstained=True,
                reason="insufficient_term_coverage",
            )

        sentence = _best_supported_sentence(best.chunk.text, set(best.matched_terms))
        return Generation(
            text=f"El fragmento más relevante dice: «{sentence}»",
            cited_chunk_ids=(best.chunk.chunk_id,),
            abstained=False,
        )


def _best_supported_sentence(text: str, matched_terms: set[str]) -> str:
    sentences = [part.strip() for part in re.split(r"(?<=[.!?])\s+|\n+", text) if part.strip()]
    if not sentences:
        return text.strip()
    return max(
        sentences,
        key=lambda sentence: len(set(tokenize(sentence)) & matched_terms),
    )[:500]
