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


def _execute_query(sql: str, params: tuple = None, as_dicts: bool = False):
    sql_upper = sql.upper()
    for kw in BLOCKED_KEYWORDS:
        if kw in sql_upper:
            msg = f"차단됨: {kw} 구문은 사용할 수 없습니다 (읽기 전용)."
            return ([], msg) if as_dicts else msg

    try:
        conn = psycopg2.connect(**DB_CONFIG)
        conn.set_session(readonly=True, autocommit=True)
        cur = conn.cursor()
        cur.execute("SET statement_timeout = '60s'")
        cur.execute(sql, params)
        cols = [d[0] for d in cur.description] if cur.description else []
        rows = cur.fetchmany(MAX_ROWS)
        cur.close()
        conn.close()

        if not cols:
            return ([], "결과 없음.") if as_dicts else "결과 없음."

        if as_dicts:
            return [dict(zip(cols, row)) for row in rows], None

        lines = [" | ".join(cols)]
        lines.append(" | ".join("---" for _ in cols))
        for row in rows:
            lines.append(" | ".join(str(v) if v is not None else "" for v in row))

        result = "\n".join(lines)
        if len(rows) == MAX_ROWS:
            result += f"\n\n(결과가 {MAX_ROWS}행으로 제한됨)"
        return result
    except Exception as e:
        msg = f"쿼리 오류: {e}"
        return ([], msg) if as_dicts else msg


def _fmt_num(n):
    """Format number with comma separators."""
    if n is None:
        return "-"
    try:
        return f"{int(n):,}"
    except (ValueError, TypeError):
        return str(n)


def _fmt_time(ts):
    """Extract HH:MM from timestamp."""
    if ts is None:
        return "-"
    s = str(ts)
    if " " in s:
        s = s.split(" ")[1]
    return s[:5]


# --- Tool implementations ---


