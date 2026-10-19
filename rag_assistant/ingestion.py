from pathlib import Path

from .models import DocumentChunk

SUPPORTED_EXTENSIONS = {".txt", ".md"}


def load_chunks(directory: Path, max_chars: int = 1000) -> list[DocumentChunk]:
    """Read UTF-8 TXT/Markdown files and split them while retaining line numbers."""
    root = directory.expanduser().resolve()
    if not root.exists() or not root.is_dir():
        raise ValueError(f"La carpeta de documentos no existe o no es una carpeta: {directory}")
    if max_chars < 100:
        raise ValueError("max_chars debe ser al menos 100")

    chunks: list[DocumentChunk] = []
    files = sorted(
        (
            path
            for path in root.rglob("*")
            if path.is_file() and path.suffix.lower() in SUPPORTED_EXTENSIONS
        ),
        key=lambda path: path.relative_to(root).as_posix().casefold(),
    )

    for path in files:
        # Do not follow a file symlink outside the folder selected by the user.
        resolved = path.resolve()
        if not resolved.is_relative_to(root):
            continue
        try:
            text = resolved.read_text(encoding="utf-8")
        except UnicodeDecodeError as exc:
            raise ValueError(f"El archivo no está codificado en UTF-8: {path}") from exc
        source = path.relative_to(root).as_posix()
        chunks.extend(_chunk_document(text, source, max_chars))
    return chunks


def _chunk_document(text: str, source: str, max_chars: int) -> list[DocumentChunk]:
    lines = text.splitlines()
    chunks: list[DocumentChunk] = []
    buffer: list[str] = []
    buffer_start = 0
    buffer_end = 0
    buffer_length = 0

    def flush() -> None:
        nonlocal buffer, buffer_start, buffer_end, buffer_length
        content = "\n".join(buffer).strip()
        if content:
            index = len(chunks) + 1
            chunks.append(
                DocumentChunk(
                    chunk_id=f"{source}#chunk-{index:03d}",
                    source=source,
                    line_start=buffer_start,
                    line_end=buffer_end,
                    text=content,
                )
            )
        buffer = []
        buffer_start = 0
        buffer_end = 0
        buffer_length = 0

    for line_number, line in enumerate(lines, start=1):
        if len(line) > max_chars:
            flush()
            remainder = line
            while remainder:
                if len(remainder) <= max_chars:
                    piece, remainder = remainder, ""
                else:
                    split_at = remainder.rfind(" ", 0, max_chars + 1)
                    if split_at < max_chars // 2:
                        split_at = max_chars
                    piece, remainder = remainder[:split_at], remainder[split_at:].lstrip()
                if piece.strip():
                    chunks.append(
                        DocumentChunk(
                            chunk_id=f"{source}#chunk-{len(chunks) + 1:03d}",
                            source=source,
                            line_start=line_number,
                            line_end=line_number,
                            text=piece.strip(),
                        )
                    )
            continue

        added_length = len(line) + (1 if buffer else 0)
        if buffer and buffer_length + added_length > max_chars:
            flush()
            added_length = len(line)
        if not buffer:
            buffer_start = line_number
        buffer.append(line)
        buffer_end = line_number
        buffer_length += added_length

    flush()
    return chunks


def chunk_text(text: str, source: str, max_chars: int = 1000) -> list[DocumentChunk]:
    """Split extracted text while keeping source lines for citations."""
    if max_chars < 100:
        raise ValueError("max_chars debe ser al menos 100")
    return _chunk_document(text, source, max_chars)
