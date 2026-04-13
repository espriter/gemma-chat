# ADS-B View

ADS-B 항공기 데이터를 퀵스타트 버튼으로 즉시 조회하고, GPU Desktop LLM(gemma4:e4b-it-q8_0)으로 요약/대화하는 웹앱. MiniPC(CPU-only)에서 서비스, GPU Desktop은 모델 서빙만.

> Built with Claude Code (vibe coding) — 대화형 AI 페어 프로그래밍으로 설계부터 구현까지 완성.

## 아키텍처

![Architecture](architecture.svg)

### 구성 요소

| 구성 요소 | 역할 | 위치 |
|-----------|------|------|
| **FastAPI** | 웹 UI 서빙 + Ollama/Tool 중계 | localhost:8095 |
| **Ollama** | LLM 런타임 (Gemma 4 E4B) | localhost:11434 |
| **Tool Router** | Ollama tool_call → PostgreSQL/Trino 실행 → 결과 반환 | tools.py |
| **PostgreSQL** | ADS-B 원본 데이터 (~3M rows/day) | 원격 DB 서버 (RPI4) |
| **Trino (Iceberg)** | 집계 레이어 읽기 (`iceberg.adsb_ice.*`) | localhost:8090 (MiniPC) |

### 데이터 흐름

**일반 대화**: Browser → FastAPI → Ollama → Gemma → SSE 스트리밍 → Browser

**DB 조회 대화** (tool calling):

```
1. Browser → FastAPI: 사용자 메시지 ("오늘 수집 통계 보여줘")
2. FastAPI → Ollama: messages + tool definitions (non-streaming)
3. Ollama → FastAPI: tool_calls 응답 (ingestion_stats 호출 결정)
4. FastAPI → tools.py: execute_tool("ingestion_stats", {hours: 24})
5. tools.py → PostgreSQL: SELECT ... (read-only)
6. PostgreSQL → tools.py: 쿼리 결과
7. FastAPI → Ollama: messages + tool result (streaming)
8. Ollama → FastAPI → Browser: 자연어 답변 SSE 스트리밍
```

## 도구 (Tools)

Ollama tool calling을 통해 Gemma가 자율적으로 호출하는 19개 도구.

### DB 도구 (12개) — PostgreSQL (raw `adsb_message`)

| 도구 | 설명 | 예시 질문 |
|------|------|----------|
| `query_adsb_db` | 자유 SELECT SQL 실행 (fallback) | "고도 40000ft 이상 항공기 찾아줘" |
| `ingestion_stats` | 시간별 수집 건수 통계 | "오늘 수집 현황 어때?" |
| `recent_aircraft` | 최근 N분 항공기 위치 | "지금 잡히는 비행기 보여줘" |
| `search_aircraft` | hex_ident로 특정 항공기 이력 추적 | "71BA12 경로 추적해줘" |
| `unique_aircraft` | 고유 항공기 목록 + 통계 (메시지수, 고도, 속도) | "오늘 관측된 항공기 목록" |
| `high_altitude` | 지정 고도 이상 비행 중인 항공기 | "35000ft 이상 비행기" |
| `ground_aircraft` | 지상 항공기 (is_on_ground=true) | "지금 지상에 있는 비행기" |
| `traffic_summary` | 일별 트래픽 요약 (최대 30일) | "이번 주 트래픽 요약" |
| `farthest_aircraft` | 안테나 기준 최장거리 항공기 (Haversine) | "가장 멀리서 잡힌 비행기" |
| `speed_extremes` | 최고속/최저속 항공기 Top 10 | "가장 빠른 비행기 뭐야?" |
| `altitude_distribution` | 고도 구간별 분포 (0-1K ~ 40K+) | "고도별 항공기 분포" |
| `describe_table` | adsb_message 테이블 스키마 조회 | "테이블 구조 보여줘" |

### 집계 레이어 도구 (2개) — Trino → Iceberg (`iceberg.adsb_ice.*`)

adsb-platform의 Airflow 배치가 HDFS/Iceberg로 적재한 Curated/Aggregation 레이어를 Trino(`localhost:8090`)로 읽는다. 연결 헬퍼는 `_execute_trino()` (async statement API, `nextUri` follow, 쓰기 키워드 차단, 500행 제한).

