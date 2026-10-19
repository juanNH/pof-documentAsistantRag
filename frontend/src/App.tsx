import { useCallback, useEffect, useRef, useState } from "react";
import type { FormEvent, KeyboardEvent } from "react";

type DocumentItem = {
  id: string;
  filename: string;
  size_bytes: number;
  status: "queued" | "processing" | "needs_review" | "ready" | "failed";
  error_message: string | null;
  created_at: string;
};

type ReviewField = {
  id: string;
  candidate_id: string | null;
  key: string;
  label: string;
  value: string;
  page: number | null;
  evidence: string;
  bbox: number[] | null;
  kind: "canonical" | "custom" | "line_item";
};

type ReviewDocument = {
  id: string;
  filename: string;
  page_count: number;
  fields: ReviewField[];
};

type Citation = {
  document_id: string;
  filename: string;
  line_start: number;
  line_end: number;
  page_start?: number | null;
  page_end?: number | null;
  label: string;
  excerpt: string;
};

type Retrieved = {
  document_id: string;
  filename: string;
  label: string;
  score: number;
  coverage: number;
  matched_terms: string[];
  excerpt: string;
};

type Message = {
  id: string;
  role: "user" | "assistant";
  content: string;
  citations: Citation[];
  retrieved: Retrieved[];
  abstained: boolean | null;
  created_at: string;
};

type Conversation = { id: string; title: string; updated_at: string };

async function api<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(path, init);
  if (!response.ok) {
    let message = `Error ${response.status}`;
    try {
      const body = await response.json();
      message = body.detail ?? message;
    } catch {
      // Keep the status message when the server did not return JSON.
    }
    throw new Error(message);
  }
  if (response.status === 204) return undefined as T;
  return response.json() as Promise<T>;
}

