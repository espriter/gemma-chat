const messagesEl = document.getElementById("messages");
const form = document.getElementById("chat-form");
const input = document.getElementById("input");
const sendBtn = document.getElementById("send-btn");
const statusEl = document.getElementById("status");

let chatHistory = [];
let generating = false;

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
  input.style.height = Math.min(input.scrollHeight, 150) + "px";
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
    const res = await fetch("/api/health");
    const data = await res.json();
    if (data.model_ready) {
      statusEl.textContent = "online";
      statusEl.className = "status online";
    } else if (data.ollama) {
      statusEl.textContent = "model loading...";
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
  // highlight any code blocks that marked didn't catch
  contentNode.querySelectorAll("pre code").forEach((block) => {
    hljs.highlightElement(block);
  });
}

function setGenerating(val) {
  generating = val;
  sendBtn.disabled = val;
  input.disabled = val;
  if (!val) input.focus();
}

form.addEventListener("submit", (e) => {
  e.preventDefault();
  handleSend();
});

async function handleSend() {
  const text = input.value.trim();
  if (!text || generating) return;

  input.value = "";
  input.style.height = "auto";
  addMessage("user", text);

  chatHistory.push({ role: "user", content: text });

  const assistantDiv = addMessage("assistant", "");
  const thinkingDiv = document.createElement("div");
  thinkingDiv.className = "thinking";
  thinkingDiv.textContent = "thinking...";
  assistantDiv.appendChild(thinkingDiv);
  const contentNode = document.createElement("div");
  contentNode.className = "content";
  assistantDiv.appendChild(contentNode);
  const cursor = document.createElement("span");
  cursor.className = "cursor";
  assistantDiv.appendChild(cursor);

  setGenerating(true);

  let fullResponse = "";
  let fullThinking = "";
  let isThinking = true;

  try {
    const res = await fetch("/api/chat", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ messages: chatHistory }),
    });

    const reader = res.body.getReader();
    const decoder = new TextDecoder();
    let buffer = "";

    while (true) {
      const { done, value } = await reader.read();
      if (done) break;

      buffer += decoder.decode(value, { stream: true });
      const lines = buffer.split("\n\n");
      buffer = lines.pop() || "";

      for (const line of lines) {
        if (!line.startsWith("data: ")) continue;
        const data = JSON.parse(line.slice(6));
        if (data.error) {
          thinkingDiv.remove();
          contentNode.remove();
          cursor.remove();
          assistantDiv.textContent = data.error;
          assistantDiv.classList.add("error");
          setGenerating(false);
          return;
        }
        if (data.status) {
          thinkingDiv.textContent = data.status === "thinking" ? "thinking..." : data.status;
          continue;
        }
        if (data.thinking) {
          fullThinking += data.thinking;
          thinkingDiv.textContent = "thinking...";
        }
        if (data.token) {
          if (isThinking) {
            isThinking = false;
            thinkingDiv.textContent = "thought for a moment";
            thinkingDiv.classList.add("done");
          }
          fullResponse += data.token;
          // Show raw text while streaming for performance
          contentNode.textContent = fullResponse;
        }
        messagesEl.scrollTop = messagesEl.scrollHeight;
      }
    }
  } catch (err) {
    fullResponse = fullResponse || `Error: ${err.message}`;
    thinkingDiv.remove();
    assistantDiv.textContent = fullResponse;
  }

  cursor.remove();

  // Render markdown when streaming is complete
  if (fullResponse) {
    renderMarkdown(contentNode, fullResponse);
  }

  chatHistory.push({ role: "assistant", content: fullResponse });
  setGenerating(false);
}
