import math
import re
import unicodedata
from collections import Counter

from .models import DocumentChunk, RetrievedChunk

_TOKEN_RE = re.compile(r"[\w]+", re.UNICODE)
_STOP_WORDS = {
    "a", "al", "algo", "ante", "como", "con", "cuando", "cual", "cuales",
    "de", "del", "desde", "donde", "el", "ella", "ellas", "ellos", "en",
    "entre", "es", "esa", "esas", "ese", "eso", "esos", "esta", "estas",
    "este", "esto", "estos", "fue", "ha", "hay", "la", "las", "le", "les",
    "lo", "los", "mas", "me", "mi", "mis", "muy", "no", "o", "para", "por",
    "que", "se", "segun", "ser", "su", "sus", "un", "una", "unas", "uno",
    "unos", "y", "yo", "tu", "tus", "cuanto", "cuanta", "cuantos", "cuantas",
    "debo", "puede", "pueden", "quiero", "necesito", "indica", "significa",
}


def tokenize(text: str) -> list[str]:
    normalized = unicodedata.normalize("NFKD", text.casefold())
    plain = "".join(char for char in normalized if not unicodedata.combining(char))
    return [
        token
        for token in _TOKEN_RE.findall(plain)
        if len(token) > 1 and token not in _STOP_WORDS
    ]


class LexicalRetriever:
    """Small IDF-weighted term coverage baseline; no embeddings or external service."""

    def __init__(self, chunks: list[DocumentChunk]):
        self.chunks = chunks
        self._tokens = [tokenize(chunk.text) for chunk in chunks]
        self._document_frequency: Counter[str] = Counter()
        for tokens in self._tokens:
            self._document_frequency.update(set(tokens))

    def search(self, question: str, limit: int = 3) -> list[RetrievedChunk]:
        query_terms = tuple(dict.fromkeys(tokenize(question)))
        if not query_terms or not self.chunks or limit <= 0:
            return []

        document_count = len(self.chunks)
        weights = {
            term: math.log(1 + (document_count + 1) / (self._document_frequency[term] + 1))
            for term in query_terms
        }
        total_weight = sum(weights.values()) or 1.0
        results: list[RetrievedChunk] = []

        for chunk, tokens in zip(self.chunks, self._tokens):
            present = set(tokens)
            matched = tuple(term for term in query_terms if term in present)
            if not matched:
                continue
            coverage = sum(weights[term] for term in matched) / total_weight
            # A mild frequency bonus helps distinguish repeated evidence without
            # allowing repetition to dominate query-term coverage.
            frequency_bonus = sum(min(tokens.count(term), 3) for term in matched)
            score = coverage + min(frequency_bonus, 6) * 0.005
            results.append(
                RetrievedChunk(
                    chunk=chunk,
                    score=score,
                    matched_terms=matched,
                    coverage=coverage,
                )
            )

        results.sort(key=lambda result: (-result.score, result.chunk.source, result.chunk.line_start))
        return results[:limit]