| 도구 | 소스 테이블 | 설명 | 예시 질문 |
|------|-------------|------|----------|
| `agg_weekly_traffic` | `iceberg.adsb_ice.daily_aircraft_stats` | Airflow DAG가 생성한 일별 항공기 통계 (고유 항공기/메시지/위치/평균·최대 고도/평균 지상 비율) | "집계 기준 주간 트래픽" |
| `gps_jump_snapshot` | `iceberg.adsb_ice.hourly_gps_jump_snapshot` | 시간별 GPS jump 이상 스냅샷 (이상 없는 시간은 0 rows = 정상) | "최근 24시간 GPS 이상" |

> `traffic_summary`(raw PostgreSQL)와 `agg_weekly_traffic`(Iceberg)은 병행한다. 전자는 실시간 수신 데이터, 후자는 Airflow 배치 결과라서 한 시간 단위의 지연이 있지만 정제된 값을 보여준다 — 두 관점을 비교하는 UX.

### 외부 API 도구 (5개) — 무료, API 키 불필요

| 도구 | API 소스 | 설명 | 예시 질문 |
|------|----------|------|----------|
| `lookup_aircraft` | hexdb.io | ICAO hex → 등록번호, 기종, 운영사 | "71BA12 이 비행기 뭐야?" |
| `get_weather` | wttr.in | 현재 날씨 + 3일 예보 | "서울 날씨 어때?" |
| `get_exchange_rate` | exchangerate-api.com | 실시간 환율 조회 | "달러 환율 얼마야?" |
| `web_fetch` | (범용 HTTP GET) | 공개 URL 내용 가져오기 (3000자) | "이 URL 내용 읽어줘" |
| `reverse_geocode` | Nominatim (OSM) | 위도/경도 → 주소 (역지오코딩) | "37.53, 127.63 어디야?" |

### 안전장치

- **읽기 전용**: INSERT/UPDATE/DELETE 등 12개 DML/DDL 키워드 차단 (PostgreSQL, Trino 공통)
- **PG readonly**: `conn.set_session(readonly=True)`
- **전용 계정**: 읽기 전용 DB 유저 (SELECT 권한만)
- **Trino 접근**: 인증 없음(내부 네트워크 전용, `X-Trino-User` 식별용), 쿼리 전 키워드 필터 + `MAX_ROWS` 제한
- **타임아웃**: 쿼리당 60초
- **행 제한**: 최대 500행
- **파일 시스템 접근 없음**: 보안상 의도적으로 미제공

## 기술 스택

| 영역 | 기술 |
|------|------|
| LLM | Google Gemma 4 E4B (4.3B active / 9.4B total) |
| GPU | NVIDIA RTX 5070 Ti 16GB VRAM |
| 런타임 | Ollama 0.20.3 (tool calling 지원) |
| 백엔드 | FastAPI + httpx (SSE 프록시) |
| 프론트엔드 | Vanilla HTML/CSS/JS (빌드 없음) |
| DB | PostgreSQL (psycopg2-binary) |
| 프로세스 | systemd (gemma-chat.service, After=ollama.service) |

## 디렉토리 구조

```
/srv/gemma-chat/
├── app/
│   ├── __init__.py
│   ├── main.py            # FastAPI 엔드포인트 + Ollama tool calling 통합
│   ├── tools.py            # 19개 도구 정의 + PostgreSQL/Trino/외부 API 실행
│   └── static/
│       ├── index.html      # marked.js + highlight.js CDN
│       ├── style.css       # 다크 테마 + 마크다운 렌더링 스타일
│       └── app.js          # SSE 클라이언트 + 마크다운 렌더링
├── docs/
│   ├── overview.md         # 이 문서
│   ├── architecture.d2     # 아키텍처 다이어그램 소스
│   └── architecture.svg    # 렌더링된 다이어그램
├── .env                    # DB 접속 정보 (gitignored)
├── .env.example            # 템플릿
├── .gitignore
├── venv/
├── requirements.txt        # fastapi, uvicorn, httpx, psycopg2-binary
├── start.sh
└── gemma-chat.service      # systemd 유닛 파일 (EnvironmentFile=.env)
```

## 운영

### 서비스 관리

```bash
sudo systemctl start gemma-chat    # 시작
sudo systemctl stop gemma-chat     # 중지
sudo systemctl status gemma-chat   # 상태 확인
journalctl -u gemma-chat -f        # 로그
```

### 접속

- URL: `http://localhost:8095`

### 다이어그램 재생성

```bash
d2 --layout=elk docs/architecture.d2 docs/architecture.svg
```
