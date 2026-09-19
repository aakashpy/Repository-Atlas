const messagesEl = document.getElementById("messages");
const questionEl = document.getElementById("question");
const formEl = document.getElementById("input-form");
const sendBtn = document.getElementById("send-btn");
const retrievalBody = document.getElementById("retrieval-body");

// Client-side conversation state: {role, text} pairs sent back to the
// server each turn (the backend stays stateless), plus the retrieval
// result captured per assistant turn so the side panel can show any
// past turn's chunks, not just the latest.
let history = [];
let turns = []; // { standalone_query, current_chunks, history_chunks }

function addMessage(role, text) {
  const el = document.createElement("div");
  el.className = `msg ${role}`;
  el.textContent = text;
  messagesEl.appendChild(el);
  messagesEl.scrollTop = messagesEl.scrollHeight;
  return el;
}

function addRewriteNote(standaloneQuery) {
  const el = document.createElement("div");
  el.className = "rewrite-note";
  el.textContent = `rewritten as: "${standaloneQuery}"`;
  messagesEl.appendChild(el);
}

function addRetrievalLink(turnIndex) {
  const el = document.createElement("div");
  el.className = "msg-meta";
  el.textContent = "View retrieved sources";
  el.addEventListener("click", () => {
    document.querySelectorAll(".msg-meta.active").forEach((n) => n.classList.remove("active"));
    el.classList.add("active");
    renderRetrieval(turns[turnIndex]);
  });
  messagesEl.appendChild(el);
  return el;
}

