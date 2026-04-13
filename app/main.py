import os
import asyncio
import logging
from datetime import datetime
from pathlib import Path

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles

from app.tools import execute_tool, TOOL_DEFINITIONS

OLLAMA_GPU = "http://localhost:11435"  # SSH tunnel → GPU Desktop WSL Ollama
# GPU 전용: 미니PC CPU 추론은 실용 속도가 안 나와 로컬 fallback 제거됨(2026-04-13)
# 더 빠른 모델을 쓰려면 GPU Desktop에서 `ollama pull qwen3:8b` 후 아래 값 변경
MODEL_GPU = "gemma4:e4b-it-q8_0"
GPU_PROBE_TIMEOUT = 3  # GPU Desktop 접근 가능 여부 확인 타임아웃(초)
SLACK_WEBHOOK = os.environ.get("SLACK_WEBHOOK_URL", "")

# --- Access Logger (외부 접근 기록) ---
_access_logger = logging.getLogger("access")
_access_logger.setLevel(logging.INFO)
_access_handler = logging.FileHandler("/srv/gemma-chat/logs/access.log")
_access_handler.setFormatter(logging.Formatter("%(message)s"))
_access_logger.addHandler(_access_handler)

# --- Rate Limiter (외부 접근용) ---
RATE_LIMIT_MAX = 10       # 분당 최대 요청
RATE_LIMIT_WINDOW = 60    # 윈도우 (초)
RATE_LIMIT_BAN = 600      # 초과 시 차단 시간 (초)
_rate_counts: dict = {}   # {ip: [timestamps]}
_rate_bans: dict = {}     # {ip: ban_until_timestamp}


def _check_rate_limit(ip: str) -> bool:
    """외부 IP rate limit 체크. True=허용, False=차단."""
    import time as _t
    now = _t.time()

    # 차단 중인지 확인
    if ip in _rate_bans:
        if now < _rate_bans[ip]:
            return False
        del _rate_bans[ip]

    # 윈도우 내 요청 수 카운트
    if ip not in _rate_counts:
        _rate_counts[ip] = []
    _rate_counts[ip] = [t for t in _rate_counts[ip] if now - t < RATE_LIMIT_WINDOW]
    _rate_counts[ip].append(now)

    if len(_rate_counts[ip]) > RATE_LIMIT_MAX:
        _rate_bans[ip] = now + RATE_LIMIT_BAN
        _rate_counts.pop(ip, None)
        return False

    return True


async def _notify_slack(backend: str, user_msg: str, client_ip: str):
    """LLM 요청 시 Slack 알림 (fire-and-forget)."""
    try:
        text = f":robot_face: *ADS-B Chat LLM 요청*\n>*Backend:* `{backend}`\n>*IP:* `{client_ip}`\n>*내용:* {user_msg[:200]}"
        async with httpx.AsyncClient(timeout=5) as client:
            await client.post(SLACK_WEBHOOK, json={"text": text})
    except Exception:
        pass  # 알림 실패해도 무시

app = FastAPI()


@app.middleware("http")
async def log_external_access(request: Request, call_next):
    """Cloudflare 경유 요청만 로깅 (IP, 국가, 경로)."""
    cf_ip = request.headers.get("cf-connecting-ip")
    if cf_ip and request.url.path != "/api/health":
        country = request.headers.get("cf-ipcountry", "??")
        ts = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
        _access_logger.info(f"{ts} | {country} | {cf_ip} | {request.method} {request.url.path}")
    return await call_next(request)


static_dir = Path(__file__).parent / "static"
app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")


@app.get("/", response_class=HTMLResponse)
async def index():
    return (static_dir / "index.html").read_text()


