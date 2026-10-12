import json
import os
import re
import time
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from contextlib import closing
from typing import Optional
from threading import Lock
from zoneinfo import ZoneInfo
from datetime import datetime, timezone
from datetime import datetime, timezone
from datetime import datetime, timezone

import psycopg2
from psycopg2 import sql
from psycopg2.extras import RealDictCursor, Json, Json, Json
from dotenv import load_dotenv
from fastapi import BackgroundTasks, FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware

load_dotenv()

app = FastAPI(
    title="WebGIS Banjir Makassar API",
    description="API awal untuk membaca layer spasial dari PostgreSQL/PostGIS dan mengirimkannya sebagai GeoJSON.",
    version="0.1.0",
)

# Untuk pengembangan lokal. Batasi origin secara spesifik sebelum dipublikasikan.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)


def get_connection():
    """Buka koneksi baru ke PostgreSQL berdasarkan variabel di .env."""
    try:
        return psycopg2.connect(
            host=os.getenv("DB_HOST", "localhost"),
            port=int(os.getenv("DB_PORT", "5432")),
            dbname=os.getenv("DB_NAME", "webgis2"),
            user=os.getenv("DB_USER", "postgres"),
            password=os.getenv("DB_PASSWORD", ""),
            connect_timeout=5,
        )
    except Exception as exc:
        # Jangan kirim detail koneksi/password ke browser.
        raise HTTPException(
            status_code=503,
            detail="Tidak dapat terhubung ke PostgreSQL. Periksa service PostgreSQL dan konfigurasi file .env.",
        ) from exc


@app.get("/")
def root():
    return {
        "message": "WebGIS Banjir Makassar API aktif",
        "docs": "/docs",
        "health": "/health",
        "layers": "/api/layers",
    }


@app.get("/health")
def health_check():
    """Tes koneksi database dan status ekstensi PostGIS."""
    with closing(get_connection()) as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("SELECT current_database() AS database, PostGIS_Full_Version() AS postgis_version;")
            row = cur.fetchone()
    return {"status": "ok", "database": row["database"], "postgis": row["postgis_version"]}


@app.get("/api/layers")
def list_spatial_layers():
    """Daftar semua tabel spasial yang terdaftar di metadata PostGIS."""
    query = """
        SELECT f_table_schema AS schema,
               f_table_name AS table,
               f_geometry_column AS geometry_column,
               srid,
               type AS geometry_type,
               coord_dimension
        FROM geometry_columns
        WHERE f_table_schema NOT IN ('pg_catalog', 'information_schema')
        ORDER BY f_table_schema, f_table_name;
    """
    with closing(get_connection()) as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(query)
            rows = cur.fetchall()
    return {"count": len(rows), "layers": rows}


@app.get("/api/layers/{schema_name}/{table_name}")
def get_layer_geojson(
    schema_name: str,
    table_name: str,
    limit: int = Query(default=1000, ge=1, le=5000),
    offset: int = Query(default=0, ge=0),
    bbox: Optional[str] = Query(default=None, description="Batas peta EPSG:4326: min_lon,min_lat,max_lon,max_lat"),
):
    """Ambil fitur suatu tabel spasial sebagai GeoJSON. Limit awal dibatasi agar tidak membebani browser."""
    with closing(get_connection()) as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            # Validasi nama tabel/schema menggunakan katalog PostGIS; jangan percaya nama dari URL begitu saja.
            cur.execute(
                """
                SELECT f_table_schema, f_table_name, f_geometry_column, srid
                FROM geometry_columns
                WHERE f_table_schema = %s AND f_table_name = %s
                LIMIT 1;
                """,
                (schema_name, table_name),
            )
            meta = cur.fetchone()
            if not meta:
                raise HTTPException(
                    status_code=404,
                    detail="Tabel spasial tidak ditemukan di geometry_columns. Periksa schema, nama tabel, dan kolom geometri.",
                )

            geom_col = meta["f_geometry_column"]
            srid = int(meta["srid"] or 0)
            if srid <= 0:
                raise HTTPException(
                    status_code=400,
                    detail="Layer ini tidak memiliki SRID yang valid. Tetapkan CRS yang benar di PostGIS sebelum ditampilkan.",
                )

            where_clause = sql.SQL("{} IS NOT NULL").format(sql.Identifier(geom_col))
            params = [geom_col]

            if bbox is not None:
                try:
                    coords = [float(part.strip()) for part in bbox.split(",")]
                    if len(coords) != 4:
                        raise ValueError
                    minx, miny, maxx, maxy = coords
                    if minx >= maxx or miny >= maxy:
                        raise ValueError
                except ValueError as exc:
                    raise HTTPException(
                        status_code=400,
                        detail="Format bbox salah. Gunakan min_lon,min_lat,max_lon,max_lat, misalnya 119.3,-5.3,119.6,-5.0.",
                    ) from exc

                where_clause += sql.SQL(" AND ST_Intersects({}, ST_Transform(ST_MakeEnvelope(%s, %s, %s, %s, 4326), %s))").format(
                    sql.Identifier(geom_col)
                )
                params.extend([minx, miny, maxx, maxy, srid])

            query = sql.SQL(
                "SELECT ST_AsGeoJSON(ST_Transform({geom}, 4326)) AS geojson, "
                "(to_jsonb(t) - %s) AS properties "
                "FROM {schema}.{table} AS t "
                "WHERE {where_clause} "
                "ORDER BY 1 NULLS LAST LIMIT %s OFFSET %s"
            ).format(
                geom=sql.Identifier(geom_col),
                schema=sql.Identifier(schema_name),
                table=sql.Identifier(table_name),
                where_clause=where_clause,
            )
            params.extend([limit, offset])
            cur.execute(query, tuple(params))
            rows = cur.fetchall()

    features = []
    for row in rows:
        geometry_text = row["geojson"]
        if not geometry_text:
            continue
        geometry = json.loads(geometry_text) if isinstance(geometry_text, str) else geometry_text
        features.append({
            "type": "Feature",
            "geometry": geometry,
            "properties": row["properties"] or {},
        })

    return {
        "type": "FeatureCollection",
        "name": f"{schema_name}.{table_name}",
        "count_returned": len(features),
        "limit": limit,
        "offset": offset,
        "features": features,
    }

# Cache prakiraan BMKG per kode kelurahan selama 10 menit untuk menghindari
# permintaan berulang. Cache ini hanya berada di memori proses backend.

