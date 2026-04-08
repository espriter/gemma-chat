"""Ollama tool definitions and execution for RPI4 PostgreSQL and external APIs."""

import json
import os

import httpx
import psycopg2

DB_CONFIG = {
    "host": os.environ.get("ADSB_DB_HOST", "localhost"),
    "port": int(os.environ.get("ADSB_DB_PORT", "5432")),
    "dbname": os.environ.get("ADSB_DB_NAME", "adsb"),
    "user": os.environ.get("ADSB_DB_USER", "readonly_user"),
    "password": os.environ.get("ADSB_DB_PASSWORD", ""),
}

BLOCKED_KEYWORDS = [
    "INSERT", "UPDATE", "DELETE", "DROP", "ALTER", "TRUNCATE",
    "CREATE", "GRANT", "REVOKE", "COPY", "VACUUM", "REINDEX",
]

# Antenna location for distance calculations
ANTENNA_LAT = 37.4
ANTENNA_LON = 127.0

MAX_ROWS = 500


def _execute_query(sql: str) -> str:
    sql_upper = sql.upper()
    for kw in BLOCKED_KEYWORDS:
        if kw in sql_upper:
            return f"차단됨: {kw} 구문은 사용할 수 없습니다 (읽기 전용)."

    try:
        conn = psycopg2.connect(**DB_CONFIG)
        conn.set_session(readonly=True, autocommit=True)
        cur = conn.cursor()
        cur.execute("SET statement_timeout = '60s'")
        cur.execute(sql)
        cols = [d[0] for d in cur.description] if cur.description else []
        rows = cur.fetchmany(MAX_ROWS)
        cur.close()
        conn.close()

        if not cols:
            return "결과 없음."

        lines = [" | ".join(cols)]
        lines.append(" | ".join("---" for _ in cols))
        for row in rows:
            lines.append(" | ".join(str(v) if v is not None else "" for v in row))

        result = "\n".join(lines)
        if len(rows) == MAX_ROWS:
            result += f"\n\n(결과가 {MAX_ROWS}행으로 제한됨)"
        return result
    except Exception as e:
        return f"쿼리 오류: {e}"


# --- Tool implementations ---


