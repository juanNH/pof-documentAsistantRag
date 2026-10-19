"""Local PDF text/OCR extraction and evidence-linked invoice suggestions."""

from io import BytesIO
import os
import re
import unicodedata
import uuid
from typing import Any

import pdfplumber
import pypdfium2 as pdfium
from pytesseract import Output
import pytesseract


MAX_PDF_PAGES = int(os.environ.get("MAX_PDF_PAGES", "50"))
OCR_DPI = int(os.environ.get("PDF_OCR_DPI", "180"))
OCR_LANGUAGES = os.environ.get("OCR_LANGUAGES", "spa+eng")

# These are document concepts, not provider templates. Each alias is matched
# independent of accents, spaces, and punctuation because many PDFs position
# individual characters without storing natural spaces in the text layer.
FIELD_SPECS: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    ("supplier", "Proveedor / emisor", ("proveedor", "emisor", "supplier", "seller")),
    (
        "supplier_tax_id",
        "Identificación fiscal del emisor",
        ("cuit del emisor", "cuit emisor", "cuit proveedor", "supplier tax id", "seller tax id", "cuit"),
    ),
    ("recipient", "Cliente / receptor", ("receptor", "cliente", "destinatario", "customer", "bill to", "buyer")),
    (
        "recipient_tax_id",
        "Identificación fiscal del cliente",
        ("cuit del cliente", "cuit del receptor", "cuit cliente", "cuit receptor", "customer tax id", "buyer tax id"),
    ),
    (
        "invoice_number",
        "Número de factura",
        ("numero de factura", "factura numero", "factura n", "nro factura", "invoice number", "invoice no", "comprobante"),
    ),
    (
        "issue_date",
        "Fecha de emisión",
        ("fecha de emision", "fecha emision", "issue date", "invoice date", "date issued"),
    ),
    (
        "due_date",
        "Fecha de vencimiento",
        ("fecha de vencimiento", "due date", "payment due", "vence el dia", "vencimiento"),
    ),
    (
        "next_due_date",
        "Próximo vencimiento estimado",
        ("proximo vencimiento estimado", "next estimated due date"),
    ),
    (
        "subscription_period",
        "Período de abono / facturación",
        ("periodo de abono", "periodo facturado", "billing period", "service period"),
    ),
    (
        "consumption_period",
        "Período de consumo",
        ("periodo de consumo", "periodo de uso", "consumption period"),
    ),
    (
        "payment_method",
        "Método de pago",
        ("metodo de pago", "forma de pago", "payment method", "payment terms"),
    ),
    (
        "amount_due",
        "Importe a pagar",
        ("total a pagar", "importe a pagar", "saldo total es de", "amount due", "balance due"),
    ),
    (
        "services_total",
        "Total de servicios del período",
        ("total de servicios del mes", "total de servicios", "total de cargos del mes", "current period total"),
    ),
    ("previous_balance", "Saldo anterior", ("saldo anterior", "previous balance", "prior balance")),
    (
        "payments_applied",
        "Pagos aplicados",
        ("pagos aplicados", "pagos al", "payment received", "payments applied"),
    ),
    (
        "taxes",
        "Impuestos informados",
        ("iva total contenido", "iva total", "total impuestos", "taxes total", "tax total"),
    ),
)

_AMOUNT_KEYS = {"amount_due", "services_total", "previous_balance", "payments_applied", "taxes"}
_DATE_KEYS = {"issue_date", "due_date", "next_due_date"}
_PERIOD_KEYS = {"subscription_period", "consumption_period"}
_AMOUNT_AT_END = re.compile(
    r"(?P<amount>(?:-\s*)?(?:\$\s*)?\d{1,3}(?:[.,]\d{3})*(?:[.,]\d{2})|(?:-\s*)?\d+[.,]\d{2})\s*$"
)
_AMOUNT_ANYWHERE = re.compile(
    r"(?P<amount>-?\s*\$?\s*\d{1,3}(?:[.,]\d{3})*(?:[.,]\d{2})|-?\s*\$?\s*\d+[.,]\d{2})"
)
_DATE_PATTERN = re.compile(r"(?<!\d)\d{1,2}[./-]\d{1,2}(?:[./-]\d{2,4})?(?!\d)")


