# Guía de la aplicación

Esta guía describe qué hace la aplicación hoy, cómo fluye un documento y qué hace realmente la búsqueda. El estado actual es local: React, FastAPI, PostgreSQL, almacenamiento local y un proveedor simulado. No hay proveedor remoto, embeddings ni generación de respuestas con un LLM.

## Arranque y datos locales

Requisitos: Docker Desktop con Compose. Desde la raíz del repositorio:

```powershell
Copy-Item .env.example .env
docker compose up --build
```

Abrí `http://localhost:5173`. Compose levanta cuatro servicios:

| Servicio | Responsabilidad |
| --- | --- |
| `frontend` | React y Vite; expone la interfaz en `127.0.0.1:5173` y reenvía `/api` a FastAPI. |
| `api` | FastAPI; valida cargas, gestiona documentos y conversaciones, acepta revisiones y coordina preguntas. |
| `worker` | Reclama trabajos de indexación de PostgreSQL y procesa archivos fuera de la petición web. |
| `db` | PostgreSQL 16; guarda metadatos, datos revisados, fragmentos, conversaciones y mensajes. |

Los originales se guardan en el volumen `document_uploads`; PostgreSQL vive en `postgres_data`. `docker compose down` detiene los servicios sin borrar los volúmenes. `docker compose down -v` también borra los datos persistidos. `.env.example` documenta límites de archivos, OCR, conexiones de PostgreSQL y frecuencia del worker. No compartas el `.env` de desarrollo como configuración de despliegue.

## Flujo de carga e indexación

1. **React envía el archivo.** `frontend/src/App.tsx` publica un `multipart/form-data` en `POST /api/documents`.
2. **FastAPI valida y registra.** `rag_assistant/web/app.py` valida extensión, tamaño, nombre, formato UTF-8 para texto y firma PDF básica. Calcula SHA-256 para rechazar duplicados, guarda el original por medio de `DocumentStorage` y crea un registro `queued` en PostgreSQL.
3. **El worker reclama el trabajo.** `rag_assistant/web/worker.py` selecciona el trabajo más antiguo con `FOR UPDATE SKIP LOCKED`; así otro worker no procesa la misma fila. Un trabajo `processing` que quedó abandonado puede recuperarse después del tiempo configurado.
4. **Se extrae y divide el contenido.** `.txt` y `.md` se decodifican como UTF-8. Los PDF pasan por `rag_assistant/pdf_ingestion.py`; el worker corta el texto en fragmentos conservando página para poder citarlo.
5. **Se indexa el texto.** Cada fragmento se guarda en `document_chunks`, junto con su contenido, texto para búsqueda y ubicación por página o líneas.
6. **Se establece el estado.** `.txt` y `.md` pasan a `ready`. Los PDF pasan a `needs_review` porque todavía falta confirmar los campos de factura.

Estados visibles:

```text
queued -> processing -> ready
                    -> needs_review -> ready
                    -> failed -> retry -> queued
```

Los documentos con `needs_review`, `queued`, `processing` o `failed` quedan fuera de la búsqueda de preguntas.

## Cómo lee y propone campos de un PDF

`extract_pdf_structure` intenta obtener palabras y coordenadas con `pdfplumber`. `_group_word_lines` agrupa las palabras que aparecen aproximadamente a la misma altura y forma líneas ordenadas de izquierda a derecha. La ubicación de esas líneas permite mostrar la página y evidencia en la revisión.

Si la página contiene menos de 20 caracteres alfanuméricos en su texto extraído, intenta OCR local: PDFium renderiza la página y Tesseract intenta leerla. Los valores predeterminados son 180 DPI, idiomas `spa+eng` y configuración `--psm 6`. El OCR transcribe la imagen; no sabe qué significa una factura.

El extractor aplica tres grupos de reglas:

1. **Campos canónicos:** `FIELD_SPECS` declara nombres estables como `due_date` e idiomas/alias que los pueden expresar. La coincidencia ignora mayúsculas, tildes, espacios y puntuación. El valor se toma del resto de la misma línea hasta otra etiqueta reconocida.
2. **Validación y prioridad:** `_valid_candidate` comprueba patrones sencillos para fechas, períodos, importes e identificadores. `_page_priority` favorece páginas que parecen contener la factura fiscal o el detalle de cargos. Para cada clave se conserva el candidato mejor puntuado. El puntaje ordena candidatos; no es una probabilidad calibrada.
3. **Campos y renglones genéricos:** `_supplier_name_candidates` busca sufijos societarios en las primeras líneas. `_custom_candidates` recoge líneas `etiqueta: valor`. `_line_item_candidates` busca secciones con encabezados conocidos, importes al final de las líneas y palabras clave para categorizar servicios, descuentos e impuestos.

Las reglas no son plantillas entrenadas por proveedor ni reconstruyen tablas de forma general. Fechas e importes se conservan como texto, y los patrones comprueban la forma, no la verdad del dato. Todo PDF requiere revisión humana.

## Revisión y persistencia

La UI muestra el PDF y cada candidato con página y evidencia. La persona puede corregir, quitar o agregar campos; `POST /api/documents/{id}/review` guarda:

