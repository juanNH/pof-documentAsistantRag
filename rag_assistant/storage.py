import os
from pathlib import Path
import uuid
from typing import Protocol


class DocumentStorage(Protocol):
    """Storage boundary for original files; replaceable with object storage later."""

    def put(self, document_id: uuid.UUID, extension: str, content: bytes) -> str: ...

    def get(self, storage_key: str) -> bytes: ...

    def delete(self, storage_key: str) -> None: ...


class LocalDocumentStorage:
    def __init__(self, root: Path | None = None):
        self.root = (root or Path(os.environ.get("DOCUMENT_STORAGE_PATH", "./data/uploads"))).expanduser().resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def put(self, document_id: uuid.UUID, extension: str, content: bytes) -> str:
        if extension not in {".txt", ".md", ".pdf"}:
            raise ValueError("Extensión no admitida")
        key = f"{document_id.hex}{extension}"
        final_path = self._path(key)
        temporary_path = final_path.with_suffix(final_path.suffix + ".tmp")
        try:
            temporary_path.write_bytes(content)
            os.replace(temporary_path, final_path)
        finally:
            temporary_path.unlink(missing_ok=True)
        return key

    def get(self, storage_key: str) -> bytes:
        return self._path(storage_key).read_bytes()

    def delete(self, storage_key: str) -> None:
        self._path(storage_key).unlink(missing_ok=True)

    def _path(self, storage_key: str) -> Path:
        candidate = (self.root / storage_key).resolve()
        if Path(storage_key).name != storage_key or not candidate.is_relative_to(self.root):
            raise ValueError("Ruta de almacenamiento inválida")
        return candidate