def _normalize(value: str) -> str:
    decomposed = unicodedata.normalize("NFKD", value.casefold())
    without_marks = "".join(char for char in decomposed if not unicodedata.combining(char))
    return re.sub(r"\s+", " ", without_marks).strip()


def _compact_with_spans(value: str) -> tuple[str, list[tuple[int, int]]]:
    compact: list[str] = []
    spans: list[tuple[int, int]] = []
    for index, char in enumerate(value):
        normalized = _normalize(char)
        for normalized_char in normalized:
            if normalized_char.isalnum():
                compact.append(normalized_char)
                spans.append((index, index + 1))
    return "".join(compact), spans


def _compact(value: str) -> str:
    return _compact_with_spans(value)[0]


def _make_line(text: str, bbox: tuple[float, float, float, float] | None) -> dict[str, Any]:
    return {
        "text": text.strip(),
        "bbox": [round(value, 2) for value in bbox] if bbox else None,
    }


def _group_word_lines(words: list[dict[str, Any]]) -> list[dict[str, Any]]:
    lines: list[dict[str, Any]] = []
    for word in sorted(words, key=lambda item: (float(item.get("top", 0)), float(item.get("x0", 0)))):
        text = str(word.get("text", "")).strip()
        if not text:
            continue
        top = float(word.get("top", 0))
        target = next((line for line in reversed(lines) if abs(top - line["top"]) <= 3.5), None)
        if target is None:
            target = {"top": top, "words": []}
            lines.append(target)
        target["words"].append(word)

    result = []
    for line in lines:
        parts = sorted(line["words"], key=lambda item: float(item.get("x0", 0)))
        bbox = (
            min(float(word["x0"]) for word in parts),
            min(float(word["top"]) for word in parts),
            max(float(word["x1"]) for word in parts),
            max(float(word["bottom"]) for word in parts),
        )
        result.append(_make_line(" ".join(str(word["text"]) for word in parts), bbox))
    return result


def _ocr_page(
    content: bytes,
    page_number: int,
    page_width: float,
    page_height: float,
) -> list[dict[str, Any]]:
    pdf = pdfium.PdfDocument(content)
    bitmap = None
    try:
        page = pdf[page_number - 1]
        bitmap = page.render(scale=OCR_DPI / 72)
        image = bitmap.to_pil()
        data = pytesseract.image_to_data(
            image,
            lang=OCR_LANGUAGES,
            config="--psm 6",
            output_type=Output.DICT,
        )
        scale_x = page_width / image.width
        scale_y = page_height / image.height
    finally:
        if bitmap is not None:
            bitmap.close()
        pdf.close()
    groups: dict[tuple[int, int, int], list[int]] = {}
    for index, raw_text in enumerate(data["text"]):
        if str(raw_text).strip() and float(data["conf"][index]) >= 0:
            key = (int(data["block_num"][index]), int(data["par_num"][index]), int(data["line_num"][index]))
            groups.setdefault(key, []).append(index)

    lines = []
    for indices in groups.values():
        indices.sort(key=lambda index: int(data["left"][index]))
        text = " ".join(str(data["text"][index]).strip() for index in indices).strip()
        bbox = (
            min(int(data["left"][index]) for index in indices) * scale_x,
            min(int(data["top"][index]) for index in indices) * scale_y,
            max(int(data["left"][index]) + int(data["width"][index]) for index in indices) * scale_x,
            max(int(data["top"][index]) + int(data["height"][index]) for index in indices) * scale_y,
        )
        if text:
            lines.append(_make_line(text, bbox))
    return sorted(lines, key=lambda line: (line["bbox"][1], line["bbox"][0]))


def _alias_matches(text: str, aliases: tuple[str, ...]) -> list[tuple[int, int, str]]:
    compact_text, spans = _compact_with_spans(text)
    matches: list[tuple[int, int, str]] = []
    for alias in sorted(aliases, key=lambda item: len(_compact(item)), reverse=True):
        needle = _compact(alias)
        if not needle:
            continue
        start_at = 0
        while True:
            index = compact_text.find(needle, start_at)
            if index < 0:
                break
            start = spans[index][0]
            end = spans[index + len(needle) - 1][1]
            # Avoid matching "vencimiento" inside "próximo vencimiento" and
            # other aliases embedded in unrelated words.
            left_is_word = start > 0 and text[start - 1].isalnum()
            right_is_word = end < len(text) and text[end].isalnum()
            if not left_is_word and not right_is_word:
                matches.append((start, end, alias))
            start_at = index + 1
    return sorted(matches, key=lambda item: (item[0], -(item[1] - item[0])))