- `parsed_structure` (JSONB): candidatos propuestos y su evidencia original;
- `approved_fields` (JSONB): claves, etiquetas y valores confirmados, página, evidencia y tipo;
- fragmentos de texto adicionales con los campos confirmados, incorporados a `document_chunks` para que la búsqueda textual pueda encontrarlos.

Al aprobar, el documento pasa a `ready`. La aprobación no actualiza automáticamente las reglas, no crea datos de entrenamiento y no normaliza importes. En candidatos editados, la API conserva la página y evidencia del candidato original junto con el valor corregido; revisá que la evidencia siga respaldándolo.

## Estructura de PostgreSQL

| Tabla | Contenido actual |
| --- | --- |
| `workspaces` | Espacios de trabajo. La interfaz actual usa un espacio local y aún no tiene autenticación de usuarios. |
| `documents` | Nombre, extensión, clave de almacenamiento, SHA-256, tamaño, estado, `parsed_structure` y `approved_fields`. |
| `document_chunks` | Fragmentos textuales, texto de búsqueda, ordinal y página o líneas de origen. Tiene un índice GIN para búsqueda de texto completo. |
| `conversations` | Conversaciones por espacio de trabajo. |
| `messages` | Preguntas/respuestas, citas, fragmentos recuperados y estado de abstención. |

Los archivos originales no están dentro de PostgreSQL. `DocumentStorage` es una frontera que permite sustituir el almacenamiento local en el futuro por almacenamiento compartido.

## Flujo de una pregunta

1. React publica la pregunta en `POST /api/conversations/{id}/messages`.
2. FastAPI guarda la pregunta y llama a `PostgresRetriever.search`.
3. `tokenize()` pasa la pregunta a minúsculas, quita acentos y palabras comunes. El recuperador crea términos con prefijo unidos por OR; en términos generales, busca fragmentos que contengan cualquiera de ellos.
4. PostgreSQL filtra por `workspace_id` y `documents.status = 'ready'`, calcula `ts_rank_cd` y devuelve hasta cuatro fragmentos.
5. `MockProvider` inspecciona el fragmento mejor puntuado. Con menos de dos términos coincidentes o menos de 0,55 de cobertura, se abstiene. Si supera ambos umbrales, devuelve una oración textual del fragmento.
6. FastAPI acepta citas solo para IDs recuperados, guarda respuesta, citas y candidatos recuperados. React muestra el archivo, la ubicación y el extracto.

La búsqueda usa PostgreSQL Full Text Search: `to_tsvector`, `to_tsquery` e índice GIN. `tsvector` es un tipo de búsqueda de texto de PostgreSQL, no un embedding. No hay pgvector. Tampoco hay generación semántica real: el proveedor simulado ignora el contenido de la pregunta para redactar y reproduce evidencia.

## Endpoints principales

| Endpoint | Uso |
| --- | --- |
| `GET /api/health` | Salud de la API y conexión. |
| `GET/POST /api/documents` | Listar y cargar documentos. |
| `GET /api/documents/{id}/file` | Servir el PDF para vista previa. |
| `GET/POST /api/documents/{id}/review` | Consultar y aprobar candidatos PDF. |
| `POST /api/documents/{id}/retry` | Reencolar un documento fallido. |
| `DELETE /api/documents/{id}` | Borrar el documento y el original local. |
| `GET/POST /api/conversations` | Listar o crear conversaciones. |
| `GET/POST /api/conversations/{id}/messages` | Leer historial o enviar pregunta. |

## Mapa del código

- `frontend/src/App.tsx`: carga, biblioteca, revisión, chat y citas.
- `rag_assistant/web/app.py`: endpoints HTTP y coordinación de revisión, búsqueda y chat.
- `rag_assistant/web/worker.py`: cola, extracción, chunking e indexación.
- `rag_assistant/pdf_ingestion.py`: lectura PDF/OCR y reglas de candidatos.
- `rag_assistant/ingestion.py`: división en fragmentos y referencias de líneas.
- `rag_assistant/db_models.py`: modelos SQLAlchemy y el índice de texto completo.
- `migrations/versions/`: versiones explícitas del esquema de base de datos.
- `rag_assistant/db_retrieval.py`: búsqueda léxica sobre PostgreSQL.
- `rag_assistant/providers/base.py` y `providers/mock.py`: contrato de proveedor y simulador local.
- `rag_assistant/storage.py`: interfaz de almacenamiento y adaptador de disco local.

## Límites actuales

- No hay embeddings ni búsqueda vectorial. Las paráfrasis pueden no encontrar documentos si comparten pocas palabras.
- No hay conversión de preguntas en SQL. La consulta SQL actual busca texto y no calcula comparaciones de facturas.
- No hay tablas relacionales para facturas, renglones, proveedor, servicio, moneda ni período de facturación. Valores aprobados siguen en JSONB/texto.
- La revisión humana no produce métricas de calidad ni ajusta el extractor automáticamente.
- El generador simulado no sintetiza respuestas ni realiza operaciones aritméticas.
- La aplicación no tiene usuarios, autenticación ni permisos entre personas.
