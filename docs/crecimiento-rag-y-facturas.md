# Crecimiento del RAG y consultas de facturas

Este documento separa lo implementado hoy de una arquitectura futura. El objetivo es añadir consultas semánticas y cálculos exactos sin perder la evidencia por página ni enviar documentos privados a un proveedor no autorizado.

## Dos tipos de preguntas requieren dos clases de datos

| Pregunta | Fuente de verdad recomendada | Ejemplo |
| --- | --- | --- |
| Comprensión y explicación | Fragmentos de texto más recuperación textual/vectorial | “¿Qué incluye el servicio?” |
| Hecho puntual aprobado | Columna estructurada y filtro SQL; recuperar la cita del documento | “¿Cuál es el CUIT del emisor?” |
| Cálculo entre facturas | Importes tipados y consultas SQL | “¿Cuánto subió internet entre enero y febrero?” |

RAG significa recuperación seguida de respuesta con evidencia. El recuperador no tiene que ser vectorial: el MVP utiliza coincidencia textual. La búsqueda vectorial sería una mejora de la etapa de recuperación, no un reemplazo para SQL ni para la aritmética.

## Modelo de datos de facturas propuesto

Mantener `documents` como registro y fuente original. Mantener `parsed_structure` para guardar la salida original del extractor y `approved_fields` como registro de lo que la persona confirmó. Agregar tablas relacionales para los datos que deben filtrarse, ordenarse y calcularse:

```text
suppliers
  id, canonical_name, tax_id

services
  id, canonical_name, category

invoices
  id, document_id, supplier_id, invoice_number
  issue_date, due_date, billing_period_start, billing_period_end
  currency, invoice_total NUMERIC, review_status

invoice_line_items
  id, invoice_id, service_id, description_raw, category
  amount NUMERIC, amount_kind, source_page, source_evidence
  approved_at
```

Los nombres y columnas son una propuesta, no parte del esquema actual. Al implementar, cada dato numérico debe mantener su moneda, signo y procedencia. `amount_kind` distingue total, precio base, descuento, impuesto, saldo anterior y pago; esto evita sumar dos veces un resumen y sus renglones o comparar importes con significados distintos. Si proveedor o período no son claros, se pide revisión en lugar de adivinar.

Guardar fechas como `DATE`/`TIMESTAMPTZ` según semántica y montos como `NUMERIC`, nunca como `FLOAT`. Mantener texto original y evidencia junto al valor normalizado permite auditar una consulta y corregir el parser sin perder lo que decía la factura.

## Pregunta a intención y SQL seguro

No conviene permitir que el generador ejecute SQL libre. Separar el reconocimiento de la intención de la ejecución:

1. Clasificar la pregunta a un objeto validado, por ejemplo:

   ```json
   {
     "intent": "compare_invoice_amounts",
     "service": "internet",
     "periods": ["2026-01", "2026-02"],
     "measure": "net_service_total",
     "currency": "ARS"
   }
   ```

2. Validar los nombres de servicio, fechas, moneda y medida contra valores permitidos del sistema.
3. Ejecutar una consulta parametrizada y predefinida sobre `invoices`/`invoice_line_items`, restringida por `workspace_id` y estado aprobado.
4. Comprobar que haya exactamente un resultado comparable por período, o informar ambigüedad.
5. Calcular diferencia y, si corresponde, variación porcentual en SQL o código determinista.
6. Adjuntar a cada resultado el documento, número de página y evidencia aprobada; responder con esas citas.

Una pregunta como “compará cuánto pagué por internet en enero y febrero” necesita resolver qué significa “pagué”: total de factura, precio del servicio después del descuento o monto antes de impuestos. La aplicación debe usar una medida explícita o pedir aclaración. Un LLM puede convertir lenguaje natural a una intención estructurada, pero la API valida esa intención y construye el SQL usando parámetros; no ejecuta texto SQL arbitrario.

## Enrutador de recuperación

Crear un componente independiente, por ejemplo `rag_assistant/question_router.py`, que devuelva un tipo limitado:

```text
exact_lookup       -> filtro SQL exacto y fragmento citador
aggregate_compare  -> SQL tipado y cálculo
semantic_question -> búsqueda vectorial/híbrida y evidencia textual
document_search   -> búsqueda léxica o híbrida
```

Para iniciar, reglas explícitas reconocen intenciones como comparar, sumar, promedio, diferencia y períodos. Una etapa posterior puede usar un clasificador local. El resultado debe ser una estructura validada (Pydantic/enum), no una cadena libre. Si la clasificación no tiene suficiente certeza, usar búsqueda híbrida o pedir una precisión al usuario. Registrar la ruta elegida facilita evaluar falsos enrutamientos.

## Búsqueda vectorial con PostgreSQL

