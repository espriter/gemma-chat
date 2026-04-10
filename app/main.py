import json
import os
import socket
import asyncio
from pathlib import Path

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from app.tools import execute_tool

OLLAMA_LOCAL = "http://localhost:11434"
OLLAMA_GPU = "http://localhost:11435"  # SSH tunnel → GPU Desktop WSL Ollama
MODEL_LOCAL = "qwen25-minipc"
MODEL_GPU = "gemma4:e4b-it-q8_0"
GPU_PROBE_TIMEOUT = 3  # GPU Desktop 접근 가능 여부 확인 타임아웃(초)
SLACK_WEBHOOK = os.environ.get("SLACK_WEBHOOK_URL", "")


async def _notify_slack(backend: str, user_msg: str, client_ip: str):
    """LLM 요청 시 Slack 알림 (fire-and-forget)."""
    try:
        text = f":robot_face: *ADS-B Chat LLM 요청*\n>*Backend:* `{backend}`\n>*IP:* `{client_ip}`\n>*내용:* {user_msg[:200]}"
        async with httpx.AsyncClient(timeout=5) as client:
            await client.post(SLACK_WEBHOOK, json={"text": text})
    except Exception:
        pass  # 알림 실패해도 무시

app = FastAPI()

static_dir = Path(__file__).parent / "static"
app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")


@app.get("/", response_class=HTMLResponse)
async def index():
    return (static_dir / "index.html").read_text()


# --- LLM Chat Mode (GPU fallback → Local) ---
# 1차: GPU Desktop Ollama (SSH 터널, localhost:11435) 시도
# 2차: 로컬 MiniPC Ollama (localhost:11434) fallback
CHAT_TIMEOUT = 600


async def _probe_gpu() -> bool:
    """GPU Desktop Ollama 접근 가능 여부를 빠르게 확인."""
    try:
        async with httpx.AsyncClient(timeout=GPU_PROBE_TIMEOUT) as client:
            resp = await client.get(f"{OLLAMA_GPU}/api/tags")
            return resp.status_code == 200
    except Exception:
        return False


def _build_stats(data: dict, backend: str) -> dict:
    return {
        "backend": backend,
        "total_sec": round(data.get("total_duration", 0) / 1e9, 1),
        "load_sec": round(data.get("load_duration", 0) / 1e9, 1),
        "prompt_tokens": data.get("prompt_eval_count", 0),
        "output_tokens": data.get("eval_count", 0),
        "tok_per_sec": round(data.get("eval_count", 0) / max(data.get("eval_duration", 1) / 1e9, 0.1), 1),
    }


import re
import time as _time

# GPU 모드: 풍부한 분석, auto_enrich 활성
SYSTEM_PROMPT_GPU = {
    "role": "system",
    "content": (
        "너는 ADS-B Chat 어시스턴트야. 사용자가 항공기 hex 코드나 좌표를 언급하면 "
        "자동으로 조회된 정보가 [참고 정보]로 제공돼. 이 정보를 활용해서 상세하게 답변해. "
        "항공사명, 기종, 위치(지역명), 고도, 속도 등을 포함해서 한국어로 답변해."
    ),
}

# Local 모드: 간결한 안내만, auto_enrich 비활성 (프롬프트 절약)
SYSTEM_PROMPT_LOCAL = {
    "role": "system",
    "content": (
        "너는 ADS-B Chat 안내 봇이야. 짧게 답변해. "
        "데이터 조회가 필요하면 퀵스타트 버튼(최근 항공기, 항공기 정보, 항공기 최근 정보, 날씨 등)을 안내해. "
        "한국어로 2~3문장 이내로 답변해."
    ),
}

# hex 코드 패턴 (6자리 hex, 대소문자)
HEX_PATTERN = re.compile(r'\b([0-9A-Fa-f]{6})\b')
# 좌표 패턴 (lat, lon)
COORD_PATTERN = re.compile(r'(\d{1,3}\.\d{2,6})\s*,\s*(\d{1,3}\.\d{2,6})')


def _auto_enrich_message(text: str) -> str:
    """사용자 메시지에서 hex 코드와 좌표를 감지하여 자동 조회."""
    enrichments = []

    # hex 코드 감지 → lookup_aircraft
    hexes = set(HEX_PATTERN.findall(text))
    for h in sorted(hexes)[:3]:  # 최대 3개
        info = execute_tool("lookup_aircraft", {"hex_ident": h.upper()})
        if "찾을 수 없습니다" not in info:
            one_line = info.replace("\n", ", ").replace("- **", "").replace("**:", ":").replace("**", "")
            enrichments.append(f"항공기 {h.upper()}: {one_line[:150]}")

    # 좌표 감지 → reverse_geocode
    coords = COORD_PATTERN.findall(text)
    for lat_s, lon_s in coords[:2]:  # 최대 2개
        lat, lon = float(lat_s), float(lon_s)
        if 30 < lat < 45 and 120 < lon < 135:  # 한반도 근처만
            geo = execute_tool("reverse_geocode", {"lat": lat, "lon": lon})
            first_line = geo.split("\n")[0].replace("**", "")
            enrichments.append(f"위치 ({lat}, {lon}): {first_line}")
            _time.sleep(1.1)  # Nominatim rate limit

    return "\n".join(enrichments) if enrichments else ""