# --- LLM Chat Mode (GPU 전용) ---
# GPU Desktop Ollama (SSH 터널, localhost:11435). GPU 꺼져있으면 즉시 에러 응답.
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
    """LLM chat — GPU Desktop 전용. GPU 꺼져있으면 에러 반환."""
    body = await request.json()
    messages = body.get("messages", [])
    client_ip = request.headers.get("cf-connecting-ip") or request.headers.get("x-real-ip") or request.client.host
    is_external = bool(request.headers.get("cf-connecting-ip"))

    # 외부 접근 차단
    if is_external:
        return {"error": "LLM 기능은 외부에서 사용할 수 없습니다."}

    gpu_available = await _probe_gpu()
    user_msg = messages[-1].get("content", "") if messages else ""

    # GPU 꺼져있으면 즉시 에러 (로컬 fallback 없음)
    if not gpu_available:
        return {
            "error": "GPU Desktop이 꺼져있어 대화 모드를 쓸 수 없습니다. "
                     "Quickstart 버튼으로 데이터 조회는 가능합니다. "
                     "LLM 대화가 필요하면 GPU를 켜주세요."
        }

    if not messages or messages[0].get("role") != "system":
        messages = [SYSTEM_PROMPT_GPU] + messages

    asyncio.create_task(_notify_slack("GPU", user_msg, client_ip))
    try:
        async with httpx.AsyncClient(timeout=CHAT_TIMEOUT) as client:
            # Phase 1: tool calling (non-streaming)
            payload = {"model": MODEL_GPU, "messages": messages, "tools": TOOL_DEFINITIONS, "stream": False}
            resp = await client.post(f"{OLLAMA_GPU}/api/chat", json=payload)
            data = resp.json()

            if "error" in data:
                return {"error": f"GPU 모델 에러: {data['error']}"}

            msg = data.get("message", {})
            tool_calls = msg.get("tool_calls")

            if tool_calls:
                # 도구 실행 (MiniPC에서 로컬 실행)
                messages.append(msg)
                for tc in tool_calls:
                    fn = tc.get("function", {})
                    name = fn.get("name", "")
                    args = fn.get("arguments", {})
                    result = execute_tool(name, args)
                    messages.append({"role": "tool", "content": result})

                # Phase 2: 도구 결과로 최종 응답
                payload2 = {"model": MODEL_GPU, "messages": messages, "stream": False}
                resp2 = await client.post(f"{OLLAMA_GPU}/api/chat", json=payload2)
                data2 = resp2.json()
                if "error" in data2:
                    return {"error": f"GPU 모델 에러(phase2): {data2['error']}"}
                content = data2.get("message", {}).get("content", "")
                return {"content": content, "stats": _build_stats(data2, "GPU")}
            else:
                # tool call 없이 직접 응답
                content = msg.get("content", "")
                return {"content": content, "stats": _build_stats(data, "GPU")}
    except httpx.ReadTimeout:
        return {"error": f"응답 시간 초과 ({CHAT_TIMEOUT}초). 질문을 짧게 해보세요."}
    except Exception as e:
        return {"error": f"GPU 요청 실패: {e}"}


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
    # 외부 접근 rate limit
    client_ip = request.headers.get("cf-connecting-ip")
    if client_ip and not _check_rate_limit(client_ip):
        return {"name": "", "result": "요청 제한 초과. 10분 후 다시 시도해주세요."}
    body = await request.json()
    name = body.get("name", "")
    args = body.get("args", {})
    result = execute_tool(name, args, pretty=True)
    return {"name": name, "result": result}


@app.post("/api/enrich")
async def enrich(request: Request):
    """Enrich tool result with aircraft info + geocoding before LLM summary."""
    # 외부 접근 rate limit
    client_ip = request.headers.get("cf-connecting-ip")
    if client_ip and not _check_rate_limit(client_ip):
        return {"enriched": "요청 제한 초과. 10분 후 다시 시도해주세요."}
    body = await request.json()
    result = body.get("result", "")

    lines = result.split("\n")
    if len(lines) < 3:
        return {"enriched": result}

    # Parse header to find hex_ident, latitude, longitude columns
    headers = [h.strip().lower() for h in lines[0].split("|")]
    hex_idx = next((i for i, h in enumerate(headers) if h in ("hex_ident", "hex")), None)
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
            await asyncio.sleep(1.1)

    enriched = result + "\n\n---\n" + "\n".join(enrichments) if enrichments else result
    return {"enriched": enriched}


# --- WOL API는 /srv/wol/server.py로 분리됨 (port 8096) ---


@app.get("/api/health")
async def health():
    """GPU Desktop 상태만 확인. 꺼져있으면 LLM 불가, Quickstart만 가능."""
    gpu_ok = await _probe_gpu()
    return {
        "ollama": gpu_ok,
        "model_ready": gpu_ok,
        "model": MODEL_GPU if gpu_ok else None,
        "backend": "GPU" if gpu_ok else "offline",
    }
