import json
from pathlib import Path

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from app.tools import TOOL_DEFINITIONS, execute_tool

OLLAMA_BASE = "http://localhost:11434"
MODEL = "gemma4:e4b"

app = FastAPI()

static_dir = Path(__file__).parent / "static"
app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")


@app.get("/", response_class=HTMLResponse)
async def index():
    return (static_dir / "index.html").read_text()


def _build_payload(messages, tools=None, stream=True):
    payload = {"model": MODEL, "messages": messages, "stream": stream}
    if tools:
        payload["tools"] = tools
    return payload


@app.post("/api/chat")
async def chat(request: Request):
    body = await request.json()
    messages = body.get("messages", [])
    use_tools = body.get("tools", True)

    async def stream():
        try:
            async with httpx.AsyncClient(timeout=None) as client:
                # Phase 1: non-streaming call with tools to check for tool_calls
                if use_tools:
                    yield f"data: {json.dumps({'status': 'thinking'})}\n\n"
                    resp = await client.post(f"{OLLAMA_BASE}/api/chat", json=_build_payload(messages, TOOL_DEFINITIONS, stream=False))
                    data = resp.json()

                    if "error" in data:
                        yield f"data: {json.dumps({'error': data['error']})}\n\n"
                        return

                    msg = data.get("message", {})
                    tool_calls = msg.get("tool_calls")

                    if tool_calls:
                        # Execute tools and build follow-up messages
                        messages.append(msg)
                        for tc in tool_calls:
                            fn = tc.get("function", {})
                            name = fn.get("name", "")
                            args = fn.get("arguments", {})
                            yield f"data: {json.dumps({'status': f'querying: {name}'})}\n\n"
                            result = execute_tool(name, args)
                            messages.append({"role": "tool", "content": result})

                        # Phase 2: stream final response with tool results
                        async with client.stream("POST", f"{OLLAMA_BASE}/api/chat", json=_build_payload(messages)) as resp2:
                            async for line in resp2.aiter_lines():
                                if not line:
                                    continue
                                chunk = json.loads(line)
                                if "error" in chunk:
                                    yield f"data: {json.dumps({'error': chunk['error']})}\n\n"
                                    return
                                m = chunk.get("message", {})
                                yield f"data: {json.dumps({'token': m.get('content', ''), 'thinking': m.get('thinking', ''), 'done': chunk.get('done', False)})}\n\n"
                        return

                    # No tool call — model answered directly (non-streamed)
                    content = msg.get("content", "")
                    thinking = msg.get("thinking", "")
                    if thinking:
                        yield f"data: {json.dumps({'thinking': thinking, 'token': '', 'done': False})}\n\n"
                    if content:
                        yield f"data: {json.dumps({'token': content, 'thinking': '', 'done': False})}\n\n"
                    yield f"data: {json.dumps({'token': '', 'thinking': '', 'done': True})}\n\n"
                    return

                # No tools — plain streaming
                async with client.stream("POST", f"{OLLAMA_BASE}/api/chat", json=_build_payload(messages)) as resp:
                    async for line in resp.aiter_lines():
                        if not line:
                            continue
                        data = json.loads(line)
                        if "error" in data:
                            yield f"data: {json.dumps({'error': data['error']})}\n\n"
                            return
                        msg = data.get("message", {})
                        yield f"data: {json.dumps({'token': msg.get('content', ''), 'thinking': msg.get('thinking', ''), 'done': data.get('done', False)})}\n\n"
        except Exception as e:
            yield f"data: {json.dumps({'error': str(e)})}\n\n"

    return StreamingResponse(stream(), media_type="text/event-stream")


@app.get("/api/health")
async def health():
    try:
        async with httpx.AsyncClient(timeout=5) as client:
            resp = await client.get(f"{OLLAMA_BASE}/api/tags")
            models = resp.json().get("models", [])
            gemma_ready = any(MODEL in m.get("name", "") for m in models)
            return {"ollama": True, "model_ready": gemma_ready, "model": MODEL}
    except Exception:
        return {"ollama": False, "model_ready": False, "model": MODEL}