@app.post("/api/chat")
async def chat(request: Request):
    """LLM chat — GPU Desktop 우선, 실패 시 로컬 fallback."""
    body = await request.json()
    messages = body.get("messages", [])
    client_ip = request.headers.get("cf-connecting-ip") or request.headers.get("x-real-ip") or request.client.host
    is_external = bool(request.headers.get("cf-connecting-ip"))

    # 외부 접근 차단
    if is_external:
        return {"error": "LLM 기능은 외부에서 사용할 수 없습니다."}

    gpu_available = await _probe_gpu()

    # 시스템 프롬프트: GPU/Local에 따라 분리
    if not messages or messages[0].get("role") != "system":
        messages = [SYSTEM_PROMPT_GPU if gpu_available else SYSTEM_PROMPT_LOCAL] + messages

    # GPU 모드: auto_enrich 활성 (hex/좌표 자동 조회)
    if gpu_available:
        user_msg = messages[-1].get("content", "") if messages else ""
        if messages[-1].get("role") == "user":
            enriched = _auto_enrich_message(user_msg)
            if enriched:
                messages[-1] = {
                    "role": "user",
                    "content": user_msg + f"\n\n[참고 정보]\n{enriched}",
                }

    user_msg = messages[-1].get("content", "") if messages else ""

    # 1차: GPU Desktop 시도
    if gpu_available:
        asyncio.create_task(_notify_slack("GPU", user_msg, client_ip))
        try:
            payload = {"model": MODEL_GPU, "messages": messages, "stream": False}
            async with httpx.AsyncClient(timeout=CHAT_TIMEOUT) as client:
                resp = await client.post(f"{OLLAMA_GPU}/api/chat", json=payload)
                data = resp.json()
                if "error" not in data:
                    content = data.get("message", {}).get("content", "")
                    return {"content": content, "stats": _build_stats(data, "GPU")}
        except Exception:
            pass  # GPU 실패 → 로컬 fallback

    # 2차: 로컬 MiniPC
    asyncio.create_task(_notify_slack("Local", user_msg, client_ip))
    try:
        payload = {"model": MODEL_LOCAL, "messages": messages, "stream": False}
        async with httpx.AsyncClient(timeout=CHAT_TIMEOUT) as client:
            resp = await client.post(f"{OLLAMA_LOCAL}/api/chat", json=payload)
            data = resp.json()
            if "error" in data:
                return {"error": data["error"]}
            content = data.get("message", {}).get("content", "")
            return {"content": content, "stats": _build_stats(data, "Local")}
    except httpx.ReadTimeout:
        return {"error": f"응답 시간 초과 ({CHAT_TIMEOUT}초). 질문을 짧게 해보세요."}
    except Exception as e:
        return {"error": str(e)}


# --- Streaming version (주석 해제하면 스트리밍 모드로 전환) ---
# 위의 chat 함수를 주석처리하고, 아래를 활성화하면 토큰 단위 스트리밍.
# 프론트엔드도 SSE 방식(handleSend의 reader 루프)으로 복원 필요.
#
# @app.post("/api/chat")
# async def chat(request: Request):
#     """LLM chat — SSE streaming, tokens arrive one by one."""
#     body = await request.json()
#     messages = body.get("messages", [])
#
#     async def stream():
#         try:
#             payload = {"model": MODEL, "messages": messages, "stream": True}
#             async with httpx.AsyncClient(timeout=CHAT_TIMEOUT) as client:
#                 async with client.stream("POST", f"{OLLAMA_BASE}/api/chat", json=payload) as resp:
#                     async for line in resp.aiter_lines():
#                         if not line:
#                             continue
#                         data = json.loads(line)
#                         if "error" in data:
#                             yield f"data: {json.dumps({'error': data['error']})}\n\n"
#                             return
#                         msg = data.get("message", {})
#                         yield f"data: {json.dumps({'token': msg.get('content', ''), 'done': data.get('done', False)})}\n\n"
#         except Exception as e:
#             yield f"data: {json.dumps({'error': str(e)})}\n\n"
#
#     return StreamingResponse(stream(), media_type="text/event-stream")


@app.post("/api/tool")
async def tool_direct(request: Request):
    """Direct tool execution without LLM — for quickstart buttons."""
    body = await request.json()
    name = body.get("name", "")
    args = body.get("args", {})
    result = execute_tool(name, args, pretty=True)
    return {"name": name, "result": result}