def execute_tool(name: str, args: dict) -> str:
    if name == "query_adsb_db":
        return _execute_query(args.get("sql", ""))

    if name == "ingestion_stats":
        hours = args.get("hours", 24)
        return _execute_query(f"""
            SELECT date_generated,
                   EXTRACT(HOUR FROM time_generated)::int AS hour,
                   COUNT(*) AS row_count
            FROM adsb_message
            WHERE (date_generated + time_generated) >= (NOW() - {int(hours)} * INTERVAL '1 hour')
            GROUP BY date_generated, hour
            ORDER BY date_generated DESC, hour DESC
        """)

    if name == "recent_aircraft":
        minutes = args.get("minutes", 30)
        limit = min(args.get("limit", 20), 100)
        return _execute_query(f"""
            SELECT hex_ident, altitude, ground_speed, latitude, longitude,
                   is_on_ground, date_generated, time_generated
            FROM adsb_message
            WHERE (date_generated + time_generated) >= (NOW() - {int(minutes)} * INTERVAL '1 minute')
            ORDER BY date_generated DESC, time_generated DESC
            LIMIT {int(limit)}
        """)

    if name == "search_aircraft":
        hex_ident = args.get("hex_ident", "").strip().upper()
        hours = args.get("hours", 24)
        limit = min(args.get("limit", 50), 200)
        return _execute_query(f"""
            SELECT hex_ident, altitude, ground_speed, latitude, longitude,
                   is_on_ground, date_generated, time_generated
            FROM adsb_message
            WHERE hex_ident = '{hex_ident}'
              AND (date_generated + time_generated) >= (NOW() - {int(hours)} * INTERVAL '1 hour')
            ORDER BY date_generated DESC, time_generated DESC
            LIMIT {int(limit)}
        """)

    if name == "unique_aircraft":
        hours = args.get("hours", 24)
        return _execute_query(f"""
            SELECT hex_ident,
                   COUNT(*) AS msg_count,
                   MIN(altitude) AS min_alt,
                   MAX(altitude) AS max_alt,
                   ROUND(AVG(ground_speed)) AS avg_speed,
                   MIN(date_generated + time_generated) AS first_seen,
                   MAX(date_generated + time_generated) AS last_seen
            FROM adsb_message
            WHERE (date_generated + time_generated) >= (NOW() - {int(hours)} * INTERVAL '1 hour')
              AND hex_ident IS NOT NULL
            GROUP BY hex_ident
            ORDER BY msg_count DESC
            LIMIT 100
        """)

    if name == "high_altitude":
        min_alt = args.get("min_altitude", 35000)
        hours = args.get("hours", 24)
        return _execute_query(f"""
            SELECT DISTINCT ON (hex_ident)
                   hex_ident, altitude, ground_speed, latitude, longitude,
                   date_generated, time_generated
            FROM adsb_message
            WHERE altitude >= {int(min_alt)}
              AND (date_generated + time_generated) >= (NOW() - {int(hours)} * INTERVAL '1 hour')
            ORDER BY hex_ident, altitude DESC
            LIMIT 50
        """)

    if name == "ground_aircraft":
        minutes = args.get("minutes", 60)
        return _execute_query(f"""
            SELECT DISTINCT ON (hex_ident)
                   hex_ident, ground_speed, latitude, longitude,
                   date_generated, time_generated
            FROM adsb_message
            WHERE is_on_ground = true
              AND (date_generated + time_generated) >= (NOW() - {int(minutes)} * INTERVAL '1 minute')
            ORDER BY hex_ident, date_generated DESC, time_generated DESC
            LIMIT 50
        """)

    if name == "traffic_summary":
        days = min(args.get("days", 7), 30)
        return _execute_query(f"""
            SELECT date_generated,
                   COUNT(*) AS total_messages,
                   COUNT(DISTINCT hex_ident) AS unique_aircraft,
                   ROUND(AVG(altitude)) AS avg_altitude,
                   MAX(altitude) AS max_altitude,
                   ROUND(AVG(ground_speed)) AS avg_speed,
                   SUM(CASE WHEN is_on_ground THEN 1 ELSE 0 END) AS ground_count
            FROM adsb_message
            WHERE date_generated >= (CURRENT_DATE - {int(days)} * INTERVAL '1 day')
            GROUP BY date_generated
            ORDER BY date_generated DESC
        """)

    if name == "farthest_aircraft":
        hours = args.get("hours", 24)
        limit = min(args.get("limit", 10), 50)
        return _execute_query(f"""
            SELECT hex_ident, latitude, longitude, altitude,
                   date_generated, time_generated,
                   ROUND(
                     (6371 * ACOS(
                       COS(RADIANS({ANTENNA_LAT})) * COS(RADIANS(latitude))
                       * COS(RADIANS(longitude) - RADIANS({ANTENNA_LON}))
                       + SIN(RADIANS({ANTENNA_LAT})) * SIN(RADIANS(latitude))
                     ))::numeric, 2
                   ) AS distance_km
            FROM adsb_message
            WHERE latitude IS NOT NULL AND longitude IS NOT NULL
              AND (date_generated + time_generated) >= (NOW() - {int(hours)} * INTERVAL '1 hour')
            ORDER BY distance_km DESC
            LIMIT {int(limit)}
        """)

    if name == "speed_extremes":
        hours = args.get("hours", 24)
        return _execute_query(f"""
            (SELECT 'fastest' AS category, hex_ident, ground_speed, altitude,
                    latitude, longitude, date_generated, time_generated
             FROM adsb_message
             WHERE ground_speed IS NOT NULL
               AND (date_generated + time_generated) >= (NOW() - {int(hours)} * INTERVAL '1 hour')
             ORDER BY ground_speed DESC LIMIT 10)
            UNION ALL
            (SELECT 'slowest_airborne' AS category, hex_ident, ground_speed, altitude,
                    latitude, longitude, date_generated, time_generated
             FROM adsb_message
             WHERE ground_speed IS NOT NULL AND ground_speed > 0 AND is_on_ground = false
               AND (date_generated + time_generated) >= (NOW() - {int(hours)} * INTERVAL '1 hour')
             ORDER BY ground_speed ASC LIMIT 10)
        """)

    if name == "altitude_distribution":
        hours = args.get("hours", 24)
        return _execute_query(f"""
            SELECT
                CASE
                    WHEN altitude < 1000 THEN '0-1K'
                    WHEN altitude < 5000 THEN '1K-5K'
                    WHEN altitude < 10000 THEN '5K-10K'
                    WHEN altitude < 20000 THEN '10K-20K'
                    WHEN altitude < 30000 THEN '20K-30K'
                    WHEN altitude < 40000 THEN '30K-40K'
                    ELSE '40K+'
                END AS altitude_band,
                COUNT(DISTINCT hex_ident) AS aircraft_count,
                COUNT(*) AS message_count
            FROM adsb_message
            WHERE altitude IS NOT NULL
              AND (date_generated + time_generated) >= (NOW() - {int(hours)} * INTERVAL '1 hour')
            GROUP BY altitude_band
            ORDER BY MIN(altitude)
        """)

    if name == "describe_table":
        return _execute_query("""
            SELECT column_name, data_type, is_nullable
            FROM information_schema.columns
            WHERE table_schema = 'public' AND table_name = 'adsb_message'
            ORDER BY ordinal_position
        """)

    # --- External API tools ---

    if name == "lookup_aircraft":
        hex_ident = args.get("hex_ident", "").strip().upper()
        try:
            resp = httpx.get(f"https://hexdb.io/api/v1/aircraft/{hex_ident}", timeout=10)
            data = resp.json()
            if data.get("status") == "404":
                return f"{hex_ident}: 항공기 정보를 찾을 수 없습니다."
            lines = []
            for k, v in data.items():
                if v:
                    lines.append(f"- **{k}**: {v}")
            return "\n".join(lines) if lines else "정보 없음."
        except Exception as e:
            return f"hexdb.io 조회 오류: {e}"

    if name == "get_weather":
        location = args.get("location", "Seoul")
        lang = args.get("lang", "ko")
        try:
            resp = httpx.get(f"https://wttr.in/{location}?format=j1&lang={lang}", timeout=10)
            data = resp.json()
            cur = data["current_condition"][0]
            area = data.get("nearest_area", [{}])[0]
            area_name = area.get("areaName", [{}])[0].get("value", location)
            country = area.get("country", [{}])[0].get("value", "")

            weather = data.get("weather", [])
            forecast_lines = []
            for w in weather[:3]:
                date = w.get("date", "")
                mint = w.get("mintempC", "")
                maxt = w.get("maxtempC", "")
                desc = w.get("hourly", [{}])[4].get("weatherDesc", [{}])[0].get("value", "") if w.get("hourly") else ""
                forecast_lines.append(f"  {date}: {mint}~{maxt}°C, {desc}")

            return (
                f"위치: {area_name}, {country}\n"
                f"현재: {cur['temp_C']}°C (체감 {cur['FeelsLikeC']}°C)\n"
                f"습도: {cur['humidity']}%, 풍속: {cur['windspeedKmph']}km/h\n"
                f"상태: {cur['weatherDesc'][0]['value']}\n"
                f"시정: {cur['visibility']}km, 기압: {cur['pressure']}hPa\n"
                f"\n3일 예보:\n" + "\n".join(forecast_lines)
            )
        except Exception as e:
            return f"날씨 조회 오류: {e}"

    if name == "get_exchange_rate":
        base = args.get("base", "KRW").upper()
        targets = [t.strip().upper() for t in args.get("targets", "USD,JPY,EUR").split(",")]
        try:
            resp = httpx.get(f"https://api.exchangerate-api.com/v4/latest/{base}", timeout=10)
            data = resp.json()
            lines = [f"기준: 1 {base} (갱신: {data.get('date', 'N/A')})"]
            for t in targets:
                rate = data["rates"].get(t)
                if rate:
                    lines.append(f"  → {t}: {rate}")
                    if base == "KRW" and rate:
                        lines.append(f"    (1 {t} = {round(1/rate):,} KRW)")
                else:
                    lines.append(f"  → {t}: 없음")
            return "\n".join(lines)
        except Exception as e:
            return f"환율 조회 오류: {e}"

    if name == "web_fetch":
        url = args.get("url", "")
        if not url.startswith("http"):
            return "유효한 URL을 입력하세요 (http:// 또는 https://)"
        try:
            resp = httpx.get(url, timeout=15, follow_redirects=True)
            content = resp.text[:3000]
            return f"HTTP {resp.status_code}\n\n{content}"
        except Exception as e:
            return f"요청 오류: {e}"

    if name == "reverse_geocode":
        lat = args.get("lat")
        lon = args.get("lon")
        if lat is None or lon is None:
            return "위도(lat)와 경도(lon)를 모두 입력하세요."
        lang = args.get("lang", "ko")
        zoom = args.get("zoom", 14)
        try:
            resp = httpx.get(
                "https://nominatim.openstreetmap.org/reverse",
                params={"format": "json", "lat": lat, "lon": lon, "zoom": zoom, "accept-language": lang},
                headers={"User-Agent": "gemma-chat/1.0"},
                timeout=10,
            )
            data = resp.json()
            if "error" in data:
                return f"결과 없음: {data['error']}"
            addr = data.get("address", {})
            lines = [f"**{data.get('display_name', '')}**", ""]
            for k, v in addr.items():
                lines.append(f"- {k}: {v}")
            return "\n".join(lines)
        except Exception as e:
            return f"역지오코딩 오류: {e}"

    return f"알 수 없는 도구: {name}"


