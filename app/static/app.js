const messagesEl = document.getElementById("messages");
const form = document.getElementById("chat-form");
const input = document.getElementById("input");
const sendBtn = document.getElementById("send-btn");
const cancelBtn = document.getElementById("cancel-btn");
const statusEl = document.getElementById("status");
const llmToggle = document.getElementById("llm-toggle");
const closeLlmBtn = document.getElementById("close-llm");
const toolbarEl = document.getElementById("toolbar");
const mapView = document.getElementById("map-view");
const mapFrame = document.getElementById("map-frame");
const tabs = document.querySelectorAll("#tabs .tab");

// --- Tab switching (Chat / Map) ---
// ADSBexchange 글로벌 맵을 lazy iframe으로 임베드.
// Map 탭을 처음 클릭할 때만 iframe src를 세팅하여 불필요한 트래픽 방지.
const MAP_SRC = "https://globe.adsbexchange.com/?lat=37.4&lon=127.0&zoom=6";
let mapLoaded = false;

function switchView(view) {
  tabs.forEach(t => t.classList.toggle("active", t.dataset.view === view));
  if (view === "map") {
    messagesEl.hidden = true;
    toolbarEl.hidden = true;
    mapView.hidden = false;
    if (!mapLoaded) {
      mapFrame.src = MAP_SRC;
      mapLoaded = true;
    }
  } else {
    messagesEl.hidden = false;
    toolbarEl.hidden = false;
    mapView.hidden = true;
  }
}

tabs.forEach(t => t.addEventListener("click", () => switchView(t.dataset.view)));

// 새 창으로 열기 — 403/차단 시 fallback
document.getElementById("map-open-new").addEventListener("click", () => {
  window.open(MAP_SRC, "_blank", "noopener");
});

// 다시 로드 — iframe src를 한번 비웠다가 재설정 (캐시 우회)
document.getElementById("map-reload").addEventListener("click", () => {
  mapFrame.src = "about:blank";
  setTimeout(() => {
    mapFrame.src = MAP_SRC + "&_t=" + Date.now();
  }, 100);
});

let chatHistory = [];
let generating = false;
let abortController = null;
const MAX_HISTORY = 4;
const IS_EXTERNAL = window.location.hostname === "r4tle.espriter.net";

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

// 외부 접근 제한은 handleSend 내부의 IS_EXTERNAL alert으로 처리

// Shift+Enter for newline, Enter to send
input.addEventListener("keydown", (e) => {
  if (e.key === "Enter" && !e.shiftKey) {
    e.preventDefault();
    handleSend();
  }
});

// Health check
let gpuReady = false;
async function checkHealth() {
  try {
    const res = await fetch("api/health");
    const data = await res.json();
    if (data.model_ready) {
      gpuReady = true;
      statusEl.textContent = "GPU";
      statusEl.className = "status online";
      llmToggle.classList.remove("disabled");
      llmToggle.querySelector(".llm-text").textContent = "LLM에게 질문하기";
    } else {
      gpuReady = false;
      statusEl.textContent = "GPU off";
      statusEl.className = "status offline";
      llmToggle.classList.add("disabled");
      llmToggle.querySelector(".llm-text").textContent = "LLM 대화 불가 (GPU 꺼짐)";
      // GPU 꺼지면 이미 열려있던 입력창도 닫기
      if (!form.hidden) closeLlmForm();
    }
  } catch {
    gpuReady = false;
    statusEl.textContent = "offline";
    statusEl.className = "status offline";
    llmToggle.classList.add("disabled");
    llmToggle.querySelector(".llm-text").textContent = "서버 연결 실패";
    if (!form.hidden) closeLlmForm();
  }
}

function openLlmForm() {
  form.hidden = false;
  llmToggle.hidden = true;
  setTimeout(() => input.focus(), 50);
}

function closeLlmForm() {
  form.hidden = true;
  llmToggle.hidden = false;
  input.value = "";
  input.style.height = "auto";
}

llmToggle.addEventListener("click", () => {
  if (!gpuReady) {
    alert("GPU Desktop이 꺼져있어 대화 모드를 쓸 수 없습니다. Quickstart 버튼으로 데이터 조회는 가능합니다.");
    return;
  }
  openLlmForm();
});

closeLlmBtn.addEventListener("click", () => {
  closeLlmForm();
});

checkHealth();
setInterval(checkHealth, 30000); // 30초마다 상태 확인

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

    // Action buttons — 내부 접근 + GPU 켜져있을 때만 LLM 요약 버튼 표시
    if (!IS_EXTERNAL && gpuReady) {
      const btnGroup = document.createElement("div");
      btnGroup.className = "result-actions";

      const summarizeBtn = document.createElement("button");
      summarizeBtn.className = "action-btn";
      summarizeBtn.textContent = "LLM 요약";
      summarizeBtn.addEventListener("click", async () => {
        summarizeBtn.textContent = "보강 중...";
        summarizeBtn.disabled = true;
        try {
          const truncated = truncateResult(data.result, 10);
          const enrichRes = await fetch("api/enrich", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ result: truncated }),
          });
          const enrichData = await enrichRes.json();
          summarizeBtn.remove();
          handleSend(`다음 ADS-B 데이터를 한국어로 정리해줘. 각 항공기의 hex 코드, 항공사명, 기종, 위치(지역명), 고도, 속도를 포함해서 요약해줘:\n\n${enrichData.enriched}`);
        } catch (err) {
          summarizeBtn.textContent = "LLM 요약";
          summarizeBtn.disabled = false;
        }
      });
      btnGroup.appendChild(summarizeBtn);
      resultDiv.appendChild(btnGroup);
    }
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
  if (IS_EXTERNAL) return; // 외부: 항상 비활성 유지
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

  // 외부 접근 차단
  if (IS_EXTERNAL) {
    alert("LLM 기능은 외부에서 사용할 수 없습니다.");
    return;
  }

  // GPU 미연결 시 차단 (로컬 모드 제거됨)
  if (!gpuReady) {
    alert("GPU Desktop이 꺼져있어 대화 모드를 쓸 수 없습니다. Quickstart 버튼으로 데이터 조회는 가능합니다.");
    return;
  }

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
