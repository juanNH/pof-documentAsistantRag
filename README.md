# Asistente RAG para documentos

MVP educativo que responde preguntas usando archivos `.txt` y `.md` locales y muestra de qué archivo y líneas salió cada respuesta. Funciona sin clave de API, proveedores pagos, dependencias externas ni conexión de red.

## Qué problema resuelve

Un modelo de lenguaje puede responder con información inventada o difícil de verificar. Un sistema RAG (generación aumentada por recuperación) busca primero evidencia en los documentos elegidos y luego redacta usando esa evidencia. Las citas permiten volver al texto original; si el sistema no encuentra evidencia suficiente, debe abstenerse.

## MVP en etapas

1. **Leer archivos:** aceptar solo `.txt` y `.md`, en UTF-8, dentro de una carpeta indicada por quien ejecuta el programa.
2. **Preparar evidencia:** dividir el texto en fragmentos acotados y guardar ruta relativa y líneas de origen para poder citar.
3. **Recuperar:** puntuar coincidencias de términos de la pregunta con una fórmula léxica pequeña. No se necesita un modelo de embeddings, base de datos o servicio externo para empezar.
4. **Responder:** pasar la pregunta y los fragmentos recuperados a una interfaz `ModelProvider`. El `MockProvider` del MVP cita una oración literal del mejor fragmento o se abstiene si la coincidencia es débil.
5. **Evaluar:** ejecutar preguntas con fuente esperada y casos sin respuesta conocida. Se informa por separado si recuperó la fuente correcta y si acertó al abstenerse.

Las etapas 1–5 componen el MVP actual. PDF e interfaz gráfica quedan como ampliaciones posibles. Una etapa posterior puede añadir embeddings para recuperar paráfrasis y, si se autoriza, un adaptador a un proveedor real. No hay adaptador real en este proyecto.

## Decisiones y límites

- **Python estándar:** permite estudiar el flujo sin instalar librerías ni configurar servicios.
- **Recuperación léxica con IDF:** premia términos de la pregunta que aparecen en un fragmento y da más peso a términos poco comunes. Es fácil de inspeccionar, pero no entiende sinónimos; embeddings serían una mejora posterior.
- **Fragmentos con líneas de origen:** las respuestas incluyen citas del tipo `archivo.md:L4-L8`, incluso cuando un archivo se divide en varios fragmentos.
- **Proveedor separado del pipeline:** `ModelProvider` describe la frontera entre evidencia y generación. `MockProvider` es local y determinista; no simula una conexión a un modelo externo.
- **Abstención conservadora:** el simulador exige al menos dos términos informativos coincidentes y cobertura mínima. Es una regla didáctica, no un detector semántico de verdad; un modelo real necesitaría instrucciones de grounding y evaluaciones más amplias.
- **Privacidad local:** el programa solo lee la carpeta que se le pasa. No sube ni copia documentos a un servicio.

## Requisitos y uso

Requiere Python 3.10 o posterior. Desde la raíz del proyecto:

```bash
python -m rag_assistant ask --documents examples/documents "¿Qué código HTTP responde la API cuando se supera la cuota?"
python -m rag_assistant evaluate
```

Para preguntar sobre tus propios documentos:

```bash
python -m rag_assistant ask --documents "./mis-documentos" "¿Cuál es la política de devolución?"
```

Solo se recorren archivos `.txt` y `.md`; las subcarpetas se incluyen. Los nombres de archivo y el texto se mantienen locales. Los archivos deben estar codificados en UTF-8.

## Cómo seguir el flujo

1. `rag_assistant/ingestion.py` carga y divide documentos en `DocumentChunk`.
2. `rag_assistant/retrieval.py` calcula puntuaciones y devuelve los fragmentos mejor posicionados.
3. `rag_assistant/pipeline.py` conecta recuperación y generación, y valida que las citas pertenezcan a evidencia recuperada.
4. `rag_assistant/providers/base.py` define el contrato que podría implementar un proveedor futuro.
5. `rag_assistant/providers/mock.py` responde con una cita textual o se abstiene.
6. `rag_assistant/evaluation.py` compara resultados con `examples/evaluation.json`.

## Evaluación incluida

El conjunto de ejemplos comprueba tres respuestas con fuente esperada y una pregunta relacionada con un documento pero sin respuesta explícita. `evaluate` muestra el resultado por pregunta y mide fuente recuperada, cita válida y decisión de responder o abstenerse. Son ejemplos para aprender, no una garantía estadística; conviene añadir preguntas representativas de los documentos reales.

## Extensiones posibles

- **PDF:** incorporar extracción de texto y validar cómo se preservan páginas como citas.
- **GUI:** exponer selección de carpeta, pregunta, respuesta y citas.
- **Recuperación semántica:** comparar embeddings contra la línea base léxica usando el mismo conjunto de evaluación.
- **Proveedor real:** definir configuración de credenciales, tratamiento de privacidad, límites de contexto y pruebas de citas antes de conectar uno. Requiere una decisión explícita; el MVP no lo hace.