# --- Ollama tool definitions ---

TOOL_DEFINITIONS = [
    {
        "type": "function",
        "function": {
            "name": "query_adsb_db",
            "description": "RPI4 PostgreSQL에서 읽기 전용 SQL 쿼리를 실행합니다. 테이블: adsb_message (id, message_type, transmission_type, session_id, aircraft_id, hex_ident, flight_id, date_generated, time_generated, altitude, ground_speed, latitude, longitude, is_on_ground, received_at). 현재 약 1천만 행. SELECT만 허용됩니다. 다른 도구로 충분하지 않을 때 사용하세요.",
            "parameters": {
                "type": "object",
                "properties": {
                    "sql": {
                        "type": "string",
                        "description": "실행할 SELECT SQL 쿼리",
                    }
                },
                "required": ["sql"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "ingestion_stats",
            "description": "시간별 ADS-B 메시지 수집 건수를 조회합니다.",
            "parameters": {
                "type": "object",
                "properties": {
                    "hours": {
                        "type": "integer",
                        "description": "조회할 시간 범위 (기본: 24)",
                    }
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "recent_aircraft",
            "description": "최근 N분 동안 수신된 항공기 위치 데이터를 조회합니다.",
            "parameters": {
                "type": "object",
                "properties": {
                    "minutes": {
                        "type": "integer",
                        "description": "조회할 분 범위 (기본: 30)",
                    },
                    "limit": {
                        "type": "integer",
                        "description": "최대 결과 수 (기본: 20, 최대: 100)",
                    },
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_aircraft",
            "description": "특정 항공기(hex_ident)의 위치 이력을 조회합니다. 항공기 경로 추적에 유용합니다.",
            "parameters": {
                "type": "object",
                "properties": {
                    "hex_ident": {
                        "type": "string",
                        "description": "ICAO 24-bit 주소 (예: 71BE07, A1B2C3)",
                    },
                    "hours": {
                        "type": "integer",
                        "description": "조회 범위 시간 (기본: 24)",
                    },
                    "limit": {
                        "type": "integer",
                        "description": "최대 결과 수 (기본: 50, 최대: 200)",
                    },
                },
                "required": ["hex_ident"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "unique_aircraft",
            "description": "최근 N시간 동안 관측된 고유 항공기 목록과 각각의 메시지 수, 고도 범위, 평균 속도, 최초/최종 관측 시각을 보여줍니다.",
            "parameters": {
                "type": "object",
                "properties": {
                    "hours": {
                        "type": "integer",
                        "description": "조회 범위 시간 (기본: 24)",
                    }
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "high_altitude",
            "description": "지정 고도 이상에서 비행 중인 항공기를 조회합니다.",
            "parameters": {
                "type": "object",
                "properties": {
                    "min_altitude": {
                        "type": "integer",
                        "description": "최소 고도 (피트, 기본: 35000)",
                    },
                    "hours": {
                        "type": "integer",
                        "description": "조회 범위 시간 (기본: 24)",
                    },
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "ground_aircraft",
            "description": "지상에 있는 항공기(is_on_ground=true)를 조회합니다. 공항 활동 파악에 유용합니다.",
            "parameters": {
                "type": "object",
                "properties": {
                    "minutes": {
                        "type": "integer",
                        "description": "조회 범위 분 (기본: 60)",
                    }
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "traffic_summary",
            "description": "일별 트래픽 요약 통계입니다. 총 메시지 수, 고유 항공기 수, 평균/최대 고도, 평균 속도, 지상 메시지 수를 보여줍니다.",
            "parameters": {
                "type": "object",
                "properties": {
                    "days": {
                        "type": "integer",
                        "description": "조회할 일수 (기본: 7, 최대: 30)",
                    }
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "farthest_aircraft",
            "description": "안테나 기준 가장 멀리서 관측된 항공기를 Haversine 거리로 조회합니다.",
            "parameters": {
                "type": "object",
                "properties": {
                    "hours": {
                        "type": "integer",
                        "description": "조회 범위 시간 (기본: 24)",
                    },
                    "limit": {
                        "type": "integer",
                        "description": "결과 수 (기본: 10, 최대: 50)",
                    },
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "speed_extremes",
            "description": "최근 N시간 내 가장 빠른 항공기 10개와 가장 느린 비행 중 항공기 10개를 조회합니다.",
            "parameters": {
                "type": "object",
                "properties": {
                    "hours": {
                        "type": "integer",
                        "description": "조회 범위 시간 (기본: 24)",
                    }
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "altitude_distribution",
            "description": "고도 구간별(0-1K, 1K-5K, ..., 40K+) 항공기 수와 메시지 수 분포를 보여줍니다.",
            "parameters": {
                "type": "object",
                "properties": {
                    "hours": {
                        "type": "integer",
                        "description": "조회 범위 시간 (기본: 24)",
                    }
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "describe_table",
            "description": "adsb_message 테이블의 스키마(컬럼명, 타입, nullable)를 조회합니다.",
            "parameters": {
                "type": "object",
                "properties": {},
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "lookup_aircraft",
            "description": "ICAO hex 코드로 항공기 상세 정보를 조회합니다 (hexdb.io). 등록번호, 제조사, 기종, 운영사 등을 반환합니다. 예: 71BA12 → HL7212, Air Seoul, A321.",
            "parameters": {
                "type": "object",
                "properties": {
                    "hex_ident": {
                        "type": "string",
                        "description": "ICAO 24-bit 주소 (예: 71BA12)",
                    }
                },
                "required": ["hex_ident"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_weather",
            "description": "지정 위치의 현재 날씨와 3일 예보를 조회합니다. 도시명 또는 좌표로 검색 가능합니다.",
            "parameters": {
                "type": "object",
                "properties": {
                    "location": {
                        "type": "string",
                        "description": "도시명 (예: Seoul, Tokyo, 37.4,127.0) 기본: Seoul",
                    },
                    "lang": {
                        "type": "string",
                        "description": "언어 코드 (기본: ko)",
                    },
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_exchange_rate",
            "description": "환율 정보를 조회합니다. 기준 통화 대비 목표 통화의 환율을 반환합니다.",
            "parameters": {
                "type": "object",
                "properties": {
                    "base": {
                        "type": "string",
                        "description": "기준 통화 (기본: KRW)",
                    },
                    "targets": {
                        "type": "string",
                        "description": "목표 통화 (쉼표 구분, 기본: USD,JPY,EUR)",
                    },
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "web_fetch",
            "description": "URL의 내용을 가져옵니다. 공개 웹페이지나 API 응답을 읽을 수 있습니다. 최대 3000자까지 반환됩니다.",
            "parameters": {
                "type": "object",
                "properties": {
                    "url": {
                        "type": "string",
                        "description": "가져올 URL (http:// 또는 https://)",
                    }
                },
                "required": ["url"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "reverse_geocode",
            "description": "위도/경도 좌표를 주소로 변환합니다 (역지오코딩). 항공기 위치를 사람이 읽을 수 있는 지역명으로 알려줍니다. 예: (37.53, 127.63) → 경기도 양평군 용문면.",
            "parameters": {
                "type": "object",
                "properties": {
                    "lat": {
                        "type": "number",
                        "description": "위도",
                    },
                    "lon": {
                        "type": "number",
                        "description": "경도",
                    },
                    "lang": {
                        "type": "string",
                        "description": "결과 언어 (기본: ko)",
                    },
                    "zoom": {
                        "type": "integer",
                        "description": "상세도 (3=국가, 10=도시, 14=동네, 18=건물, 기본: 14)",
                    },
                },
                "required": ["lat", "lon"],
            },
        },
    },
]
