const messagesEl = document.getElementById("messages");
const form = document.getElementById("chat-form");
const input = document.getElementById("input");
const sendBtn = document.getElementById("send-btn");
const cancelBtn = document.getElementById("cancel-btn");
const statusEl = document.getElementById("status");

let chatHistory = [];
let generating = false;
let abortController = null;
const MAX_HISTORY = 4; // 최근 2턴(user+assistant x2)만 유지, 프롬프트 크기 제한

// Configure marked
marked.setOptions({
  highlight: (code, lang) => {
    if (lang && hljs.getLanguage(lang)) {
      return hljs.highlight(code, { language: lang }).value;
    }
    return hljs.highlightAuto(code).value;
  },
  breaks: true,
});

// Auto-resize textarea
input.addEventListener("input", () => {
  input.style.height = "auto";
  input.style.height = Math.min(input.scrollHeight, 120) + "px";
});

// Shift+Enter for newline, Enter to send
input.addEventListener("keydown", (e) => {
  if (e.key === "Enter" && !e.shiftKey) {
    e.preventDefault();
    handleSend();
  }
});

// Health check
async function checkHealth() {
  try {
    const res = await fetch("api/health");
    const data = await res.json();
    if (data.model_ready) {
      statusEl.textContent = data.backend || "online";
      statusEl.className = "status online";
    } else if (data.ollama) {
      statusEl.textContent = "loading";
      statusEl.className = "status loading";
    } else {
      statusEl.textContent = "offline";
      statusEl.className = "status offline";
    }
  } catch {
    statusEl.textContent = "offline";
    statusEl.className = "status offline";
  }
}

checkHealth();
setInterval(checkHealth, 10000);

function truncateResult(result, maxRows) {
  const lines = result.split("\n");
  if (lines.length <= maxRows + 2) return result;
  const header = lines.slice(0, 2);
  const rows = lines.slice(2).filter(l => l.trim());
  const total = rows.length;
  return [...header, ...rows.slice(0, maxRows), `\n(총 ${total}행 중 상위 ${maxRows}행)`].join("\n");
}

// --- Quickstart ---

document.querySelectorAll(".qs-btn").forEach((btn) => {
  btn.addEventListener("click", () => handleQuickstart(btn));
});

async function handleQuickstart(btn) {
  const tool = btn.dataset.tool;
  let args = JSON.parse(btn.dataset.args);

  if (tool === "search_aircraft" || tool === "lookup_aircraft") {
    const hex = prompt("ICAO hex 코드 입력 (예: 71BE07):");
    if (!hex) return;
    args.hex_ident = hex.trim().toUpperCase();
  }

  if (tool === "reverse_geocode") {
    const lat = prompt("위도 (lat):", "37.4");
    if (!lat) return;
    const lon = prompt("경도 (lon):", "127.0");
    if (!lon) return;
    args.lat = parseFloat(lat);
    args.lon = parseFloat(lon);
  }

  addMessage("user", `${btn.textContent}`);

  const resultDiv = document.createElement("div");
  resultDiv.className = "message tool-result";
  resultDiv.innerHTML = `<div class="tool-name">${tool}</div><pre>조회 중...</pre>`;
  messagesEl.appendChild(resultDiv);
  messagesEl.scrollTop = messagesEl.scrollHeight;

  document.querySelectorAll(".qs-btn").forEach(b => b.disabled = true);

  try {
    const res = await fetch("api/tool", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name: tool, args }),
    });
    const data = await res.json();
    const contentEl = resultDiv.querySelector("pre");
    contentEl.outerHTML = `<div class="content">${marked.parse(data.result)}</div>`;
    resultDiv.querySelectorAll("pre code").forEach(b => hljs.highlightElement(b));

    // Action buttons
    const btnGroup = document.createElement("div");
    btnGroup.className = "result-actions";

    const summarizeBtn = document.createElement("button");
    summarizeBtn.className = "action-btn";
    summarizeBtn.textContent = "LLM 정리";
    summarizeBtn.addEventListener("click", async () => {
      summarizeBtn.textContent = "보강 중...";
      summarizeBtn.disabled = true;
      try {
        // 1단계: MiniPC에서 항공기 정보 + 지역명 보강
        const truncated = truncateResult(data.result, 10);
        const enrichRes = await fetch("api/enrich", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ result: truncated }),
        });
        const enrichData = await enrichRes.json();
        // 2단계: 보강된 데이터를 GPU LLM에 전달
        summarizeBtn.remove();
        handleSend(`다음 ADS-B 데이터와 보강 정보를 한국어로 읽기 쉽게 정리해줘. 항공기 이름, 위치명을 포함해서:\n\n${enrichData.enriched}`);
      } catch (err) {
        summarizeBtn.textContent = "LLM 정리";
        summarizeBtn.disabled = false;
      }
    });
    btnGroup.appendChild(summarizeBtn);

    const geoTools = ["recent_aircraft", "search_aircraft", "farthest_aircraft"];
    if (geoTools.includes(tool)) {
      const geoBtn = document.createElement("button");
      geoBtn.className = "action-btn";
      geoBtn.textContent = "지역 분석";
      geoBtn.addEventListener("click", () => handleGeoEnrich(data.result, resultDiv, geoBtn));
      btnGroup.appendChild(geoBtn);
    }

    resultDiv.appendChild(btnGroup);
  } catch (err) {
    resultDiv.querySelector("pre").textContent = `Error: ${err.message}`;
    resultDiv.classList.add("error");
  }

  document.querySelectorAll(".qs-btn").forEach(b => b.disabled = false);
  messagesEl.scrollTop = messagesEl.scrollHeight;
}