def _all_label_matches(text: str) -> list[tuple[int, int, str, str]]:
    matches: list[tuple[int, int, str, str]] = []
    for key, _label, aliases in FIELD_SPECS:
        for start, end, alias in _alias_matches(text, aliases):
            matches.append((start, end, key, alias))
    return sorted(matches, key=lambda item: (item[0], -(item[1] - item[0])))


def _value_after_match(text: str, match: tuple[int, int, str, str]) -> str:
    _start, end, key, _alias = match
    next_start = min(
        (start for start, _next_end, next_key, _next_alias in _all_label_matches(text) if start >= end and next_key != key),
        default=len(text),
    )
    value = text[end:next_start].strip(" \t\r\n:#-–/|")
    # A label often sits after unrelated text in a two-column invoice line.
    # Cut away preceding columns when they remain between the label and value.
    return value[:500].strip()


def _valid_candidate(key: str, value: str) -> bool:
    if not value or not re.search(r"\w", value, flags=re.UNICODE):
        return False
    if key in _AMOUNT_KEYS:
        amount_match = _AMOUNT_AT_END.search(value)
        if amount_match is None:
            amount_match = re.search(r"-?\s*\$?\s*\d[\d.,]*[.,]\d{2}", value)
        return amount_match is not None and not set(value.strip()) <= {"*", "_", "-", "."}
    if key in _DATE_KEYS:
        return _DATE_PATTERN.search(value) is not None
    if key in _PERIOD_KEYS:
        return len(_DATE_PATTERN.findall(value)) >= 2
    if key == "invoice_number":
        return sum(char.isdigit() for char in value) >= 5
    if key == "supplier_tax_id":
        return sum(char.isdigit() for char in value) >= 8
    return True


def _clean_candidate_value(key: str, value: str) -> str:
    if key in _AMOUNT_KEYS:
        match = _AMOUNT_ANYWHERE.search(value)
        return match.group("amount").strip() if match else value.strip()
    if key == "invoice_number":
        cleaned = value.strip(" \t\r\n:#-–/|°º")
        tokens = re.findall(r"[A-Za-z0-9][A-Za-z0-9./-]*", cleaned)
        for token in tokens:
            if sum(char.isdigit() for char in token) >= 5:
                return token
    return value.strip()


def _page_priority(page: dict[str, Any]) -> int:
    page_text = _compact(" ".join(line["text"] for line in page["lines"]))
    score = 0
    if "cargosdelmes" in page_text or "conceptosfacturados" in page_text:
        score += 4
    if "facturan" in page_text and ("cuit" in page_text or "iva" in page_text):
        score += 4
    if "resumendeservicios" in page_text:
        score -= 1
    return score