function fileSize(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(0)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

function App() {
  const [documents, setDocuments] = useState<DocumentItem[]>([]);
  const [conversations, setConversations] = useState<Conversation[]>([]);
  const [conversationId, setConversationId] = useState<string | null>(null);
  const [messages, setMessages] = useState<Message[]>([]);
  const [question, setQuestion] = useState("");
  const [loading, setLoading] = useState(false);
  const [uploading, setUploading] = useState(false);
  const [ready, setReady] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [reviewDocument, setReviewDocument] = useState<ReviewDocument | null>(null);
  const [reviewFields, setReviewFields] = useState<ReviewField[]>([]);
  const [reviewPage, setReviewPage] = useState(1);
  const [savingReview, setSavingReview] = useState(false);
  const inputRef = useRef<HTMLInputElement>(null);
  const bottomRef = useRef<HTMLDivElement>(null);

  const refreshDocuments = useCallback(async () => {
    const data = await api<DocumentItem[]>("/api/documents");
    setDocuments(data);
  }, []);

  const refreshConversations = useCallback(async () => {
    const data = await api<Conversation[]>("/api/conversations");
    setConversations(data);
    return data;
  }, []);

  const loadMessages = useCallback(async (id: string) => {
    const data = await api<Message[]>(`/api/conversations/${id}/messages`);
    setMessages(data);
  }, []);

  const createConversation = useCallback(async () => {
    const conversation = await api<Conversation>("/api/conversations", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ title: "Nueva conversación" }),
    });
    setConversations((current) => [conversation, ...current]);
    setConversationId(conversation.id);
    setMessages([]);
  }, []);

  useEffect(() => {
    let active = true;
    async function initialize() {
      try {
        const [, data] = await Promise.all([refreshDocuments(), refreshConversations()]);
        if (!active) return;
        if (data.length > 0) {
          setConversationId(data[0].id);
          await loadMessages(data[0].id);
        } else {
          await createConversation();
        }
      } catch (cause) {
        if (active) setError(cause instanceof Error ? cause.message : "No se pudo conectar con la API.");
      } finally {
        if (active) setReady(true);
      }
    }
    void initialize();
    return () => {
      active = false;
    };
  }, [createConversation, loadMessages, refreshConversations, refreshDocuments]);

  useEffect(() => {
    const timer = window.setInterval(() => {
      void refreshDocuments().catch(() => undefined);
    }, 3000);
    return () => window.clearInterval(timer);
  }, [refreshDocuments]);

  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [messages, loading]);

  async function uploadFiles(files: FileList | null) {
    if (!files?.length) return;
    setUploading(true);
    setError(null);
    const failures: string[] = [];
    try {
      for (const file of Array.from(files)) {
        const form = new FormData();
        form.append("file", file);
        try {
          await api<DocumentItem>("/api/documents", { method: "POST", body: form });
        } catch (cause) {
          failures.push(`${file.name}: ${cause instanceof Error ? cause.message : "error de carga"}`);
        }
      }
      await refreshDocuments();
      if (failures.length) setError(failures.join(" · "));
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "No se pudo actualizar la lista de archivos.");
    } finally {
      setUploading(false);
      if (inputRef.current) inputRef.current.value = "";
    }
  }

  async function removeDocument(document: DocumentItem) {
    if (!window.confirm(`¿Quitar “${document.filename}” y sus fragmentos del espacio?`)) return;
    try {
      await api<void>(`/api/documents/${document.id}`, { method: "DELETE" });
      await refreshDocuments();
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "No se pudo quitar el archivo.");
    }
  }

  async function retryDocument(document: DocumentItem) {
    try {
      await api<DocumentItem>(`/api/documents/${document.id}/retry`, { method: "POST" });
      await refreshDocuments();
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "No se pudo reintentar la carga.");
    }
  }

  async function openReview(document: DocumentItem) {
    setError(null);
    try {
      const review = await api<Omit<ReviewDocument, "fields"> & { fields: Omit<ReviewField, "candidate_id">[] }>(
        `/api/documents/${document.id}/review`,
      );
      const fields = review.fields.map((field) => ({ ...field, candidate_id: field.id }));
      setReviewDocument({ ...review, fields });
      setReviewFields(fields);
      setReviewPage(1);
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "No se pudo abrir la revisión del PDF.");
    }
  }

  function updateReviewField(id: string, updates: Partial<ReviewField>) {
    setReviewFields((current) => current.map((field) => field.id === id ? { ...field, ...updates } : field));
  }

  function addCustomReviewField() {
    const id = `custom-${Date.now()}-${reviewFields.length}`;
    setReviewFields((current) => [
      ...current,
      {
        id,
        candidate_id: null,
        key: `custom_field_${current.length + 1}`,
        label: "Campo adicional",
        value: "",
        page: reviewPage,
        evidence: "",
        bbox: null,
        kind: "custom",
      },
    ]);
  }

  async function approveReview() {
    if (!reviewDocument || savingReview) return;
    const fields = reviewFields
      .filter((field) => field.label.trim() && field.value.trim())
      .map((field) => ({
        candidate_id: field.candidate_id,
        key: field.key || `custom_field_${Date.now()}`,
        label: field.label.trim(),
        value: field.value.trim(),
        page: field.page,
        evidence: field.evidence,
      }));
    setSavingReview(true);
    try {
      await api<DocumentItem>(`/api/documents/${reviewDocument.id}/review`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ fields }),
      });
      setReviewDocument(null);
      setReviewFields([]);
      await refreshDocuments();
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "No se pudo confirmar la extracción.");
    } finally {
      setSavingReview(false);
    }
  }

  async function selectConversation(id: string) {
    setConversationId(id);
    setError(null);
    try {
      await loadMessages(id);
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "No se pudo cargar la conversación.");
    }
  }

  async function sendQuestion(event?: FormEvent) {
    event?.preventDefault();
    const content = question.trim();
    if (!content || !conversationId || loading) return;
    setQuestion("");
    setLoading(true);
    setError(null);
    const temporaryId = `pending-${Date.now()}`;
    const pendingMessage: Message = {
      id: temporaryId,
      role: "user",
      content,
      citations: [],
      retrieved: [],
      abstained: null,
      created_at: new Date().toISOString(),
    };
    setMessages((current) => [...current, pendingMessage]);
    try {
      const result = await api<{ user_message: Message; assistant_message: Message }>(
        `/api/conversations/${conversationId}/messages`,
        {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ content }),
        },
      );
      setMessages((current) => [
        ...current.filter((message) => message.id !== temporaryId),
        result.user_message,
        result.assistant_message,
      ]);
      await refreshConversations();
      await refreshDocuments();
    } catch (cause) {
      setMessages((current) => current.filter((message) => message.id !== temporaryId));
      setQuestion(content);
      setError(cause instanceof Error ? cause.message : "No se pudo enviar la pregunta.");
    } finally {
      setLoading(false);
    }
  }

  function onComposerKeyDown(event: KeyboardEvent<HTMLTextAreaElement>) {
    if (event.key === "Enter" && !event.shiftKey) {
      event.preventDefault();
      void sendQuestion();
    }
  }

  const readyCount = documents.filter((document) => document.status === "ready").length;

  return (
    <div className="app-shell">
      <aside className="sidebar">
        <div className="brand-lockup">
          <div className="brand-mark">a</div>
          <div>
            <div className="brand-name">archivo</div>
            <div className="brand-caption">asistente documental</div>
          </div>
        </div>

        <button className="new-chat-button" onClick={() => void createConversation()}>
          <span className="plus-sign">＋</span> Nueva conversación
        </button>

        <section className="side-section chats-section">
          <div className="section-heading">CONVERSACIONES</div>
          <div className="conversation-list">
            {conversations.map((conversation) => (
              <button
                className={`conversation-item ${conversation.id === conversationId ? "selected" : ""}`}
                key={conversation.id}
                onClick={() => void selectConversation(conversation.id)}
                title={conversation.title}
              >
                <span className="conversation-dot" />
                <span className="conversation-title">{conversation.title}</span>
              </button>
            ))}
            {conversations.length === 0 && <div className="sidebar-empty">Todavía no hay conversaciones.</div>}
          </div>
        </section>

        <section className="side-section document-section">
          <div className="documents-heading-row">
            <div className="section-heading">TUS DOCUMENTOS</div>
            <span className="document-count">{documents.length}</span>
          </div>
          <button
            className="upload-button"
            onClick={() => inputRef.current?.click()}
            disabled={uploading}
          >
            <span className="upload-icon">↑</span>
            {uploading ? "Cargando archivos…" : "Agregar documentos"}
          </button>
          <input
            ref={inputRef}
            className="visually-hidden"
            type="file"
            accept=".txt,.md,.pdf,text/plain,text/markdown,application/pdf"
            multiple
            onChange={(event) => void uploadFiles(event.target.files)}
          />
          <div className="file-type-note">TXT, Markdown y PDF · hasta 15 MB por archivo</div>

          <div className="document-list">
            {documents.map((document) => (
              <div className="document-item" key={document.id}>
                <div className="file-icon">{document.filename.toLowerCase().endsWith(".pdf") ? "P" : document.filename.toLowerCase().endsWith(".md") ? "M" : "T"}</div>
                <div className="document-info">
                  <div className="document-name" title={document.filename}>{document.filename}</div>
                  <div className="document-meta">
                    <span>{fileSize(document.size_bytes)}</span>
                    <span className={`status status-${document.status}`}>
                      {document.status === "queued" && "En cola"}
                      {document.status === "processing" && "Indexando"}
                      {document.status === "needs_review" && "Revisar datos"}
                      {document.status === "ready" && "Disponible"}
                      {document.status === "failed" && "Error"}
                    </span>
                  </div>
                  {document.error_message && (
                    <button className="retry-link" onClick={() => void retryDocument(document)}>
                      Reintentar indexación
                    </button>
                  )}
                  {document.status === "needs_review" && (
                    <button className="retry-link" onClick={() => void openReview(document)}>
                      Revisar campos extraídos
                    </button>
                  )}
                </div>
                <button
                  className="remove-file"
                  aria-label={`Quitar ${document.filename}`}
                  title="Quitar documento"
                  onClick={() => void removeDocument(document)}
                >
                  ×
                </button>
              </div>
            ))}
            {documents.length === 0 && (
              <div className="documents-empty">
                <div className="empty-file-icon">▤</div>
                <span>Tus archivos aparecerán acá</span>
                <span>cuando los agregues.</span>
              </div>
            )}
          </div>
        </section>

        <div className="privacy-note">
          <span className="privacy-dot" />
          <div>
            <strong>Almacenamiento local</strong>
            <span>Los documentos quedan en esta instancia.</span>
          </div>
        </div>
      </aside>

      <main className="main-panel">
        <header className="topbar">
          <div className="topbar-context">
            <span className="topbar-eyebrow">ESPACIO PERSONAL</span>
            <span className="topbar-divider">/</span>
            <span className="topbar-title">Asistente RAG</span>
          </div>
          <div className="mode-pill"><span /> Modo simulado</div>
        </header>

        <div className="chat-scroll-area">
          {!ready ? (
            <div className="loading-state">Conectando con el espacio local…</div>
          ) : messages.length === 0 ? (
            <div className="welcome-state">
              <div className="welcome-orbit"><span className="orbit-core">a</span></div>
              <div className="welcome-kicker">CONSULTA TUS FUENTES</div>
              <h1>¿Qué querés<br />entender hoy?</h1>
              <p>Preguntá sobre tus documentos y revisá el fragmento exacto que respalda cada respuesta.</p>
              {readyCount === 0 ? (
                <button className="welcome-upload" onClick={() => inputRef.current?.click()}>
                  Agregá un archivo para comenzar <span>→</span>
                </button>
              ) : (
                <div className="ready-hint"><span /> {readyCount} {readyCount === 1 ? "documento disponible" : "documentos disponibles"}</div>
              )}
              <div className="suggestion-row">
                <button onClick={() => setQuestion("¿Qué información importante aparece en mis documentos?")}>Resumir lo importante <span>↗</span></button>
                <button onClick={() => setQuestion("¿Qué dicen las fuentes sobre…?")}>Buscar un tema <span>↗</span></button>
              </div>
            </div>
          ) : (
            <div className="message-list">
              <div className="conversation-date">CONVERSACIÓN ACTUAL</div>
              {messages.map((message) => (
                <article className={`message-row message-${message.role}`} key={message.id}>
                  <div className={`avatar avatar-${message.role}`}>{message.role === "user" ? "V" : "a"}</div>
                  <div className="message-content">
                    <div className="message-author">{message.role === "user" ? "Vos" : "Archivo"}</div>
                    <div className="message-text">{message.content}</div>
                    {message.role === "assistant" && message.abstained && (
                      <div className="abstention-note">No encontré evidencia suficiente en los documentos disponibles.</div>
                    )}
                    {message.citations?.length > 0 && (
                      <div className="citations-block">
                        <div className="citations-title"><span>◇</span> FUENTES</div>
                        {message.citations.map((citation, index) => (
                          <details className="citation-card" key={`${citation.document_id}-${citation.label}-${index}`}>
                            <summary>
                              <span className="citation-marker">{index + 1}</span>
                              <span className="citation-filename">{citation.filename}</span>
                              {citation.page_start ? (
                                <a className="citation-location" href={`/api/documents/${citation.document_id}/file#page=${citation.page_start}`} target="_blank" rel="noreferrer">{citation.label.split(":").slice(1).join(":")}</a>
                              ) : (
                                <span className="citation-location">{citation.label.split(":").slice(1).join(":")}</span>
                              )}
                              <span className="citation-chevron">⌄</span>
                            </summary>
                            <blockquote>{citation.excerpt}</blockquote>
                          </details>
                        ))}
                      </div>
                    )}
                    {message.retrieved?.length > 0 && (
                      <details className="retrieval-details">
                        <summary>Ver {message.retrieved.length} fragmentos recuperados y sus puntuaciones</summary>
                        <div className="retrieval-list">
                          {message.retrieved.map((item) => (
                            <div className="retrieval-item" key={item.document_id + item.label}>
                              <div className="retrieval-heading">
                                <span>{item.label}</span>
                                <span>{Math.round(item.coverage * 100)}% de términos</span>
                              </div>
                              <p>{item.excerpt}</p>
                              <small>Coincidencias: {item.matched_terms.join(", ") || "ninguna"}</small>
                            </div>
                          ))}
                        </div>
                      </details>
                    )}
                  </div>
                </article>
              ))}
              {loading && (
                <div className="message-row message-assistant">
                  <div className="avatar avatar-assistant">a</div>
                  <div className="message-content"><div className="message-author">Archivo</div><div className="typing-indicator"><i /><i /><i /></div></div>
                </div>
              )}
              <div ref={bottomRef} />
            </div>
          )}
        </div>

        {error && <div className="error-banner" role="alert"><span>{error}</span><button onClick={() => setError(null)}>×</button></div>}

        <div className="composer-wrap">
          <form className="composer" onSubmit={(event) => void sendQuestion(event)}>
            <textarea
              value={question}
              onChange={(event) => setQuestion(event.target.value)}
              onKeyDown={onComposerKeyDown}
              placeholder="Preguntá sobre los archivos cargados…"
              rows={2}
              aria-label="Tu pregunta"
              disabled={!ready || loading}
            />
            <div className="composer-footer">
              <div className="composer-hint"><span>↵</span> para enviar · <span>Shift + ↵</span> para nueva línea</div>
              <button className="send-button" type="submit" disabled={!question.trim() || loading || !conversationId} aria-label="Enviar pregunta">
                <span>Enviar</span><b>↑</b>
              </button>
            </div>
          </form>
          <div className="disclaimer">Las respuestas del modo simulado citan fragmentos textuales y pueden abstenerse si falta evidencia.</div>
        </div>
      </main>
      {reviewDocument && (
        <div className="review-backdrop" role="presentation" onMouseDown={(event) => {
          if (event.target === event.currentTarget && !savingReview) setReviewDocument(null);
        }}>
          <section className="review-dialog" role="dialog" aria-modal="true" aria-labelledby="review-title">
            <header className="review-header">
              <div>
                <div className="review-eyebrow">REVISIÓN ANTES DE CONSULTAR</div>
                <h2 id="review-title">Verificá los datos de {reviewDocument.filename}</h2>
                <p>Los nombres de proveedores varían. Corregí estos candidatos y agregá los campos propios del servicio si hace falta.</p>
              </div>
              <button className="review-close" onClick={() => setReviewDocument(null)} disabled={savingReview} aria-label="Cerrar revisión">×</button>
            </header>
            <div className="review-body">
              <div className="pdf-preview-panel">
                <div className="pdf-preview-toolbar">
                  <strong>PDF original</strong>
                  <label>Página <input type="number" min={1} max={Math.max(1, reviewDocument.page_count)} value={reviewPage} onChange={(event) => setReviewPage(Math.max(1, Number(event.target.value) || 1))} /></label>
                </div>
                <iframe title={`Vista previa de ${reviewDocument.filename}`} src={`/api/documents/${reviewDocument.id}/file#page=${reviewPage}&zoom=page-width`} />
              </div>
              <div className="review-fields-panel">
                <div className="review-fields-intro">
                  <strong>{reviewFields.length} campos sugeridos</strong>
                  <span>Revisá el valor junto a su texto de origen. Se conservan también los detalles no normalizados para poder consultarlos.</span>
                </div>
                {reviewFields.length === 0 && <div className="review-no-fields">No detecté campos con confianza suficiente. Podés agregarlos manualmente; el texto de las páginas igual quedará disponible.</div>}
                <div className="review-fields-list">
                  {reviewFields.map((field) => (
                    <article className="review-field-card" key={field.id}>
                      <div className="review-field-topline">
                        <label>Campo<input value={field.label} onChange={(event) => updateReviewField(field.id, { label: event.target.value })} /></label>
                        <button className="review-remove-field" onClick={() => setReviewFields((current) => current.filter((item) => item.id !== field.id))} aria-label={`Quitar ${field.label}`}>×</button>
                      </div>
                      <label className="review-value-label">Valor<input value={field.value} onChange={(event) => updateReviewField(field.id, { value: event.target.value })} /></label>
                      {field.evidence && <blockquote className="review-evidence">“{field.evidence}”</blockquote>}
                      <div className="review-field-footer">
                        <span>{!field.candidate_id ? "Agregado manualmente" : field.kind === "line_item" ? "Renglón de cargos detectado" : "Detectado automáticamente"}</span>
                        {field.page && <button onClick={() => setReviewPage(field.page!)}>Ver página {field.page}</button>}
                        {!field.candidate_id && <label>Página<input type="number" min={1} max={Math.max(1, reviewDocument.page_count)} value={field.page ?? ""} onChange={(event) => updateReviewField(field.id, { page: Number(event.target.value) || null })} /></label>}
                      </div>
                    </article>
                  ))}
                </div>
                <button className="review-add-field" onClick={addCustomReviewField}>＋ Agregar campo del proveedor o servicio</button>
              </div>
            </div>
            <footer className="review-actions">
              <span>El PDF solo entra en las búsquedas después de confirmar.</span>
              <div><button className="review-cancel" onClick={() => setReviewDocument(null)} disabled={savingReview}>Dejar pendiente</button><button className="review-approve" onClick={() => void approveReview()} disabled={savingReview}>{savingReview ? "Guardando…" : "Confirmar y habilitar"}</button></div>
            </footer>
          </section>
        </div>
      )}
    </div>
  );
}

export default App;