// --- Geo Enrich ---

async function handleGeoEnrich(result, parentDiv, btn) {
  btn.disabled = true;
  btn.textContent = "분석 중...";

  const lines = result.split("\n").filter(l => l.trim());
  if (lines.length < 3) { btn.textContent = "좌표 없음"; return; }

  const headers = lines[0].split("|").map(h => h.trim().toLowerCase());
  const latIdx = headers.findIndex(h => h === "latitude");
  const lonIdx = headers.findIndex(h => h === "longitude");
  const hexIdx = headers.findIndex(h => h === "hex_ident");
  if (latIdx === -1 || lonIdx === -1) { btn.textContent = "좌표 없음"; return; }

  const rows = lines.slice(2);
  const coords = [];
  for (const row of rows.slice(0, 10)) {
    const cols = row.split("|").map(c => c.trim());
    const lat = parseFloat(cols[latIdx]);
    const lon = parseFloat(cols[lonIdx]);
    const hex = hexIdx >= 0 ? cols[hexIdx] : "";
    if (!isNaN(lat) && !isNaN(lon)) coords.push({ hex, lat, lon });
  }

  if (coords.length === 0) { btn.textContent = "유효한 좌표 없음"; return; }

  const geoResults = [];
  for (let i = 0; i < coords.length; i++) {
    btn.textContent = `분석 중... (${i + 1}/${coords.length})`;
    try {
      const res = await fetch("api/tool", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ name: "reverse_geocode", args: { lat: coords[i].lat, lon: coords[i].lon } }),
      });
      const data = await res.json();
      const firstLine = data.result.split("\n")[0].replace(/\*\*/g, "");
      geoResults.push(`- **${coords[i].hex}** (${coords[i].lat}, ${coords[i].lon}) → ${firstLine}`);
    } catch {
      geoResults.push(`- **${coords[i].hex}** (${coords[i].lat}, ${coords[i].lon}) → 조회 실패`);
    }
    if (i < coords.length - 1) await new Promise(r => setTimeout(r, 1100));
  }

  const geoDiv = document.createElement("div");
  geoDiv.className = "message tool-result";
  geoDiv.innerHTML = `<div class="tool-name">지역 정보 분석</div><div class="content">${marked.parse(geoResults.join("\n"))}</div>`;
  parentDiv.after(geoDiv);
  btn.remove();
  messagesEl.scrollTop = messagesEl.scrollHeight;
}

// --- Helpers ---

function addMessage(role, text) {
  const div = document.createElement("div");
  div.className = `message ${role}`;
  if (role === "user") {
    div.textContent = text;
  }
  messagesEl.appendChild(div);
  messagesEl.scrollTop = messagesEl.scrollHeight;
  return div;
}

function renderMarkdown(el, md) {
  const contentNode = el.querySelector(".content") || el;
  contentNode.innerHTML = marked.parse(md);
  contentNode.querySelectorAll("pre code").forEach((block) => {
    hljs.highlightElement(block);
  });
}

