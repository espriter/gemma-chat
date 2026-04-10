# ADS-B Chat

ADS-B 항공기 데이터를 퀵스타트 버튼으로 즉시 조회하고, GPU Desktop LLM으로 요약/대화하는 웹앱.

> Built with Claude Code (vibe coding) — 대화형 AI 페어 프로그래밍으로 설계부터 구현까지 완성.

## 아키텍처

![Architecture](docs/architecture.svg)

### 하이브리드 구조

MiniPC(CPU-only)에서 Ollama tool calling이 비현실적(프롬프트 평가 5분+)이어서, 퀵스타트(직접 호출)와 LLM(순수 대화)을 분리한 하이브리드 구조.

| 기능 | 실행 위치 | LLM 사용 | 속도 |
|------|----------|---------|------|
| 퀵스타트 (DB 조회) | MiniPC 직접 | X | ~15초 |
| LLM 정리 (보강+요약) | MiniPC 보강 → GPU LLM | O | ~10초 (GPU) |
| LLM 대화 | GPU 우선, 로컬 fallback | O | GPU ~5초, Local ~2분 |
| 지역 분석 | MiniPC → Nominatim | X | ~10초 |

### 데이터 흐름

**퀵스타트** (LLM 없음):
```
Browser → FastAPI /api/tool → tools.py → PostgreSQL → Pretty 포맷 → Browser
```

**LLM 정리** (보강 + GPU 요약):
```
1. Browser → /api/enrich → hexdb.io(항공기 정보) + Nominatim(지역명) 보강
2. Browser → /api/chat → SSH 터널 → GPU Ollama (gemma4:e4b-it-q8_0) → 한국어 요약
3. GPU 불가 시 → 로컬 Ollama (qwen2.5:1.5b) fallback
```

### 노드 역할

| 노드 | 역할 | 상시 |
|------|------|------|
| **MiniPC** | FastAPI 서비스, 도구 실행, 데이터 보강, 로컬 LLM fallback | ON |
| **GPU Desktop** | Ollama 모델 서빙만 (서비스 코드 없음) | WOL ON/OFF |
| **RPI4** | PostgreSQL (ADS-B 원본 데이터) | ON |

> **GPU Desktop에는 gemma-chat 코드를 배포하지 않는다.** MiniPC가 SSH 터널로 Ollama API만 호출.

## 퀵스타트 도구 (8개)

### DB 도구 (5개) — RPI4 PostgreSQL

| 도구 | 설명 | 예시 |
|------|------|------|
| `recent_aircraft` | 최근 N분 고유 항공기 last 위치 (KST) | "지금 뜨는 비행기" |
| `search_aircraft` | 특정 hex 10분 단위 위치 이력 (최대 6행) | "71BE07 경로" |
| `unique_aircraft` | 고유 항공기 통계 | "오늘 몇 대 지나갔어?" |
| `farthest_aircraft` | 안테나 기준 최장거리 항공기 | "가장 먼 비행기" |
| `traffic_summary` | 일별 트래픽 요약 | "주간 트래픽" |

### 외부 API 도구 (3개)

| 도구 | API | 설명 |
|------|-----|------|
| `lookup_aircraft` | hexdb.io | ICAO hex → 등록번호/기종/항공사 |
| `get_weather` | wttr.in | 현재 날씨 + 3일 예보 |
| `reverse_geocode` | Nominatim | 좌표 → 주소 (지역 분석 버튼) |

### 안전장치

- 12개 DML/DDL 키워드 차단 + PG readonly + 전용 계정 + 60초 타임아웃 + 500행 제한

## 기술 스택

| 영역 | MiniPC | GPU Desktop |
|------|--------|-------------|
| LLM | qwen2.5:1.5b (986MB, CPU) | gemma4:e4b-it-q8_0 (12GB, GPU) |
| GPU | — | RTX 5070 Ti 16GB |
| 런타임 | Ollama 0.20.4 | Ollama 0.20.3 |
| 백엔드 | FastAPI + httpx | — |
| 프론트엔드 | Vanilla HTML/CSS/JS | — |
| DB | psycopg2 → RPI4 PostgreSQL | — |
| 연결 | SSH 터널 (11435→11434) | — |
| 프로세스 | systemd | systemd (Ollama) |
| 외부 접근 | Cloudflare Tunnel | WOL 원격 ON/OFF |

## 프론트엔드

- **디자인**: GitHub Dark 모바일 우선
- **퀵스타트**: 7개 버튼, 모바일 가로 스크롤, Pretty 포맷 (마크다운 테이블)
- **결과 액션**: LLM 정리 (보강→GPU 요약), 지역 분석 (Nominatim)
- **LLM 대화**: Non-streaming, 취소 버튼, 경과 시간, 통계 `[GPU/Local] tok/s`
- **Templates**: 쿼리 파라미터 복붙용 페이지

## WOL 원격 제어

GPU Desktop은 상시 OFF. 필요할 때 원격으로 켜고 끔.

- **Wake**: UDP 매직 패킷 (MAC `34:5a:60:6c:00:74`)
- **Shutdown**: SSH → `shutdown /s /t 5`
- **상태 확인**: SSH 포트(22) TCP 체크
- **자동 체인**: WOL → Windows 자동 로그인 → 작업 스케줄러(WSL) → Ollama systemd
- **WOL 페이지**: `/wol/` (nginx mTLS 보호)

## 디렉토리 구조

```
/srv/gemma-chat/
├── app/
│   ├── __init__.py
│   ├── main.py              # FastAPI: chat(GPU fallback), tool, enrich, wol, health
│   ├── tools.py              # 8개 도구 + pretty 포맷 + DB/API 실행
│   └── static/
│       ├── index.html        # 메인 UI (퀵스타트 + 채팅)
│       ├── style.css         # GitHub Dark 모바일 우선 테마
│       ├── app.js            # 퀵스타트, LLM 대화, 지역 분석, 취소
│       ├── templates.html    # 쿼리 파라미터 레퍼런스
│       └── favicon.svg
├── docs/
│   ├── overview.md
│   ├── architecture.d2       # 아키텍처 다이어그램 소스
│   └── architecture.svg
├── Modelfile                 # qwen2.5:1.5b → qwen25-minipc (num_ctx=4096, thread=8)
├── ollama-override.conf      # Ollama systemd 리소스 제한
├── .env                      # DB 접속 + Slack webhook (gitignored)
├── .env.example
├── requirements.txt          # fastapi, uvicorn, httpx, psycopg2-binary
├── start.sh
└── gemma-chat.service        # systemd 유닛
```

## 운영

### 서비스 관리

```bash
sudo systemctl start gemma-chat
sudo systemctl stop gemma-chat
journalctl -u gemma-chat -f
```

### 접속

- 내부: `http://localhost:8095`
- 외부: `https://adsb.espriter.net`
- WOL: `https://<host>/wol/` (mTLS)

### SSH 터널 (GPU 연동)

```bash
ssh -f -N -L 11435:localhost:11434 espriter@192.168.12.32
```

### 다이어그램 재생성

```bash
d2 --layout=elk docs/architecture.d2 docs/architecture.svg
```
