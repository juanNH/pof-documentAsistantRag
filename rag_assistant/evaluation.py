import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .models import Answer
from .pipeline import RAGAssistant


@dataclass(frozen=True)
class EvaluationCase:
    case_id: str
    question: str
    expected_source: str
    answerable: bool


@dataclass(frozen=True)
class EvaluationResult:
    case: EvaluationCase
    answer: Answer
    actual_source: str | None

    @property
    def source_correct(self) -> bool:
        return self.actual_source == self.case.expected_source

    @property
    def abstention_correct(self) -> bool:
        return self.answer.abstained is (not self.case.answerable)

    @property
    def citation_correct(self) -> bool:
        if not self.case.answerable:
            return self.answer.abstained and not self.answer.citations
        return (
            not self.answer.abstained
            and any(citation.startswith(f"{self.case.expected_source}:") for citation in self.answer.citations)
        )


def load_cases(path: Path) -> list[EvaluationCase]:
    try:
        raw: Any = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"No se pudo leer el conjunto de evaluación {path}: {exc}") from exc
    if not isinstance(raw, list):
        raise ValueError("El conjunto de evaluación debe ser una lista JSON")

    cases: list[EvaluationCase] = []
    for index, item in enumerate(raw, start=1):
        if not isinstance(item, dict):
            raise ValueError(f"El caso {index} debe ser un objeto JSON")
        try:
            case = EvaluationCase(
                case_id=str(item["id"]),
                question=str(item["question"]),
                expected_source=str(item["expected_source"]),
                answerable=item["answerable"],
            )
        except KeyError as exc:
            raise ValueError(f"Falta el campo {exc.args[0]!r} en el caso {index}") from exc
        if not case.case_id or not case.question or not case.expected_source:
            raise ValueError(f"El caso {index} debe tener id, pregunta y fuente esperada")
        if not isinstance(case.answerable, bool):
            raise ValueError(f"El campo answerable del caso {index} debe ser booleano")
        cases.append(case)
    return cases


def evaluate(assistant: RAGAssistant, cases: list[EvaluationCase]) -> list[EvaluationResult]:
    results: list[EvaluationResult] = []
    for case in cases:
        answer = assistant.ask(case.question)
        actual_source = answer.retrieved[0].chunk.source if answer.retrieved else None
        results.append(EvaluationResult(case, answer, actual_source))
    return results