function setGenerating(val) {
  generating = val;
  sendBtn.style.display = val ? "none" : "";
  cancelBtn.style.display = val ? "" : "none";
  input.disabled = val;
  if (!val) input.focus();
}

form.addEventListener("submit", (e) => {
  e.preventDefault();
  handleSend();
});

// Cancel button
cancelBtn.addEventListener("click", () => {
  if (abortController) {
    abortController.abort();
    abortController = null;
  }
});

// --- Non-streaming LLM chat with cancel support ---

async function handleSend(directText) {
  const text = directText || input.value.trim();
  if (!text || generating) return;

  input.value = "";
  input.style.height = "auto";
  addMessage("user", directText ? "(LLM 정리 요청)" : text);

  chatHistory.push({ role: "user", content: text });
  // 이력 크기 제한 — 오래된 대화 제거로 프롬프트 크기 관리
  while (chatHistory.length > MAX_HISTORY + 1) {
    chatHistory.shift();
  }

  const assistantDiv = addMessage("assistant", "");
  const thinkingDiv = document.createElement("div");
  thinkingDiv.className = "thinking";
  thinkingDiv.textContent = "모델 로딩 중...";
  assistantDiv.appendChild(thinkingDiv);
  const contentNode = document.createElement("div");
  contentNode.className = "content";
  assistantDiv.appendChild(contentNode);

  const startTime = Date.now();
  const thinkingTimer = setInterval(() => {
    const elapsed = Math.floor((Date.now() - startTime) / 1000);
    thinkingDiv.textContent = `응답 생성 중... (${elapsed}초)`;
  }, 1000);

  setGenerating(true);
  abortController = new AbortController();

  try {
    const res = await fetch("api/chat", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ messages: chatHistory }),
      signal: abortController.signal,
    });
    const text = await res.text();
    let data;
    try {
      data = JSON.parse(text);
    } catch {
      if (res.status === 524 || res.status === 522) {
        data = { error: "Cloudflare 타임아웃 (100초). GPU Desktop이 꺼져있을 수 있습니다." };
      } else if (res.status === 403) {
        data = { error: "Cloudflare 차단 (403). 내부 네트워크에서 시도해주세요." };
      } else {
        data = { error: `응답 처리 실패 (HTTP ${res.status}). 내부 네트워크에서 시도해주세요.` };
      }
    }

    clearInterval(thinkingTimer);
    const elapsed = Math.floor((Date.now() - startTime) / 1000);

    if (data.error) {
      thinkingDiv.remove();
      contentNode.remove();
      assistantDiv.textContent = data.error;
      assistantDiv.classList.add("error");
    } else {
      const s = data.stats || {};
      const backend = s.backend || "Local";
      thinkingDiv.textContent = `[${backend}] ${s.total_sec || elapsed}초 | 로드 ${s.load_sec || '?'}초 | ${s.prompt_tokens || '?'}→${s.output_tokens || '?'} tok | ${s.tok_per_sec || '?'} tok/s`;
      thinkingDiv.classList.add("done");
      renderMarkdown(contentNode, data.content);
      chatHistory.push({ role: "assistant", content: data.content });
    }
  } catch (err) {
    clearInterval(thinkingTimer);
    if (err.name === "AbortError") {
      thinkingDiv.textContent = "취소됨";
      thinkingDiv.classList.add("done");
      chatHistory.pop(); // remove the unanswered user message
    } else {
      clearInterval(thinkingTimer);
      const elapsed = Math.floor((Date.now() - startTime) / 1000);
      thinkingDiv.remove();
      contentNode.remove();
      if (elapsed > 90) {
        assistantDiv.textContent = `Cloudflare 타임아웃 (${elapsed}초). GPU Desktop이 꺼져있을 수 있습니다. 내부 네트워크에서 시도해주세요.`;
      } else {
        assistantDiv.textContent = `Error: ${err.message}`;
      }
      assistantDiv.classList.add("error");
      chatHistory.pop();
    }
  }

  abortController = null;
  setGenerating(false);
  messagesEl.scrollTop = messagesEl.scrollHeight;
}

// --- Streaming version (주석 해제 시 스트리밍 모드로 전환) ---
// 백엔드 main.py의 streaming chat 주석도 함께 활성화 필요.
// setGenerating에서 sendBtn/cancelBtn 토글이 동일하게 동작함.
//
// async function handleSend(directText) { ... SSE reader loop version ... }
