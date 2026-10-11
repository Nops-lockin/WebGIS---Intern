import json
import os
import re
import time
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from contextlib import closing
from typing import Optional
from datetime import datetime, timezone
from datetime import datetime, timezone
from datetime import datetime, timezone

import psycopg2
from psycopg2 import sql
from psycopg2.extras import RealDictCursor, Json, Json, Json
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
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
    if not re.fullmatch(r"\\d{2}\\.\\d{2}\\.\\d{2}\\.\\d{4}", adm4):
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