La ruta semántica requiere más que una bandera de configuración:

1. Instalar y habilitar la extensión `pgvector` en PostgreSQL.
2. Seleccionar un modelo de embeddings local o aprobado. El mismo modelo y versión debe usarse para fragmentos y preguntas. Al cambiar el modelo, los fragmentos se deben re-embeder.
3. Agregar una tabla de embeddings versionada, relacionada con `document_chunks`, que guarde `model_id`, dimensión y `embedding vector(n)`. La dimensión depende del modelo.
4. Durante la indexación, el worker calcula y guarda un embedding para cada fragmento. Al consultar, la API calcula un embedding de la pregunta.
5. Buscar los vecinos más cercanos con distancia coseno y aplicar los filtros de workspace y `ready` antes de entregar la evidencia.
6. Para crecer, crear un índice HNSW adecuado a la métrica elegida. pgvector soporta el operador de distancia coseno `<=>` y operadores de índice HNSW; el tamaño de la dimensión y el índice dependen del modelo y volumen.

Esta ruta mejora paráfrasis y sinónimos, pero no garantiza exactitud de identificadores, decimales o aritmética. Una búsqueda semántica puede recuperar dos facturas parecidas; SQL debe seleccionar los períodos y calcular con importes normalizados.

## Búsqueda híbrida

La búsqueda léxica actual es fuerte para nombres exactos, números, etiquetas conocidas y fragmentos con coincidencia literal. La vectorial ayuda cuando la pregunta usa otra expresión. Para consultas descriptivas, recuperar candidatos por ambos caminos y fusionar sus rangos puede ser más estable que sumar directamente `ts_rank_cd` y similitud coseno, cuyas escalas no son equivalentes.

Una estrategia implementable es Reciprocal Rank Fusion (RRF):

```text
score(chunk) = 1 / (k + rank_lexical) + 1 / (k + rank_vector)
```

Recuperar, por ejemplo, candidatos de ambas fuentes y luego conservar el conjunto mejor puntuado, siempre filtrando por espacio de trabajo y documento listo. `k`, los límites de candidatos y cualquier umbral son parámetros de configuración que se deben ajustar con evaluaciones; no son valores universales.

La búsqueda híbrida sigue recuperando texto. La pregunta de diferencia mensual se desvía al camino estructurado porque necesita cálculo, no más fragmentos parecidos.

## Configuración y evaluación previstas

Posibles parámetros futuros, aún no implementados:

```text
QUESTION_ROUTER=rules
RETRIEVAL_MODE=hybrid
LEXICAL_CANDIDATES=20
VECTOR_CANDIDATES=20
FINAL_EVIDENCE_LIMIT=4
VECTOR_MODEL=<modelo aprobado>
VECTOR_METRIC=cosine
RRF_K=60
```

Evaluar por separado antes de cambiar valores:

- **Extracción:** exactitud y cobertura por campo, renglón, proveedor y formato PDF; evidencia y página correctas.
- **Enrutador:** ruta correcta por pregunta; especialmente reconocer qué preguntas requieren datos estructurados.
- **Recuperación:** recall@k/MRR sobre preguntas cuya evidencia correcta esté etiquetada; comparación léxica, vectorial e híbrida con el mismo conjunto.
- **Respuesta:** exactitud aritmética, unidad/moneda, citas que respaldan cada cifra y abstención cuando falta un período o hay ambigüedad.

Usar documentos anonimizados o sintéticos en el conjunto de evaluación y mantener la evaluación local. Las correcciones humanas se pueden comparar con `parsed_structure` para construir ejemplos etiquetados; el sistema actual no aprende de ellas automáticamente. No conectar un proveedor pago o externo sin autorización expresa.

## Etapas sugeridas

1. **Documentar y medir el MVP actual:** preservar casos de evaluación y verificar separación entre parseo, revisión, búsqueda y respuesta.
2. **Normalizar datos aprobados:** migración Alembic para proveedores, facturas y renglones; revisión de unidad, moneda, período, signos y tipo de importe.
3. **Añadir consultas de negocio:** intenciones tipadas, consultas SQL parametrizadas y citas por cada registro usado en un cálculo.
4. **Comparar búsqueda vectorial e híbrida:** modelo local/aprobado, pgvector, embeddings versionados, métricas y evaluación contra FTS actual.
5. **Endurecer el despliegue multiusuario:** autenticación, autorización por workspace, almacenamiento compartido, copias de seguridad, retención, monitoreo y límites.

La base actual ya tiene `workspace_id`, cola de PostgreSQL, abstracción `DocumentStorage`, citas y revisión humana. Esas fronteras permiten escalar partes del sistema sin reemplazar la interfaz y todo el modelo documental a la vez.