_BMKG_CACHE = {}
_BMKG_CACHE_TTL_SECONDS = 600


def _flatten_bmkg_forecasts(value):
    """Ratakan struktur list cuaca BMKG yang dikelompokkan per periode."""
    rows = []
    if isinstance(value, dict):
        if any(key in value for key in ("local_datetime", "utc_datetime", "datetime")):
            rows.append(value)
        else:
            for child in value.values():
                rows.extend(_flatten_bmkg_forecasts(child))
    elif isinstance(value, list):
        for child in value:
            rows.extend(_flatten_bmkg_forecasts(child))
    return rows


@app.get("/api/weather/bmkg")
def bmkg_weather_forecast(
    adm4: str = Query(..., description="Kode wilayah administrasi kelurahan/desa tingkat IV, misalnya 73.71.15.1003"),
):
    """Ambil prakiraan cuaca BMKG per 3 jam. tp ditampilkan sebagai nilai prakiraan presipitasi (mm), bukan observasi stasiun."""
    adm4 = adm4.strip()
    if not re.fullmatch(r"\d{2}\.\d{2}\.\d{2}\.\d{4}", adm4):
        raise HTTPException(
            status_code=400,
            detail="Format adm4 tidak valid. Gunakan kode wilayah tingkat kelurahan/desa, misalnya 73.71.15.1003.",
        )

    now = time.monotonic()
    cached = _BMKG_CACHE.get(adm4)
    if cached and now - cached["saved_at"] < _BMKG_CACHE_TTL_SECONDS:
        return cached["payload"]

    source_url = f"https://api.bmkg.go.id/publik/prakiraan-cuaca?adm4={adm4}"
    request = Request(
        source_url,
        headers={"User-Agent": "WebGIS-Banjir-Makassar/1.0", "Accept": "application/json"},
        method="GET",
    )
    try:
        with urlopen(request, timeout=15) as response:
            raw_bytes = response.read()
        source_data = json.loads(raw_bytes.decode("utf-8"))
    except HTTPError as exc:
        raise HTTPException(status_code=502, detail=f"API BMKG mengembalikan HTTP {exc.code}.") from exc
    except URLError as exc:
        raise HTTPException(status_code=502, detail="Backend tidak berhasil mengakses API BMKG. Periksa koneksi internet.") from exc
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise HTTPException(status_code=502, detail="Respons API BMKG tidak dapat dibaca sebagai JSON.") from exc

    top_location = source_data.get("lokasi") or {}
    blocks = source_data.get("data") or []
    raw_forecasts = []
    for block in blocks:
        if not isinstance(block, dict):
            continue
        if not top_location and isinstance(block.get("lokasi"), dict):
            top_location = block["lokasi"]
        raw_forecasts.extend(_flatten_bmkg_forecasts(block.get("cuaca", [])))

    if not raw_forecasts:
        raise HTTPException(status_code=502, detail="Data prakiraan BMKG kosong atau strukturnya berubah.")

    forecasts = []
    for item in raw_forecasts:
        forecasts.append({
            "datetime_local": item.get("local_datetime"),
            "datetime_utc": item.get("utc_datetime") or item.get("datetime"),
            "condition": item.get("weather_desc"),
            "temperature_c": item.get("t"),
            "precipitation_mm": item.get("tp"),
            "humidity_percent": item.get("hu"),
            "wind_speed_kmh": item.get("ws"),
            "wind_direction": item.get("wd"),
            "cloud_cover_percent": item.get("tcc"),
            "visibility": item.get("vs_text"),
            "analysis_date": item.get("analysis_date"),
        })

    payload = {
        "source": "BMKG",
        "source_url": source_url,
        "retrieved_at_epoch": int(time.time()),
        "cache_seconds": _BMKG_CACHE_TTL_SECONDS,
        "adm4": adm4,
        "location": {
            "province": top_location.get("provinsi"),
            "city": top_location.get("kotkab"),
            "district": top_location.get("kecamatan"),
            "village": top_location.get("desa"),
            "longitude": top_location.get("lon"),
            "latitude": top_location.get("lat"),
            "timezone": top_location.get("timezone"),
        },
        "forecast_count": len(forecasts),
        "forecast": forecasts,
        "note": "Ini prakiraan BMKG, bukan pengamatan curah hujan aktual dari alat ukur. Field tp berasal dari respons prakiraan BMKG.",
    }
    _BMKG_CACHE[adm4] = {"saved_at": now, "payload": payload}
    return payload