function escapeAttr(s) {
  return s.replace(/"/g, "&quot;");
}

function escapeHtml(s) {
  return s.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
}

// Small hand-rolled Markdown-to-HTML renderer -- not a full CommonMark
// implementation, just what the LLM's answers actually use: bold, inline
// code, fenced code blocks, and (possibly nested) bullet/numbered lists.
// Kept dependency-free rather than pulling in a library, matching this
// project's existing plain-JS/no-build-step frontend. All raw text is
// HTML-escaped before any tag is introduced, so nothing in the model's
// output (or a retrieved chunk it quotes) can inject markup.
function inlineFormat(escaped) {
  let s = escaped;
  s = s.replace(/`([^`]+)`/g, "<code>$1</code>");
  s = s.replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>");
  s = s.replace(/(^|[^*\w])\*([^*\n]+)\*(?!\*)/g, "$1<em>$2</em>");
  return s;
}

function renderMarkdown(raw) {
  const lines = raw.replace(/\r\n/g, "\n").split("\n");
  let html = "";
  let paragraphBuf = [];
  const listStack = []; // { indent: number, type: "ul" | "ol" }

  function flushParagraph() {
    if (paragraphBuf.length) {
      html += `<p>${paragraphBuf.join("<br>")}</p>`;
      paragraphBuf = [];
    }
  }

  function closeAllLists() {
    while (listStack.length) {
      const top = listStack.pop();
      html += `</li></${top.type}>`;
    }
  }

  function openOrContinueList(level, type) {
    while (listStack.length && listStack[listStack.length - 1].indent > level) {
      const top = listStack.pop();
      html += `</li></${top.type}>`;
    }
    if (listStack.length && listStack[listStack.length - 1].indent === level) {
      const top = listStack[listStack.length - 1];
      if (top.type === type) {
        html += `</li><li>`;
        return;
      }
      listStack.pop();
      html += `</li></${top.type}>`;
    }
    html += `<${type}><li>`;
    listStack.push({ indent: level, type });
  }

  let i = 0;
  while (i < lines.length) {
    const line = lines[i];

    // Fenced code block -- may be indented (nested under a list item);
    // doesn't touch list/paragraph state so it renders inside whatever
    // list item it appeared under, same as the model intended.
    const fenceOpen = line.match(/^\s*```(\w*)\s*$/);
    if (fenceOpen) {
      flushParagraph();
      const codeLines = [];
      i++;
      while (i < lines.length && !/^\s*```\s*$/.test(lines[i])) {
        codeLines.push(lines[i]);
        i++;
      }
      i++; // skip closing fence
      html += `<pre><code>${escapeHtml(codeLines.join("\n"))}</code></pre>`;
      continue;
    }

    const listMatch = line.match(/^(\s*)([*-]|\d+\.)\s+(.*)$/);
    if (listMatch) {
      flushParagraph();
      const level = Math.floor(listMatch[1].length / 2);
      const type = /\d+\./.test(listMatch[2]) ? "ol" : "ul";
      openOrContinueList(level, type);
      html += inlineFormat(escapeHtml(listMatch[3]));
      i++;
      continue;
    }

    if (line.trim() === "") {
      flushParagraph();
      i++;
      continue;
    }

    // Plain text line -- back to top-level prose, so any open lists end.
    closeAllLists();
    paragraphBuf.push(inlineFormat(escapeHtml(line)));
    i++;
  }

  flushParagraph();
  closeAllLists();
  return html;
}

function chunkCard(chunk, kind) {
  const scoreLabel = chunk.score === 1.0 ? "boosted" : chunk.score.toFixed(3);
  const metaBits = [];
  if (chunk.date) metaBits.push(chunk.date);
  if (chunk.chunk_type) metaBits.push(chunk.chunk_type);
  if (chunk.function_name) metaBits.push(`fn: ${chunk.function_name}`);
  if (chunk.referenced_definition_of) {
    metaBits.push(`↳ referenced definition of ${chunk.referenced_definition_of}`);
  } else if (chunk.symbol_name) {
    metaBits.push(`symbol: ${chunk.symbol_name}`);
  }
  if (chunk.referenced_symbols && chunk.referenced_symbols.length) {
    metaBits.push(`refs: ${chunk.referenced_symbols.join(", ")}`);
  }

  return `
    <div class="chunk">
      <div class="chunk-head">
        <span class="tag ${kind}">${kind === "current" ? "current" : "history"}</span>
        <span class="chunk-file" title="${escapeAttr(chunk.filepath)}">${escapeHtml(chunk.filepath)}</span>
        <span class="chunk-score">score: ${scoreLabel}</span>
      </div>
      ${metaBits.length ? `<div class="chunk-score" style="margin-bottom:6px;">${escapeHtml(metaBits.join(" · "))}</div>` : ""}
      <div class="chunk-text">${escapeHtml(chunk.text.slice(0, 600))}${chunk.text.length > 600 ? "…" : ""}</div>
    </div>
  `;
}

function renderRetrieval(turn) {
  if (!turn) {
    retrievalBody.innerHTML = `<div class="retrieval-empty">Ask a question to see what gets retrieved and sent to the LLM.</div>`;
    return;
  }

  const parts = [];
  parts.push(`
    <div class="standalone-query">
      <strong>Standalone query sent to retrieval</strong>
      ${escapeHtml(turn.standalone_query)}
    </div>
  `);

  parts.push(`<div class="chunk-group-title">Current code (${turn.current_chunks.length})</div>`);
  parts.push(
    turn.current_chunks.length
      ? turn.current_chunks.map((c) => chunkCard(c, "current")).join("")
      : `<div class="retrieval-empty">No current-code chunks retrieved.</div>`
  );

  parts.push(`<div class="chunk-group-title">History (${turn.history_chunks.length})</div>`);
  parts.push(
    turn.history_chunks.length
      ? turn.history_chunks.map((c) => chunkCard(c, "history")).join("")
      : `<div class="retrieval-empty">No history chunks retrieved.</div>`
  );

  retrievalBody.innerHTML = parts.join("");
}

async function sendMessage(question) {
  addMessage("user", question);
  const pending = addMessage("assistant", "Thinking…");
  pending.classList.add("pending");

  sendBtn.disabled = true;
  questionEl.disabled = true;

  try {
    const res = await fetch("/api/chat", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ message: question, history }),
    });

    if (!res.ok) {
      const errText = await res.text();
      throw new Error(`Server error ${res.status}: ${errText}`);
    }

    const data = await res.json();

    pending.classList.remove("pending");
    pending.innerHTML = renderMarkdown(data.answer);

    if (data.standalone_query && data.standalone_query !== question) {
      addRewriteNote(data.standalone_query);
    }

    const turnIndex = turns.length;
    turns.push({
      standalone_query: data.standalone_query,
      current_chunks: data.current_chunks,
      history_chunks: data.history_chunks,
    });

    document.querySelectorAll(".msg-meta.active").forEach((n) => n.classList.remove("active"));
    const link = addRetrievalLink(turnIndex);
    link.classList.add("active");
    renderRetrieval(turns[turnIndex]);

    history.push({ role: "user", text: question });
    history.push({ role: "assistant", text: data.answer });
  } catch (err) {
    pending.classList.remove("pending");
    pending.textContent = `Error: ${err.message}`;
  } finally {
    sendBtn.disabled = false;
    questionEl.disabled = false;
    questionEl.focus();
  }
}

formEl.addEventListener("submit", (e) => {
  e.preventDefault();
  const question = questionEl.value.trim();
  if (!question) return;
  questionEl.value = "";
  sendMessage(question);
});

questionEl.addEventListener("keydown", (e) => {
  if (e.key === "Enter" && !e.shiftKey) {
    e.preventDefault();
    formEl.requestSubmit();
  }
});

renderRetrieval(null);