def _canonical_candidates(pages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    options_by_key: dict[str, list[tuple[int, dict[str, Any]]]] = {}
    for page in pages:
        page_priority = _page_priority(page)
        for line in page["lines"]:
            text = line["text"]
            if not text:
                continue
            for key, label, aliases in FIELD_SPECS:
                matches = _alias_matches(text, aliases)
                if not matches:
                    continue
                # Prefer the most specific label on a line (for example,
                # "próximo vencimiento estimado" over "vencimiento").
                match = max(matches, key=lambda item: len(_compact(item[2])))
                value = _clean_candidate_value(
                    key,
                    _value_after_match(text, (match[0], match[1], key, match[2])),
                )
                if not _valid_candidate(key, value):
                    continue
                specificity = len(_compact(match[2]))
                score = page_priority * 100 + specificity
                candidate = {
                    "id": uuid.uuid4().hex,
                    "key": key,
                    "label": label,
                    "value": value,
                    "page": page["page_number"],
                    "evidence": text[:800],
                    "bbox": line["bbox"],
                    "kind": "canonical",
                }
                options_by_key.setdefault(key, []).append((score, candidate))

    chosen = []
    for options in options_by_key.values():
        options.sort(key=lambda item: (item[0], len(item[1]["evidence"])), reverse=True)
        chosen.append(options[0][1])
    field_order = {key: index for index, (key, _label, _aliases) in enumerate(FIELD_SPECS)}
    return sorted(chosen, key=lambda field: field_order.get(field["key"], len(field_order)))


def _supplier_name_candidates(pages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    legal_suffix = re.compile(r"\b(?:S\.?\s*A\.?|S\.?\s*R\.?\s*L\.?|S\.?\s*A\.?\s*S\.?|LLC|INC\.?|LTD\.?)\b", re.IGNORECASE)
    candidates = []
    for page in pages:
        for line in page["lines"][:14]:
            text = line["text"].strip()
            match = legal_suffix.search(text)
            if match is None:
                continue
            name = text[:match.end()].strip(" ,.-")
            if len(name) < 4 or len(name) > 120 or sum(char.isalpha() for char in name) < 4:
                continue
            candidates.append(
                {
                    "id": uuid.uuid4().hex,
                    "key": "supplier",
                    "label": "Proveedor / emisor",
                    "value": name,
                    "page": page["page_number"],
                    "evidence": text[:800],
                    "bbox": line["bbox"],
                    "kind": "canonical",
                    "_score": _page_priority(page) * 100 + 25,
                }
            )
    return candidates


def _charge_category(description: str) -> str:
    normalized = _compact(description)
    if any(term in normalized for term in ("descuento", "bonificacion", "bonif", "rebate")):
        return "discount"
    if any(term in normalized for term in ("impuesto", "iva", "tax")):
        return "tax"
    if any(term in normalized for term in ("internet", "fibra", "bandaancha", "broadband")):
        return "internet"
    if any(term in normalized for term in ("telefon", "lineamovil", "celular", "mobile")):
        return "telephony"
    if any(term in normalized for term in ("electricidad", "energia", "electricity", "kwh")):
        return "electricity"
    if "gas" in normalized:
        return "gas"
    if any(term in normalized for term in ("agua", "water")):
        return "water"
    if any(term in normalized for term in ("seguro", "poliza", "insurance", "premium")):
        return "insurance"
    return "other"


def _line_item_candidates(pages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    starts = (
        "cargos del mes",
        "detalle de cargos",
        "detalle de consumo",
        "detalle de facturacion",
        "conceptos facturados",
        "services billed",
        "charges for this period",
    )
    ends = (
        "total de servicios",
        "total de cargos",
        "total a pagar",
        "importe total",
        "son pesos",
    )
    candidates: list[dict[str, Any]] = []
    for page in pages:
        active = False
        rows: list[dict[str, Any]] = []
        for line in page["lines"]:
            text = line["text"]
            normalized = _compact(text)
            if any(_compact(start) in normalized for start in starts):
                active = True
                continue
            if not active:
                continue
            if any(_compact(end) in normalized for end in ends):
                active = False
                continue
            amount_match = _AMOUNT_AT_END.search(text)
            if amount_match is None:
                continue
            description = text[:amount_match.start()].strip(" \t:-–|.")
            amount = amount_match.group("amount").strip()
            if len(description) < 2 or not _valid_candidate("taxes", amount):
                continue
            category = _charge_category(description)
            rows.append({
                "category": category,
                "description": description,
                "amount": amount,
                "line": line,
            })

        for index, row in enumerate(rows):
            category = row["category"]
            description = row["description"]
            same_description = [
                other_index
                for other_index, other in enumerate(rows)
                if _compact(other["description"]) == _compact(description)
            ]
            suffix = ""
            if len(same_description) > 1:
                later_discount = any(item["category"] == "discount" for item in rows[index + 1 :])
                if index == same_description[0] and later_discount:
                    suffix = " (resumen del servicio)"
                elif index == same_description[1] and later_discount:
                    suffix = " (precio base)"
                else:
                    suffix = f" (renglón {same_description.index(index) + 1})"
            line = row["line"]
            candidates.append(
                {
                    "id": uuid.uuid4().hex,
                    "key": f"line_item_{category}",
                    "label": f"{category.replace('_', ' ').title()}: {description}{suffix}"[:120],
                    "value": row["amount"],
                    "page": page["page_number"],
                    "evidence": line["text"][:800],
                    "bbox": line["bbox"],
                    "kind": "line_item",
                }
            )
            if len(candidates) >= 60:
                return candidates
    return candidates


def _custom_candidates(pages: list[dict[str, Any]], canonical_lines: set[tuple[int, str]]) -> list[dict[str, Any]]:
    candidates: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for page in pages:
        for line in page["lines"]:
            text = line["text"]
            if (page["page_number"], text) in canonical_lines:
                continue
            if ":" not in text and "：" not in text:
                continue
            label, value = re.split("[:：]", text, maxsplit=1)
            label = label.strip(" \t-–|.")
            value = value.strip()
            if not value or not 2 <= len(label) <= 60 or len(value) > 500:
                continue
            if len(label.split()) > 7 or sum(char.isalpha() for char in label) < 3:
                continue
            slug = re.sub(r"[^a-z0-9]+", "_", _normalize(label)).strip("_")[:48] or "campo"
            dedupe_key = (slug, _normalize(value))
            if dedupe_key in seen:
                continue
            seen.add(dedupe_key)
            candidates.append(
                {
                    "id": uuid.uuid4().hex,
                    "key": f"custom_{slug}",
                    "label": label,
                    "value": value[:500],
                    "page": page["page_number"],
                    "evidence": text[:800],
                    "bbox": line["bbox"],
                    "kind": "custom",
                }
            )
            if len(candidates) >= 60:
                return candidates
    return candidates


def _candidate_fields(pages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    canonical = _canonical_candidates(pages)
    supplier_options = _supplier_name_candidates(pages)
    existing_supplier = next((field for field in canonical if field["key"] == "supplier"), None)
    if existing_supplier is None and supplier_options:
        supplier_options.sort(key=lambda item: item.pop("_score"), reverse=True)
        canonical.insert(0, supplier_options[0])

    canonical_lines = {
        (field["page"], field["evidence"])
        for field in canonical
    }
    return (canonical + _line_item_candidates(pages) + _custom_candidates(pages, canonical_lines))[:100]


def extract_pdf_structure(content: bytes) -> dict[str, Any]:
    """Extract PDF text and evidence-backed field candidates without vendor templates."""
    try:
        pdf = pdfplumber.open(BytesIO(content))
    except Exception as exc:
        raise ValueError("No pude abrir el PDF. Revisá que no esté dañado o protegido con contraseña.") from exc

    with pdf:
        if not pdf.pages:
            raise ValueError("El PDF no contiene páginas.")
        if len(pdf.pages) > MAX_PDF_PAGES:
            raise ValueError(f"El PDF supera el máximo de {MAX_PDF_PAGES} páginas.")

        pages: list[dict[str, Any]] = []
        for page_number, page in enumerate(pdf.pages, start=1):
            # A small tolerance preserves spaces between text runs in PDFs that
            # draw characters separately; the default merges labels together.
            words = page.extract_words(x_tolerance=1.0, keep_blank_chars=False) or []
            lines = _group_word_lines(words)
            extracted_text = "\n".join(line["text"] for line in lines).strip()
            method = "pdf_text"
            if len(re.findall(r"[\w]", extracted_text, flags=re.UNICODE)) < 20:
                try:
                    lines = _ocr_page(content, page_number, float(page.width), float(page.height))
                except Exception as exc:
                    raise ValueError(
                        "Falló el OCR local. Verificá que Tesseract y el idioma español estén instalados."
                    ) from exc
                method = "ocr" if lines else "empty"
            text = "\n".join(line["text"] for line in lines).strip()
            pages.append(
                {
                    "page_number": page_number,
                    "width": round(float(page.width), 2),
                    "height": round(float(page.height), 2),
                    "method": method,
                    "text": text,
                    "lines": lines,
                }
            )

    full_text = "\n\n".join(page["text"] for page in pages if page["text"]).strip()
    if not full_text:
        raise ValueError("No pude extraer texto del PDF, ni siquiera con OCR.")
    return {
        "schema_version": 2,
        "format": "pdf",
        "page_count": len(pages),
        "character_count": len(full_text),
        "pages": pages,
        "fields": _candidate_fields(pages),
    }