# Penyimpanan prakiraan BMKG ke PostgreSQL.
# Skema ini bersifat aditif: jika weather.weather_forecasts sudah ada, kolom yang
# diperlukan ditambahkan tanpa menghapus kolom atau baris yang sudah ada.
def _ensure_weather_forecasts_table(conn):
    with conn.cursor() as cur:
        cur.execute("CREATE SCHEMA IF NOT EXISTS weather;")
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS weather.weather_forecasts (
                id BIGSERIAL PRIMARY KEY,
                source TEXT NOT NULL DEFAULT 'BMKG',
                adm4 VARCHAR(20) NOT NULL,
                province TEXT,
                city TEXT,
                district TEXT,
                village TEXT,
                forecast_datetime_local TIMESTAMP WITHOUT TIME ZONE NOT NULL,
                forecast_datetime_utc TIMESTAMPTZ,
                retrieved_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                weather_condition TEXT,
                temperature_c DOUBLE PRECISION,
                precipitation_mm DOUBLE PRECISION,
                humidity_percent DOUBLE PRECISION,
                wind_speed_kmh DOUBLE PRECISION,
                wind_direction TEXT,
                cloud_cover_percent DOUBLE PRECISION,
                visibility TEXT,
                analysis_date_utc TIMESTAMPTZ,
                raw_data JSONB
            );
            """
        )
        additions = {
            "source": "TEXT DEFAULT 'BMKG'",
            "adm4": "VARCHAR(20)",
            "province": "TEXT",
            "city": "TEXT",
            "district": "TEXT",
            "village": "TEXT",
            "forecast_datetime_local": "TIMESTAMP WITHOUT TIME ZONE",
            "forecast_datetime_utc": "TIMESTAMPTZ",
            "retrieved_at": "TIMESTAMPTZ DEFAULT NOW()",
            "weather_condition": "TEXT",
            "temperature_c": "DOUBLE PRECISION",
            "precipitation_mm": "DOUBLE PRECISION",
            "humidity_percent": "DOUBLE PRECISION",
            "wind_speed_kmh": "DOUBLE PRECISION",
            "wind_direction": "TEXT",
            "cloud_cover_percent": "DOUBLE PRECISION",
            "visibility": "TEXT",
            "analysis_date_utc": "TIMESTAMPTZ",
            "raw_data": "JSONB",
        }
        for col, typ in additions.items():
            cur.execute(
                sql.SQL("ALTER TABLE weather.weather_forecasts ADD COLUMN IF NOT EXISTS {} " + typ).format(sql.Identifier(col))
            )
    conn.commit()

def _parse_forecast_datetime(value, assume_utc=False):
    if not value:
        return None
    text_value = str(value).strip()
    if text_value.endswith("Z"):
        text_value = text_value[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(text_value)
    except ValueError:
        return None
    if assume_utc:
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    # local_datetime dari BMKG adalah waktu dinding lokal; simpan tanpa timezone.
    if dt.tzinfo is not None:
        dt = dt.replace(tzinfo=None)
    return dt

def _numeric_or_none(value):
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None

@app.post("/api/weather/bmkg/save")
def save_bmkg_forecast(
    adm4: str = Query(..., description="Kode wilayah administrasi kelurahan/desa, contoh 73.71.15.1003"),
):
    """Ambil prakiraan BMKG dan simpan/update ke weather.weather_forecasts.

    Pasangan ADM4 + waktu lokal dipakai untuk menemukan baris sebelumnya. Kolom
    lazim dari rancangan tabel lama (forecast_time, rainfall_mm, temp_c, dll.) juga
    diisi jika tersedia, agar kompatibel dengan schema awal database.
    """
    payload = bmkg_weather_forecast(adm4=adm4)
    location = payload.get("location") or {}
    forecasts = payload.get("forecast") or []
    inserted = 0
    updated = 0
    skipped = 0
    location_full = ", ".join(str(location.get(k)) for k in ("village", "district", "city", "province") if location.get(k))

    with closing(get_connection()) as conn:
        _ensure_weather_forecasts_table(conn)
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                """
                SELECT column_name, is_nullable, column_default, is_identity
                FROM information_schema.columns
                WHERE table_schema='weather' AND table_name='weather_forecasts'
                """
            )
            column_info = cur.fetchall()
            available = {row["column_name"] for row in column_info}
            required_unmapped = [
                row["column_name"] for row in column_info
                if row["is_nullable"] == "NO"
                and row["column_default"] is None
                and row.get("is_identity", "NO") != "YES"
                and row["column_name"] not in {"adm4", "forecast_datetime_local"}
            ]

            for item in forecasts:
                local_dt = _parse_forecast_datetime(item.get("datetime_local"), assume_utc=False)
                if local_dt is None:
                    skipped += 1
                    continue
                utc_dt = _parse_forecast_datetime(item.get("datetime_utc"), assume_utc=True)
                analysis_dt = _parse_forecast_datetime(item.get("analysis_date"), assume_utc=True)
                num_temperature = _numeric_or_none(item.get("temperature_c"))
                num_rain = _numeric_or_none(item.get("precipitation_mm"))
                num_humidity = _numeric_or_none(item.get("humidity_percent"))
                num_wind = _numeric_or_none(item.get("wind_speed_kmh"))
                num_cloud = _numeric_or_none(item.get("cloud_cover_percent"))
                now_utc = datetime.now(timezone.utc)
                values = {
                    "source": "BMKG", "provider": "BMKG", "adm4": payload.get("adm4"),
                    "province": location.get("province"), "city": location.get("city"),
                    "district": location.get("district"), "village": location.get("village"),
                    "location": location_full,
                    "forecast_datetime_local": local_dt, "forecast_datetime_utc": utc_dt,
                    "forecast_time": local_dt, "forecast_datetime": local_dt,
                    "forecast_date": local_dt.date(), "forecast_at": local_dt,
                    "valid_time": local_dt, "datetime": local_dt,
                    "retrieved_at": now_utc, "created_at": now_utc, "updated_at": now_utc,
                    "weather_condition": item.get("condition"), "weather_description": item.get("condition"),
                    "description": item.get("condition"), "weather": item.get("condition"),
                    "temperature_c": num_temperature, "temp_c": num_temperature, "temperature": num_temperature, "temp": num_temperature,
                    "precipitation_mm": num_rain, "rainfall_mm": num_rain, "rain_mm": num_rain,
                    "rainfall": num_rain, "precipitation": num_rain, "tp": num_rain,
                    "humidity_percent": num_humidity, "humidity": num_humidity,
                    "wind_speed_kmh": num_wind, "wind_speed": num_wind,
                    "wind_direction": item.get("wind_direction"),
                    "cloud_cover_percent": num_cloud, "cloud_cover": num_cloud,
                    "visibility": item.get("visibility"), "analysis_date_utc": analysis_dt,
                    "raw_data": Json(item), "raw_response": json.dumps(item, ensure_ascii=False),
                }
                # Pastikan tidak diam-diam menebak nilai kolom wajib yang tidak dikenal.
                unmapped = [c for c in required_unmapped if c not in values or values.get(c) is None]
                if unmapped:
                    raise HTTPException(
                        status_code=500,
                        detail=("Tabel weather.weather_forecasts memiliki kolom wajib yang belum memiliki "
                                "pemetaan otomatis: " + ", ".join(unmapped) + ". Periksa struktur tabel di pgAdmin."),
                    )

                update_cols = [c for c in values if c in available and c not in {"adm4", "forecast_datetime_local", "forecast_time", "forecast_datetime", "forecast_date", "forecast_at", "valid_time", "datetime"}]
                update_parts = [sql.SQL("{} = %s").format(sql.Identifier(c)) for c in update_cols]
                update_params = [values[c] for c in update_cols]
                update_params.extend([payload.get("adm4"), local_dt])
                cur.execute(
                    sql.SQL("UPDATE weather.weather_forecasts SET ")
                    + sql.SQL(", ").join(update_parts)
                    + sql.SQL(" WHERE adm4 = %s AND forecast_datetime_local = %s"),
                    tuple(update_params),
                )
                if cur.rowcount:
                    updated += cur.rowcount
                else:
                    insert_cols = [c for c in values if c in available and values[c] is not None]
                    insert_query = sql.SQL("INSERT INTO weather.weather_forecasts ({}) VALUES ({})").format(
                        sql.SQL(", ").join(sql.Identifier(c) for c in insert_cols),
                        sql.SQL(", ").join(sql.Placeholder() for _ in insert_cols),
                    )
                    cur.execute(insert_query, tuple(values[c] for c in insert_cols))
                    inserted += 1
        conn.commit()

    return {
        "status": "ok",
        "source": "BMKG",
        "table": "weather.weather_forecasts",
        "adm4": payload.get("adm4"),
        "forecast_received": len(forecasts),
        "rows_inserted": inserted,
        "rows_updated": updated,
        "rows_skipped": skipped,
        "note": "Prakiraan disimpan di PostgreSQL. Menekan simpan lagi akan memperbarui waktu prakiraan yang sama bila baris cocok.",
    }

@app.get("/api/weather/saved")
def get_saved_bmkg_forecasts(
    adm4: str = Query(..., description="Kode wilayah ADM4"),
    limit: int = Query(default=100, ge=1, le=500),
):
    """Baca prakiraan BMKG yang sudah disimpan di PostgreSQL."""
    adm4 = adm4.strip()
    if not re.fullmatch(r"\d{2}\.\d{2}\.\d{2}\.\d{4}", adm4):
        raise HTTPException(status_code=400, detail="Format ADM4 tidak valid.")
    with closing(get_connection()) as conn:
        _ensure_weather_forecasts_table(conn)
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                """
                SELECT adm4, province, city, district, village,
                       forecast_datetime_local AS datetime_local,
                       forecast_datetime_utc AS datetime_utc,
                       weather_condition AS condition, temperature_c, precipitation_mm,
                       humidity_percent, wind_speed_kmh, wind_direction, cloud_cover_percent,
                       visibility, analysis_date_utc AS analysis_date, retrieved_at
                FROM weather.weather_forecasts
                WHERE adm4 = %s AND forecast_datetime_local IS NOT NULL
                ORDER BY forecast_datetime_local
                LIMIT %s
                """,
                (adm4, limit),
            )
            rows = cur.fetchall()
    return {"status": "ok", "source": "BMKG", "table": "weather.weather_forecasts", "adm4": adm4, "count": len(rows), "forecast": rows}


# ---------------------------------------------------------------------------
# Sinkronisasi prakiraan BMKG untuk semua kelurahan Makassar yang sudah dipetakan.
# Data ADM4 diambil dari referensi.bmkg_adm4_codes dan dipasangkan melalui
# referensi.bmkg_kelurahan_mapping; API key tidak diperlukan untuk API terbuka BMKG.
# ---------------------------------------------------------------------------

_BMKG_SYNC_LOCK = Lock()
_BMKG_SYNC_STATUS = {
    "status": "idle",
    "total_codes": 0,
    "processed_codes": 0,
    "successful_codes": 0,
    "failed_codes": 0,
    "forecasts_received": 0,
    "rows_inserted": 0,
    "rows_updated": 0,
    "rows_skipped": 0,
    "current_area": None,
    "errors": [],
    "started_at": None,
    "finished_at": None,
    "message": "Sinkronisasi belum dijalankan.",
}


def _sync_status_update(**kwargs):
    with _BMKG_SYNC_LOCK:
        _BMKG_SYNC_STATUS.update(kwargs)


def _make_sync_row_values(payload, item, available_columns, column_types):
    """Buat nilai kolom berdasarkan skema lama/new tanpa menghapus kolom yang ada."""
    local_dt = _parse_forecast_datetime(item.get("datetime_local"), assume_utc=False)
    if local_dt is None:
        return None
    utc_dt = _parse_forecast_datetime(item.get("datetime_utc"), assume_utc=True)
    analysis_dt = _parse_forecast_datetime(item.get("analysis_date"), assume_utc=True)
    now_utc = datetime.now(timezone.utc)
    now_local = datetime.now(ZoneInfo("Asia/Jakarta")).replace(tzinfo=None)
    location = payload.get("location") or {}
    number = _numeric_or_none
    rain = number(item.get("precipitation_mm"))
    temperature = number(item.get("temperature_c"))
    humidity = number(item.get("humidity_percent"))
    wind = number(item.get("wind_speed_kmh"))
    cloud = number(item.get("cloud_cover_percent"))
    values = {
        "area_code": payload.get("adm4"),
        "adm4": payload.get("adm4"),
        "source": "BMKG",
        "provider": "BMKG",
        "province": location.get("province"),
        "city": location.get("city"),
        "district": location.get("district"),
        "village": location.get("village"),
        "location": ", ".join(str(location.get(k)) for k in ("village", "district", "city", "province") if location.get(k)),
        "forecast_time": local_dt,
        "forecast_datetime_local": local_dt,
        "forecast_datetime_utc": utc_dt,
        "forecast_datetime": local_dt,
        "forecast_date": local_dt.date(),
        "forecast_at": local_dt,
        "valid_time": local_dt,
        "datetime": local_dt,
        "forecast_time_local": local_dt,
        "retrieved_at": now_utc,
        "fetched_at": now_local,
        "created_at": now_local,
        "updated_at": now_local,
        "weather_description": item.get("condition"),
        "weather_condition": item.get("condition"),
        "description": item.get("condition"),
        "weather": item.get("condition"),
        "condition": item.get("condition"),
        "temperature_c": temperature,
        "temp_c": temperature,
        "temperature": temperature,
        "temp": temperature,
        "rainfall_mm": rain,
        "precipitation_mm": rain,
        "rain_mm": rain,
        "rainfall": rain,
        "precipitation": rain,
        "tp": rain,
        "humidity_pct": humidity,
        "humidity_percent": humidity,
        "humidity": humidity,
        "wind_speed_kmh": wind,
        "wind_speed": wind,
        "wind_direction": item.get("wind_direction"),
        "cloud_cover_percent": cloud,
        "cloud_cover": cloud,
        "visibility": item.get("visibility"),
        "analysis_date_utc": analysis_dt,
        "raw_data": Json(item),
        "raw_response": json.dumps(item, ensure_ascii=False),
    }
    values = {k: v for k, v in values.items() if k in available_columns}
    # Sesuaikan datetime dengan jenis kolom actual. forecast_time yang biasanya
    # bertipe timestamp tanpa zona tetap menyimpan local_datetime BMKG sebagai waktu lokal.
    jakarta = ZoneInfo("Asia/Jakarta")
    for col, value in list(values.items()):
        if not isinstance(value, datetime):
            continue
        typ = column_types.get(col, "")
        if typ == "timestamp without time zone" and value.tzinfo is not None:
            values[col] = value.astimezone(jakarta).replace(tzinfo=None)
        elif typ == "timestamp with time zone" and value.tzinfo is None:
            values[col] = value.replace(tzinfo=jakarta).astimezone(timezone.utc)
    return values


def _save_one_area_forecasts(conn, payload, column_info):
    """Upsert prakiraan satu area memakai kolom yang tersedia pada tabel existing."""
    available = {row["column_name"] for row in column_info}
    column_types = {row["column_name"]: row["data_type"] for row in column_info}
    area_key = "area_code" if "area_code" in available else ("adm4" if "adm4" in available else None)
    time_key = "forecast_time" if "forecast_time" in available else ("forecast_datetime_local" if "forecast_datetime_local" in available else None)
    if not area_key or not time_key:
        raise RuntimeError("weather.weather_forecasts harus memiliki kolom area_code/adm4 dan forecast_time/forecast_datetime_local.")

    inserted = updated = skipped = 0
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        for item in payload.get("forecast") or []:
            values = _make_sync_row_values(payload, item, available, column_types)
            if values is None:
                skipped += 1
                continue
            key_area = values.get(area_key) or payload.get("adm4")
            key_time = values.get(time_key)
            if key_area is None or key_time is None:
                skipped += 1
                continue
            # Pastikan semua kolom wajib tanpa default yang dikenali benar-benar memiliki nilai.
            missing_required = [
                row["column_name"] for row in column_info
                if row["is_nullable"] == "NO"
                and row["column_default"] is None
                and row.get("is_identity", "NO") != "YES"
                and row["column_name"] not in values
            ]
            null_required = [
                row["column_name"] for row in column_info
                if row["is_nullable"] == "NO"
                and row["column_default"] is None
                and row.get("is_identity", "NO") != "YES"
                and row["column_name"] in values
                and values[row["column_name"]] is None
            ]
            if missing_required or null_required:
                raise RuntimeError("Kolom wajib belum terisi: " + ", ".join(missing_required + null_required))

            update_cols = [c for c in values if c not in {area_key, time_key, "id"}]
            cur.execute(
                sql.SQL("UPDATE weather.weather_forecasts SET ")
                + sql.SQL(", ").join(sql.SQL("{} = %s").format(sql.Identifier(c)) for c in update_cols)
                + sql.SQL(" WHERE {} = %s AND {} = %s").format(sql.Identifier(area_key), sql.Identifier(time_key)),
                tuple(values[c] for c in update_cols) + (key_area, key_time),
            )
            if cur.rowcount:
                updated += cur.rowcount
            else:
                insert_values = {c: v for c, v in values.items() if v is not None or c in {area_key, time_key}}
                cur.execute(
                    sql.SQL("INSERT INTO weather.weather_forecasts ({}) VALUES ({})").format(
                        sql.SQL(", ").join(sql.Identifier(c) for c in insert_values),
                        sql.SQL(", ").join(sql.Placeholder() for _ in insert_values),
                    ),
                    tuple(insert_values.values()),
                )
                inserted += 1
    return len(payload.get("forecast") or []), inserted, updated, skipped


def _run_bmkg_sync_all(codes):
    """Background task: ambil dan simpan prakiraan satu-per-satu untuk menjaga batas akses BMKG."""
    try:
        with closing(get_connection()) as conn:
            _ensure_weather_forecasts_table(conn)
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                cur.execute(
                    """
                    SELECT column_name, data_type, is_nullable, column_default, is_identity
                    FROM information_schema.columns
                    WHERE table_schema='weather' AND table_name='weather_forecasts'
                    ORDER BY ordinal_position
                    """
                )
                column_info = cur.fetchall()
            for idx, area in enumerate(codes, start=1):
                code = area["bmkg_adm4_code"]
                label = f'{area.get("kelurahan_name")}, {area.get("district_name")}'
                _sync_status_update(current_area=f"{idx}/{len(codes)} — {label} ({code})")
                try:
                    payload = bmkg_weather_forecast(adm4=code)
                    received, inserted, updated, skipped = _save_one_area_forecasts(conn, payload, column_info)
                    conn.commit()
                    with _BMKG_SYNC_LOCK:
                        _BMKG_SYNC_STATUS["processed_codes"] = idx
                        _BMKG_SYNC_STATUS["successful_codes"] += 1
                        _BMKG_SYNC_STATUS["forecasts_received"] += received
                        _BMKG_SYNC_STATUS["rows_inserted"] += inserted
                        _BMKG_SYNC_STATUS["rows_updated"] += updated
                        _BMKG_SYNC_STATUS["rows_skipped"] += skipped
                        _BMKG_SYNC_STATUS["message"] = f"Berhasil menyimpan prakiraan untuk {label}."
                except Exception as exc:
                    conn.rollback()
                    detail = getattr(exc, "detail", None) or str(exc)
                    with _BMKG_SYNC_LOCK:
                        _BMKG_SYNC_STATUS["processed_codes"] = idx
                        _BMKG_SYNC_STATUS["failed_codes"] += 1
                        if len(_BMKG_SYNC_STATUS["errors"]) < 25:
                            _BMKG_SYNC_STATUS["errors"].append({"code": code, "area": label, "error": str(detail)[:500]})
                        _BMKG_SYNC_STATUS["message"] = f"Gagal mengambil/menyimpan {label}; proses dilanjutkan ke wilayah berikutnya."
                # BMKG mencantumkan batas akses 60 permintaan per menit/IP. Jeda ini menjaga
                # jarak minimal sekitar satu detik antarpermintaan kode wilayah berbeda.
                if idx < len(codes):
                    time.sleep(1.05)
        with _BMKG_SYNC_LOCK:
            status_name = "completed_with_errors" if _BMKG_SYNC_STATUS["failed_codes"] else "completed"
            _BMKG_SYNC_STATUS["status"] = status_name
            _BMKG_SYNC_STATUS["current_area"] = None
            _BMKG_SYNC_STATUS["finished_at"] = datetime.now(ZoneInfo("Asia/Jakarta")).isoformat(timespec="seconds")
            _BMKG_SYNC_STATUS["message"] = (
                f"Selesai. Berhasil { _BMKG_SYNC_STATUS['successful_codes'] } dari {len(codes)} wilayah; "
                f"gagal { _BMKG_SYNC_STATUS['failed_codes'] }."
            )
    except Exception as exc:
        with _BMKG_SYNC_LOCK:
            _BMKG_SYNC_STATUS["status"] = "failed"
            _BMKG_SYNC_STATUS["finished_at"] = datetime.now(ZoneInfo("Asia/Jakarta")).isoformat(timespec="seconds")
            _BMKG_SYNC_STATUS["message"] = "Sinkronisasi berhenti karena kesalahan backend/database."
            if len(_BMKG_SYNC_STATUS["errors"]) < 25:
                _BMKG_SYNC_STATUS["errors"].append({"code": None, "area": None, "error": str(exc)[:500]})


@app.post("/api/weather/bmkg/sync-all")
def start_bmkg_sync_all(background_tasks: BackgroundTasks):
    """Mulai sinkronisasi semua kode BMKG yang terpetakan; polling progress lewat endpoint status."""
    with closing(get_connection()) as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                """
                SELECT c.bmkg_adm4_code, c.district_name, c.kelurahan_name,
                       m.adm4_pcode, k.adm4_name AS polygon_name
                FROM referensi.bmkg_adm4_codes c
                LEFT JOIN referensi.bmkg_kelurahan_mapping m
                    ON m.bmkg_adm4_code = c.bmkg_adm4_code
                LEFT JOIN referensi."KelurahanMakassar" k
                    ON TRIM(k.adm4_pcode) = TRIM(m.adm4_pcode)
                ORDER BY c.district_name, c.kelurahan_name
                """
            )
            rows = cur.fetchall()
    if not rows:
        raise HTTPException(status_code=409, detail="Daftar kode BMKG kosong. Jalankan SQL pemetaan 153 kelurahan terlebih dahulu.")
    missing = [
        {"code": row["bmkg_adm4_code"], "kelurahan": row["kelurahan_name"], "reason": "Belum ada pasangan kode poligon" if not row["adm4_pcode"] else "Kode poligon tidak ditemukan di referensi.KelurahanMakassar"}
        for row in rows if not row["adm4_pcode"] or not row["polygon_name"]
    ]
    if missing:
        raise HTTPException(status_code=409, detail={
            "message": "Sinkronisasi dihentikan karena masih ada kode yang belum cocok dengan poligon kelurahan.",
            "total_kode": len(rows), "belum_cocok": missing[:25]
        })
    codes = [dict(row) for row in rows]
    with _BMKG_SYNC_LOCK:
        if _BMKG_SYNC_STATUS.get("status") == "running":
            raise HTTPException(status_code=409, detail="Sinkronisasi semua kelurahan sedang berjalan. Buka endpoint status untuk memantau progres.")
        _BMKG_SYNC_STATUS.update({
            "status": "running", "total_codes": len(codes), "processed_codes": 0,
            "successful_codes": 0, "failed_codes": 0, "forecasts_received": 0,
            "rows_inserted": 0, "rows_updated": 0, "rows_skipped": 0,
            "current_area": "Menyiapkan permintaan BMKG…", "errors": [],
            "started_at": datetime.now(ZoneInfo("Asia/Jakarta")).isoformat(timespec="seconds"),
            "finished_at": None,
            "message": "Sinkronisasi dimulai. Proses berjalan bertahap untuk menjaga batas akses BMKG."
        })
    background_tasks.add_task(_run_bmkg_sync_all, codes)
    return {
        "status": "running", "total_codes": len(codes),
        "message": "Sinkronisasi dimulai. Pantau /api/weather/bmkg/sync-all/status untuk progres."
    }


@app.get("/api/weather/bmkg/sync-all/status")
def get_bmkg_sync_all_status():
    """Status/progress sinkronisasi prakiraan BMKG untuk seluruh kode wilayah."""
    with _BMKG_SYNC_LOCK:
        return dict(_BMKG_SYNC_STATUS, errors=list(_BMKG_SYNC_STATUS.get("errors", [])))


@app.get("/api/weather/rainfall-map/times")
def get_rainfall_map_times():
    """Daftar waktu prakiraan yang dapat ditampilkan pada choropleth kelurahan."""
    query = """
        WITH latest_per_area_time AS (
            SELECT DISTINCT ON (TRIM(area_code), forecast_time)
                   TRIM(area_code) AS area_code,
                   forecast_time,
                   rainfall_mm,
                   fetched_at,
                   id
            FROM weather.weather_forecasts
            WHERE area_code IS NOT NULL
              AND TRIM(area_code) <> ''
              AND forecast_time IS NOT NULL
            ORDER BY TRIM(area_code), forecast_time, fetched_at DESC NULLS LAST, id DESC
        )
        SELECT forecast_time,
               COUNT(DISTINCT area_code) AS area_count,
               COUNT(rainfall_mm) AS values_count,
               AVG(rainfall_mm) AS average_mm,
               MAX(rainfall_mm) AS maximum_mm
        FROM latest_per_area_time
        GROUP BY forecast_time
        ORDER BY COUNT(DISTINCT area_code) DESC, forecast_time DESC;
    """
    with closing(get_connection()) as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(query)
            rows = cur.fetchall()
    times = []
    for row in rows:
        ts = row["forecast_time"]
        times.append({
            "forecast_time": ts.strftime("%Y-%m-%d %H:%M:%S") if ts else None,
            "area_count": int(row["area_count"] or 0),
            "values_count": int(row["values_count"] or 0),
            "average_mm": float(row["average_mm"]) if row["average_mm"] is not None else None,
            "maximum_mm": float(row["maximum_mm"]) if row["maximum_mm"] is not None else None,
        })
    return {"status": "ok", "count": len(times), "times": times}


@app.get("/api/weather/rainfall-map")
def get_rainfall_map(
    forecast_time: Optional[str] = Query(default=None, description="Waktu prakiraan lokal, format YYYY-MM-DD HH:MM:SS"),
):
    """GeoJSON choropleth: poligon kelurahan digabungkan dengan prakiraan hujan pada satu waktu."""
    selected_time = None
    if forecast_time:
        try:
            selected_time = datetime.fromisoformat(forecast_time.strip())
            if selected_time.tzinfo is not None:
                raise ValueError("Gunakan waktu lokal tanpa offset zona waktu.")
        except ValueError as exc:
            raise HTTPException(
                status_code=400,
                detail="Format forecast_time harus YYYY-MM-DD HH:MM:SS dan menggunakan waktu lokal Makassar.",
            ) from exc

    with closing(get_connection()) as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            if selected_time is None:
                cur.execute("""
                    WITH latest_per_area_time AS (
                        SELECT DISTINCT ON (TRIM(area_code), forecast_time)
                               TRIM(area_code) AS area_code,
                               forecast_time,
                               rainfall_mm,
                               fetched_at,
                               id
                        FROM weather.weather_forecasts
                        WHERE area_code IS NOT NULL
                          AND TRIM(area_code) <> ''
                          AND forecast_time IS NOT NULL
                        ORDER BY TRIM(area_code), forecast_time, fetched_at DESC NULLS LAST, id DESC
                    )
                    SELECT forecast_time
                    FROM latest_per_area_time
                    GROUP BY forecast_time
                    ORDER BY COUNT(DISTINCT area_code) DESC, forecast_time DESC
                    LIMIT 1;
                """)
                choice = cur.fetchone()
                if not choice or not choice["forecast_time"]:
                    raise HTTPException(status_code=404, detail="Belum ada data prakiraan tersimpan untuk membuat peta hujan.")
                selected_time = choice["forecast_time"]

            cur.execute("""
                WITH latest AS (
                    SELECT DISTINCT ON (TRIM(w.area_code), w.forecast_time)
                           TRIM(w.area_code) AS area_code,
                           w.forecast_time,
                           w.rainfall_mm,
                           w.weather_description,
                           w.fetched_at,
                           w.id
                    FROM weather.weather_forecasts AS w
                    WHERE w.area_code IS NOT NULL
                      AND TRIM(w.area_code) <> ''
                      AND w.forecast_time = %s
                    ORDER BY TRIM(w.area_code), w.forecast_time, w.fetched_at DESC NULLS LAST, w.id DESC
                )
                SELECT
                    m.bmkg_adm4_code,
                    m.adm4_pcode,
                    k.adm4_name AS kelurahan_name,
                    f.forecast_time,
                    f.rainfall_mm,
                    f.weather_description,
                    f.fetched_at,
                    ST_AsGeoJSON(ST_Transform(k.geom, 4326)) AS geojson
                FROM referensi.bmkg_kelurahan_mapping AS m
                JOIN referensi."KelurahanMakassar" AS k
                  ON TRIM(k.adm4_pcode) = TRIM(m.adm4_pcode)
                LEFT JOIN latest AS f
                  ON TRIM(f.area_code) = TRIM(m.bmkg_adm4_code)
                WHERE k.geom IS NOT NULL
                ORDER BY k.adm4_name;
            """, (selected_time,))
            rows = cur.fetchall()

    features = []
    with_values = 0
    missing_values = 0
    for row in rows:
        if not row["geojson"]:
            continue
        rainfall = row["rainfall_mm"]
        if rainfall is None:
            missing_values += 1
        else:
            with_values += 1
        geometry = json.loads(row["geojson"]) if isinstance(row["geojson"], str) else row["geojson"]
        features.append({
            "type": "Feature",
            "geometry": geometry,
            "properties": {
                "kelurahan_name": row["kelurahan_name"],
                "adm4_pcode": row["adm4_pcode"],
                "area_code": row["bmkg_adm4_code"],
                "forecast_time": selected_time.strftime("%Y-%m-%d %H:%M:%S"),
                "rainfall_mm": float(rainfall) if rainfall is not None else None,
                "weather_description": row["weather_description"],
                "fetched_at": row["fetched_at"].isoformat(sep=" ", timespec="seconds") if row["fetched_at"] else None,
            },
        })
    return {
        "type": "FeatureCollection",
        "name": "weather.rainfall_map",
        "forecast_time": selected_time.strftime("%Y-%m-%d %H:%M:%S"),
        "feature_count": len(features),
        "areas_with_rain_value": with_values,
        "areas_without_rain_value": missing_values,
        "note": "Visualisasi prakiraan presipitasi BMKG per waktu prakiraan, bukan peta banjir terkonfirmasi. Warna adalah kelas tampilan curah hujan, bukan kelas dampak banjir.",
        "features": features,
    }


@app.get("/api/weather/bmkg/coverage")
def get_bmkg_forecast_coverage():
    """Ringkasan cakupan prakiraan tersimpan dibandingkan 153 kode dan pemetaan kelurahan."""
    with closing(get_connection()) as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                """
                SELECT
                    COUNT(DISTINCT c.bmkg_adm4_code) AS total_kode_bmkg,
                    COUNT(DISTINCT m.bmkg_adm4_code) AS total_terpetakan,
                    COUNT(DISTINCT w.area_code) AS wilayah_dengan_prakiraan,
                    COUNT(w.id) AS total_baris_prakiraan,
                    COUNT(w.id) FILTER (WHERE w.rainfall_mm IS NOT NULL) AS baris_dengan_nilai_hujan
                FROM referensi.bmkg_adm4_codes c
                LEFT JOIN referensi.bmkg_kelurahan_mapping m
                    ON m.bmkg_adm4_code = c.bmkg_adm4_code
                LEFT JOIN referensi."KelurahanMakassar" k
                    ON TRIM(k.adm4_pcode) = TRIM(m.adm4_pcode)
                LEFT JOIN weather.weather_forecasts w
                    ON w.area_code = c.bmkg_adm4_code
                """
            )
            row = cur.fetchone()
    return {"status": "ok", **row}


# ---------------------------------------------------------------------------
# Isochrone evakuasi otomatis via OpenRouteService (ORS)
# API key harus disimpan di file .env backend, jangan di frontend.
# Hasil opsional disimpan di routing.isochrone_results.
# ---------------------------------------------------------------------------

def _parse_isochrone_ranges(raw_ranges: str) -> list[int]:
    try:
        values = [int(part.strip()) for part in raw_ranges.split(",") if part.strip()]
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="Parameter ranges harus berupa detik, contoh 300,600,900.") from exc
    if not values:
        raise HTTPException(status_code=400, detail="Pilih minimal satu waktu isochrone.")
    if len(values) > 10:
        raise HTTPException(status_code=400, detail="Maksimal 10 interval isochrone per permintaan.")
    if any(value < 60 or value > 72000 for value in values):
        raise HTTPException(status_code=400, detail="Rentang waktu harus 60 sampai 72000 detik untuk profil berjalan kaki.")
    if len(set(values)) != len(values):
        raise HTTPException(status_code=400, detail="Rentang waktu tidak boleh duplikat.")
    return sorted(values)


def _ensure_isochrone_results_table(conn):
    with conn.cursor() as cur:
        cur.execute("CREATE SCHEMA IF NOT EXISTS routing;")
        cur.execute("""
            CREATE TABLE IF NOT EXISTS routing.isochrone_results (
                id BIGSERIAL PRIMARY KEY,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                profile TEXT NOT NULL,
                center_lon DOUBLE PRECISION NOT NULL,
                center_lat DOUBLE PRECISION NOT NULL,
                range_seconds INTEGER NOT NULL,
                geom geometry(MULTIPOLYGON, 4326) NOT NULL,
                properties JSONB NOT NULL DEFAULT '{}'::jsonb
            );
        """)
        cur.execute("""
            CREATE INDEX IF NOT EXISTS isochrone_results_geom_gix
            ON routing.isochrone_results USING GIST (geom);
        """)


@app.get("/api/isochrone")
def calculate_isochrone(
    lon: float = Query(..., ge=-180, le=180, description="Bujur titik asal dalam EPSG:4326"),
    lat: float = Query(..., ge=-90, le=90, description="Lintang titik asal dalam EPSG:4326"),
    ranges: str = Query(default="300,600,900", description="Daftar waktu dalam detik, contoh 300,600,900"),
    save_to_db: bool = Query(default=False, description="Simpan hasil ke routing.isochrone_results"),
):
    """Hitung poligon jangkauan berjalan kaki dari satu titik, lalu kembalikan sebagai GeoJSON."""
    api_key = (os.getenv("ORS_API_KEY") or "").strip()
    if not api_key:
        raise HTTPException(
            status_code=503,
            detail="ORS_API_KEY belum ada. Tambahkan ORS_API_KEY=... di file .env backend, lalu restart Uvicorn.",
        )

    ranges_seconds = _parse_isochrone_ranges(ranges)
    source_url = "https://api.openrouteservice.org/v2/isochrones/foot-walking"
    request_body = {
        "locations": [[float(lon), float(lat)]],
        "range": ranges_seconds,
        "range_type": "time",
        "location_type": "start",
    }
    request = Request(
        source_url,
        data=json.dumps(request_body).encode("utf-8"),
        headers={
            "Authorization": api_key,
            "Content-Type": "application/json",
            "Accept": "application/geo+json, application/json",
            "User-Agent": "WebGIS-Banjir-Makassar/1.0",
        },
        method="POST",
    )

    try:
        with urlopen(request, timeout=45) as response:
            raw_response = response.read()
        ors_data = json.loads(raw_response.decode("utf-8"))
    except HTTPError as exc:
        try:
            error_obj = json.loads(exc.read().decode("utf-8", errors="replace"))
            error_message = (error_obj.get("error") or {}).get("message") or error_obj.get("message") or ""
        except Exception:
            error_message = ""
        if exc.code in (401, 403):
            detail = "OpenRouteService menolak API key. Periksa ORS_API_KEY di .env. " + error_message
        elif exc.code == 429:
            detail = "Batas penggunaan OpenRouteService tercapai. Coba lagi nanti. " + error_message
        else:
            detail = f"OpenRouteService mengembalikan HTTP {exc.code}. " + error_message
        raise HTTPException(status_code=502, detail=detail.strip()) from exc
    except URLError as exc:
        raise HTTPException(status_code=502, detail="Backend tidak dapat mengakses OpenRouteService. Periksa koneksi internet.") from exc
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise HTTPException(status_code=502, detail="Respons OpenRouteService tidak dapat dibaca sebagai JSON.") from exc

    features = ors_data.get("features") if isinstance(ors_data, dict) else None
    if not isinstance(features, list) or not features:
        raise HTTPException(status_code=502, detail="OpenRouteService tidak mengembalikan poligon isochrone.")

    # Tambahkan atribut yang mudah dibaca UI dan dipakai saat penyimpanan.
    normalized_features = []
    for index, feature in enumerate(features):
        if not isinstance(feature, dict) or not isinstance(feature.get("geometry"), dict):
            continue
        properties = dict(feature.get("properties") or {})
        candidate = properties.get("value", properties.get("range_seconds"))
        try:
            range_seconds = int(round(float(candidate)))
        except (TypeError, ValueError):
            range_seconds = ranges_seconds[min(index, len(ranges_seconds) - 1)]
        if range_seconds not in ranges_seconds:
            range_seconds = ranges_seconds[min(index, len(ranges_seconds) - 1)]
        properties["range_seconds"] = range_seconds
        properties["range_minutes"] = int(round(range_seconds / 60))
        feature_copy = dict(feature)
        feature_copy["properties"] = properties
        normalized_features.append(feature_copy)

    if not normalized_features:
        raise HTTPException(status_code=502, detail="Respons isochrone tidak berisi geometri yang bisa ditampilkan.")

    rows_saved = 0
    if save_to_db:
        with closing(get_connection()) as conn:
            _ensure_isochrone_results_table(conn)
            with conn.cursor() as cur:
                for feature in normalized_features:
                    geometry_json = json.dumps(feature["geometry"], ensure_ascii=False)
                    properties = feature.get("properties") or {}
                    range_seconds = int(properties["range_seconds"])
                    cur.execute(
                        """
                        INSERT INTO routing.isochrone_results
                            (profile, center_lon, center_lat, range_seconds, geom, properties)
                        VALUES (
                            %s, %s, %s, %s,
                            ST_Multi(ST_SetSRID(ST_GeomFromGeoJSON(%s), 4326)),
                            %s
                        );
                        """,
                        ("foot-walking", float(lon), float(lat), range_seconds,
                         geometry_json, Json(properties)),
                    )
                    rows_saved += 1
            conn.commit()

    return {
        "type": "FeatureCollection",
        "name": "isochrone_evakuasi",
        "profile": "foot-walking",
        "origin": {"lon": float(lon), "lat": float(lat)},
        "ranges_seconds": ranges_seconds,
        "features": normalized_features,
        "saved_to_db": save_to_db,
        "rows_saved": rows_saved,
        "table": "routing.isochrone_results" if save_to_db else None,
        "note": "Isochrone adalah area jangkauan berdasarkan jaringan jalan dan profil berjalan kaki; tidak otomatis menghindari ruas banjir tanpa data/aturan pembatas tambahan.",
    }

