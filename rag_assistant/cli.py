import argparse
from pathlib import Path
import sys

from .evaluation import evaluate, load_cases
from .pipeline import RAGAssistant
from .providers import MockProvider

PROJECT_ROOT = Path(__file__).resolve().parent.parent
EXAMPLE_DOCUMENTS = PROJECT_ROOT / "examples" / "documents"
EXAMPLE_CASES = PROJECT_ROOT / "examples" / "evaluation.json"
PROVIDERS = {"mock": MockProvider}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="rag-assistant",
        description="Asistente RAG local para documentos TXT y Markdown (sin API).",
    )
    commands = parser.add_subparsers(dest="command", required=True)

    ask_parser = commands.add_parser("ask", help="hacer una pregunta a tus documentos")
    ask_parser.add_argument("question", help="pregunta en lenguaje natural")
    ask_parser.add_argument(
        "--documents", type=Path, default=EXAMPLE_DOCUMENTS,
        help="carpeta local con archivos .txt y .md",
    )
    ask_parser.add_argument("--provider", choices=["mock"], default="mock")

    evaluate_parser = commands.add_parser("evaluate", help="evaluar fuente recuperada y abstención")
    evaluate_parser.add_argument("--documents", type=Path, default=EXAMPLE_DOCUMENTS)
    evaluate_parser.add_argument("--cases", type=Path, default=EXAMPLE_CASES)
    evaluate_parser.add_argument("--provider", choices=["mock"], default="mock")
    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    try:
        provider = PROVIDERS[args.provider]()
        assistant = RAGAssistant(args.documents, provider)
        if not assistant.chunks:
            print(f"No encontré archivos .txt o .md con contenido en: {args.documents}", file=sys.stderr)
            raise SystemExit(2)
        if args.command == "ask":
            _print_answer(assistant.ask(args.question))
        else:
            cases = load_cases(args.cases)
            results = evaluate(assistant, cases)
            _print_evaluation(results)
            if not results or not all(
                result.source_correct and result.citation_correct and result.abstention_correct
                for result in results
            ):
                raise SystemExit(1)
    except ValueError as exc:
        parser.error(str(exc))


def _print_answer(answer) -> None:
    print("Modo: mock local (respuesta extractiva; sin servicio de modelo)")
    print(f"\nPregunta: {answer.question}")
    print(f"\nRespuesta: {answer.text}")
    if answer.citations:
        print("\nCitas:")
        for citation in answer.citations:
            print(f"  - {citation}")
    else:
        print("\nCitas: ninguna")
    if answer.abstained:
        print(f"Abstención: sí ({answer.reason})")
    print("\nFragmentos recuperados:")
    if not answer.retrieved:
        print("  - ninguno")
    for item in answer.retrieved:
        terms = ", ".join(item.matched_terms)
        print(
            f"  - {item.chunk.citation} | puntuación {item.score:.2f} | "
            f"cobertura {item.coverage:.2f} | términos: {terms}"
        )


def _print_evaluation(results) -> None:
    print("Evaluación local (fuente esperada y abstención):")
    for result in results:
        source_mark = "OK" if result.source_correct else "FALLO"
        abstention_mark = "OK" if result.abstention_correct else "FALLO"
        citation_mark = "OK" if result.citation_correct else "FALLO"
        actual = result.actual_source or "sin fuente"
        print(
            f"[{source_mark}] fuente {actual} (esperada {result.case.expected_source}) | "
            f"[{citation_mark}] cita {'válida' if result.answer.citations else 'ausente'} | "
            f"[{abstention_mark}] abstención {'sí' if result.answer.abstained else 'no'} "
            f"(esperada {'no' if result.case.answerable else 'sí'}) | {result.case.case_id}"
        )
        print(f"  Pregunta: {result.case.question}")

    source_total = len(results)
    source_correct = sum(result.source_correct for result in results)
    abstention_total = len(results)
    abstention_correct = sum(result.abstention_correct for result in results)
    citation_total = len(results)
    citation_correct = sum(result.citation_correct for result in results)
    if results:
        print(f"\nFuente correcta: {source_correct}/{source_total}")
        print(f"Citas válidas: {citation_correct}/{citation_total}")
        print(f"Decisión de responder/abstenerse correcta: {abstention_correct}/{abstention_total}")


if __name__ == "__main__":
    main()