def execute_tool(name: str, args: dict, pretty: bool = False) -> str:
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
        minutes = args.get("minutes", 60)
        limit = min(args.get("limit", 20), 100)
        sql = f"""
            SELECT DISTINCT ON (hex_ident)
                   hex_ident,
                   altitude,
                   ground_speed,
                   latitude,
                   longitude,
                   is_on_ground,
                   (date_generated + time_generated) + INTERVAL '9 hours' AS last_seen_kst
            FROM adsb_message
            WHERE (date_generated + time_generated) >= (NOW() - {int(minutes)} * INTERVAL '1 minute')
              AND hex_ident IS NOT NULL
            ORDER BY hex_ident, date_generated DESC, time_generated DESC
        """
        # 외부 쿼리로 last_seen 정렬 + limit
        sql = f"SELECT * FROM ({sql}) sub ORDER BY last_seen_kst DESC LIMIT {int(limit)}"
        if pretty:
            rows, err = _execute_query(sql, as_dicts=True)
            if err:
                return err
            if not rows:
                return f"최근 {minutes}분 내 관측된 항공기가 없습니다."
            lines = [f"### 최근 {minutes}분 고유 항공기 — {len(rows)}대\n"]
            lines.append("| # | hex | 고도(ft) | 속도(kts) | 위도 | 경도 | 최종(KST) | 비고 |")
            lines.append("|---|-----|----------|-----------|------|------|-----------|------|")
            for i, r in enumerate(rows, 1):
                alt = _fmt_num(r.get("altitude"))
                spd = _fmt_num(r.get("ground_speed"))
                lat = r.get("latitude") or "-"
                lon = r.get("longitude") or "-"
                t = _fmt_time(r.get("last_seen_kst"))
                note = "지상" if r.get("is_on_ground") else ""
                lines.append(f"| {i} | `{r.get('hex_ident','-')}` | {alt} | {spd} | {lat} | {lon} | {t} | {note} |")
            return "\n".join(lines)
        return _execute_query(sql)

    if name == "search_aircraft":
        hex_ident = args.get("hex_ident", "").strip().upper()
        hours = args.get("hours", 24)
        sql = """
            SELECT hex_ident,
                   DATE_TRUNC('hour', date_generated + time_generated + INTERVAL '9 hours')
                     + INTERVAL '10 min' * FLOOR(EXTRACT(MINUTE FROM date_generated + time_generated) / 10)
                     AS time_slot_kst,
                   (ARRAY_AGG(altitude ORDER BY date_generated DESC, time_generated DESC))[1] AS altitude,
                   (ARRAY_AGG(ground_speed ORDER BY date_generated DESC, time_generated DESC))[1] AS ground_speed,
                   (ARRAY_AGG(latitude ORDER BY date_generated DESC, time_generated DESC))[1] AS latitude,
                   (ARRAY_AGG(longitude ORDER BY date_generated DESC, time_generated DESC))[1] AS longitude,
                   (ARRAY_AGG(is_on_ground ORDER BY date_generated DESC, time_generated DESC))[1] AS is_on_ground,
                   COUNT(*) AS msg_count
            FROM adsb_message
            WHERE hex_ident = %s
              AND (date_generated + time_generated) >= (NOW() - %s * INTERVAL '1 hour')
            GROUP BY hex_ident, time_slot_kst
            ORDER BY time_slot_kst DESC
            LIMIT 6
        """
        params = (hex_ident, int(hours))
        if pretty:
            rows, err = _execute_query(sql, params=params, as_dicts=True)
            if err:
                return err
            if not rows:
                return f"{hex_ident}: 최근 {hours}시간 내 데이터 없음"
            lines = [f"### {hex_ident} 위치 이력 (10분 단위, KST, 최근 {hours}시간)\n"]
            lines.append("| 시간(KST) | 고도(ft) | 속도(kts) | 위도 | 경도 | 건수 | 비고 |")
            lines.append("|-----------|----------|-----------|------|------|------|------|")
            for r in rows:
                slot = str(r.get("time_slot_kst", "-"))
                if " " in slot:
                    slot = slot.split(" ")[1][:5]
                alt = _fmt_num(r.get("altitude"))
                spd = _fmt_num(r.get("ground_speed"))
                lat = r.get("latitude") or "-"
                lon = r.get("longitude") or "-"
                cnt = _fmt_num(r.get("msg_count"))
                note = "지상" if r.get("is_on_ground") else ""
                lines.append(f"| {slot} | {alt} | {spd} | {lat} | {lon} | {cnt} | {note} |")
            return "\n".join(lines)
        return _execute_query(sql, params=params)

    if name == "unique_aircraft":
        hours = args.get("hours", 24)
        sql = f"""
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
        """
        if pretty:
            rows, err = _execute_query(sql, as_dicts=True)
            if err:
                return err
            if not rows:
                return f"최근 {hours}시간 내 관측된 항공기가 없습니다."
            lines = [f"### 최근 {hours}시간 항공기 현황 — 총 {len(rows)}대\n"]
            for i, r in enumerate(rows[:20], 1):
                cnt = _fmt_num(r.get("msg_count"))
                lo = _fmt_num(r.get("min_alt"))
                hi = _fmt_num(r.get("max_alt"))
                spd = _fmt_num(r.get("avg_speed"))
                t1 = _fmt_time(r.get("first_seen"))
                t2 = _fmt_time(r.get("last_seen"))
                lines.append(f"**{i}.** `{r.get('hex_ident','-')}` — {cnt}건, 고도 {lo}~{hi}ft, {spd}kts ({t1}~{t2})")
            if len(rows) > 20:
                lines.append(f"\n*...외 {len(rows)-20}대 더*")
            return "\n".join(lines)
        return _execute_query(sql)

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
        sql = f"""
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
        """
        if pretty:
            rows, err = _execute_query(sql, as_dicts=True)
            if err:
                return err
            if not rows:
                return f"최근 {days}일 트래픽 데이터가 없습니다."
            lines = [f"### 최근 {days}일 트래픽 요약\n"]
            for r in rows:
                d = str(r.get("date_generated", "-"))
                msgs = _fmt_num(r.get("total_messages"))
                planes = _fmt_num(r.get("unique_aircraft"))
                avg_alt = _fmt_num(r.get("avg_altitude"))
                max_alt = _fmt_num(r.get("max_altitude"))
                lines.append(f"**{d}** — {msgs}건, {planes}대, 평균고도 {avg_alt}ft, 최대 {max_alt}ft")
            return "\n".join(lines)
        return _execute_query(sql)

    if name == "farthest_aircraft":
        hours = args.get("hours", 24)
        limit = min(args.get("limit", 10), 50)
        sql = f"""
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
        """
        if pretty:
            rows, err = _execute_query(sql, as_dicts=True)
            if err:
                return err
            if not rows:
                return f"최근 {hours}시간 내 위치 데이터가 없습니다."
            lines = [f"### 최근 {hours}시간 가장 먼 항공기 — Top {len(rows)}\n"]
            for i, r in enumerate(rows, 1):
                dist = r.get("distance_km", "-")
                alt = _fmt_num(r.get("altitude"))
                t = _fmt_time(r.get("time_generated"))
                lat = r.get("latitude", "-")
                lon = r.get("longitude", "-")
                lines.append(f"**{i}.** `{r.get('hex_ident','-')}` — **{dist}km**, {alt}ft, ({lat}, {lon}), {t}")
            return "\n".join(lines)
        return _execute_query(sql)

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

    if name == "nearby_aircraft":
        lat = args.get("lat", ANTENNA_LAT)
        lon = args.get("lon", ANTENNA_LON)
        radius_km = min(args.get("radius_km", 50), 500)
        hours = args.get("hours", 24)
        limit = min(args.get("limit", 20), 100)
        return _execute_query(f"""
            SELECT * FROM (
                SELECT hex_ident, latitude, longitude, altitude, ground_speed,
                       date_generated, time_generated,
                       ROUND(
                         (6371 * ACOS(
                           COS(RADIANS({float(lat)})) * COS(RADIANS(latitude))
                           * COS(RADIANS(longitude) - RADIANS({float(lon)}))
                           + SIN(RADIANS({float(lat)})) * SIN(RADIANS(latitude))
                         ))::numeric, 2
                       ) AS distance_km
                FROM adsb_message
                WHERE latitude IS NOT NULL AND longitude IS NOT NULL
                  AND (date_generated + time_generated) >= (NOW() - {int(hours)} * INTERVAL '1 hour')
            ) sub
            WHERE distance_km <= {float(radius_km)}
            ORDER BY distance_km ASC
            LIMIT {int(limit)}
        """)

    if name == "rapid_altitude_change":
        hours = args.get("hours", 6)
        min_change = args.get("min_change_ft", 10000)
        limit = min(args.get("limit", 20), 50)
        return _execute_query(f"""
            SELECT hex_ident,
                   MIN(altitude) AS min_alt,
                   MAX(altitude) AS max_alt,
                   MAX(altitude) - MIN(altitude) AS alt_range_ft,
                   COUNT(*) AS msg_count,
                   MIN(date_generated + time_generated) AS first_seen,
                   MAX(date_generated + time_generated) AS last_seen
            FROM adsb_message
            WHERE altitude IS NOT NULL
              AND hex_ident IS NOT NULL
              AND (date_generated + time_generated) >= (NOW() - {int(hours)} * INTERVAL '1 hour')
            GROUP BY hex_ident
            HAVING MAX(altitude) - MIN(altitude) >= {int(min_change)}
            ORDER BY alt_range_ft DESC
            LIMIT {int(limit)}
        """)

    if name == "flight_duration":
        hours = args.get("hours", 24)
        order = "DESC" if args.get("longest", True) else "ASC"
        limit = min(args.get("limit", 20), 50)
        return _execute_query(f"""
            SELECT hex_ident,
                   MIN(date_generated + time_generated) AS first_seen,
                   MAX(date_generated + time_generated) AS last_seen,
                   EXTRACT(EPOCH FROM MAX(date_generated + time_generated)
                           - MIN(date_generated + time_generated))::int / 60 AS duration_min,
                   COUNT(*) AS msg_count,
                   ROUND(AVG(altitude)) AS avg_altitude,
                   ROUND(AVG(ground_speed)) AS avg_speed
            FROM adsb_message
            WHERE hex_ident IS NOT NULL
              AND (date_generated + time_generated) >= (NOW() - {int(hours)} * INTERVAL '1 hour')
            GROUP BY hex_ident
            HAVING COUNT(*) >= 5
            ORDER BY duration_min {order}
            LIMIT {int(limit)}
        """)

    if name == "busiest_hours":
        days = min(args.get("days", 7), 30)
        return _execute_query(f"""
            SELECT EXTRACT(HOUR FROM time_generated)::int AS hour,
                   COUNT(*) AS total_messages,
                   COUNT(DISTINCT hex_ident) AS unique_aircraft,
                   ROUND(AVG(altitude)) AS avg_altitude
            FROM adsb_message
            WHERE date_generated >= (CURRENT_DATE - {int(days)} * INTERVAL '1 day')
            GROUP BY hour
            ORDER BY total_messages DESC
        """)

    if name == "korean_aircraft":
        hours = args.get("hours", 24)
        limit = min(args.get("limit", 30), 100)
        return _execute_query(f"""
            SELECT hex_ident,
                   COUNT(*) AS msg_count,
                   MIN(altitude) AS min_alt,
                   MAX(altitude) AS max_alt,
                   ROUND(AVG(ground_speed)) AS avg_speed,
                   MIN(date_generated + time_generated) AS first_seen,
                   MAX(date_generated + time_generated) AS last_seen
            FROM adsb_message
            WHERE hex_ident LIKE '71%'
              AND hex_ident IS NOT NULL
              AND (date_generated + time_generated) >= (NOW() - {int(hours)} * INTERVAL '1 hour')
            GROUP BY hex_ident
            ORDER BY msg_count DESC
            LIMIT {int(limit)}
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
                forecast_lines.append(f"  {date}: {mint}\~{maxt}°C, {desc}")

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

    return f"알 수 없는 도구: {name}"


# --- Ollama tool definitions ---

TOOL_DEFINITIONS = [
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
]