@app.post("/api/enrich")
async def enrich(request: Request):
    """Enrich tool result with aircraft info + geocoding before LLM summary."""
    body = await request.json()
    result = body.get("result", "")

    lines = result.split("\n")
    if len(lines) < 3:
        return {"enriched": result}

    # Parse header to find hex_ident, latitude, longitude columns
    headers = [h.strip().lower() for h in lines[0].split("|")]
    hex_idx = next((i for i, h in enumerate(headers) if h == "hex_ident"), None)
    lat_idx = next((i for i, h in enumerate(headers) if h in ("latitude", "위도")), None)
    lon_idx = next((i for i, h in enumerate(headers) if h in ("longitude", "경도")), None)

    # Collect unique hex_idents and coordinates
    hex_set = set()
    coords = []
    for line in lines[2:]:
        cols = [c.strip() for c in line.split("|")]
        if hex_idx is not None and hex_idx < len(cols):
            h = cols[hex_idx].strip().replace("`", "")
            if h and h != "-":
                hex_set.add(h)
        if lat_idx is not None and lon_idx is not None:
            try:
                lat = float(cols[lat_idx])
                lon = float(cols[lon_idx])
                coords.append((lat, lon))
            except (ValueError, IndexError):
                pass

    enrichments = []

    # Aircraft lookup (parallel-safe, no rate limit)
    if hex_set:
        enrichments.append("**항공기 정보:**")
        for hex_id in sorted(hex_set)[:10]:
            info = execute_tool("lookup_aircraft", {"hex_ident": hex_id})
            # Extract key fields from result
            one_line = info.replace("\n", " ").replace("- **", "").replace("**:", ":").replace("**", "")
            enrichments.append(f"- `{hex_id}`: {one_line[:120]}")

    # Reverse geocode (1 req/sec rate limit, max 5)
    if coords:
        import time
        enrichments.append("\n**위치 정보:**")
        seen = set()
        for lat, lon in coords[:5]:
            key = f"{round(lat,2)},{round(lon,2)}"
            if key in seen:
                continue
            seen.add(key)
            geo = execute_tool("reverse_geocode", {"lat": lat, "lon": lon})
            first_line = geo.split("\n")[0].replace("**", "")
            enrichments.append(f"- ({lat}, {lon}) → {first_line}")
            time.sleep(1.1)

    enriched = result + "\n\n---\n" + "\n".join(enrichments) if enrichments else result
    return {"enriched": enriched}


# --- Wake-on-LAN ---
GPU_DESKTOP_IP = "192.168.12.32"
GPU_DESKTOP_MAC = "34:5a:60:6c:00:74"
GPU_BROADCAST = "192.168.12.255"
WOL_PORT = 9


def _build_magic_packet(mac: str) -> bytes:
    """FF FF FF FF FF FF + MAC 16번 반복 (총 102바이트)"""
    mac_bytes = bytes.fromhex(mac.replace(":", "").replace("-", ""))
    return b"\xff" * 6 + mac_bytes * 16


@app.post("/api/wol")
async def wake_on_lan():
    """GPU Desktop에 Wake-on-LAN 매직 패킷 전송."""
    try:
        packet = _build_magic_packet(GPU_DESKTOP_MAC)
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
            sock.sendto(packet, (GPU_BROADCAST, WOL_PORT))
        return {"status": "sent", "mac": GPU_DESKTOP_MAC}
    except Exception as e:
        return {"status": "error", "detail": str(e)}


@app.post("/api/wol/shutdown")
async def shutdown_gpu():
    """GPU Desktop을 SSH 경유로 원격 종료."""
    try:
        proc = await asyncio.create_subprocess_exec(
            "ssh", "-o", "ConnectTimeout=3", f"espriter@{GPU_DESKTOP_IP}",
            "shutdown", "/s", "/t", "5",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        await asyncio.wait_for(proc.wait(), timeout=10)
        return {"status": "shutdown_sent"}
    except Exception as e:
        return {"status": "error", "detail": str(e)}


@app.get("/api/wol/status")
async def wol_status():
    """GPU Desktop 상태 체크 (SSH 포트 22 접근으로 판단)."""
    try:
        conn = asyncio.open_connection(GPU_DESKTOP_IP, 22)
        reader, writer = await asyncio.wait_for(conn, timeout=2)
        writer.close()
        await writer.wait_closed()
        return {"status": "online", "ip": GPU_DESKTOP_IP}
    except Exception:
        return {"status": "offline", "ip": GPU_DESKTOP_IP}


@app.get("/api/health")
async def health():
    gpu_ok = await _probe_gpu()
    try:
        async with httpx.AsyncClient(timeout=5) as client:
            resp = await client.get(f"{OLLAMA_LOCAL}/api/tags")
            models = resp.json().get("models", [])
            local_ready = any(MODEL_LOCAL in m.get("name", "") for m in models)
            return {
                "ollama": True,
                "model_ready": local_ready or gpu_ok,
                "model": MODEL_GPU if gpu_ok else MODEL_LOCAL,
                "backend": "GPU" if gpu_ok else "Local",
            }
    except Exception:
        return {"ollama": gpu_ok, "model_ready": gpu_ok, "model": MODEL_GPU if gpu_ok else MODEL_LOCAL, "backend": "GPU" if gpu_ok else "offline"}
