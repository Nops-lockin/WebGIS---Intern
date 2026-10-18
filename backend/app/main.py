
from datetime import datetime, timezone

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import text
import httpx

from app.database import engine


app = FastAPI(
    title="WebGIS2MBC",
    description="WebGIS potensi banjir dan rute evakuasi Kota Makassar",
    version="1.2.0",
)


# =========================================================
# 1. KONFIGURASI CORS
# =========================================================

app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:5173",
        "http://127.0.0.1:5173",
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# =========================================================
# 2. BERANDA API
# =========================================================

@app.get("/")
def beranda():
    return {
        "aplikasi": "WebGIS2MBC",
        "status": "aktif",
        "dokumentasi": "/docs",
    }


# =========================================================
# 3. PEMERIKSAAN DATABASE DAN POSTGIS
# =========================================================

@app.get("/api/health")
def health():
    try:
        with engine.connect() as conn:
            db_name = conn.execute(
                text("SELECT current_database()")
            ).scalar_one()

            postgis = conn.execute(
                text("SELECT PostGIS_Version()")
            ).scalar_one()

            jumlah_kelurahan = conn.execute(
                text("SELECT COUNT(*) FROM public.kelurahan")
            ).scalar_one()

        return {
            "status": "ok",
            "database": db_name,
            "postgis": postgis,
            "jumlah_kelurahan": jumlah_kelurahan,
        }

    except Exception as exc:
        raise HTTPException(
            status_code=503,
            detail=f"Koneksi database gagal: {str(exc)}",
        ) from exc


# =========================================================
# 4. DAFTAR DAN PENCARIAN KELURAHAN
# =========================================================

@app.get("/api/kelurahan")
def daftar_kelurahan(
    q: str = Query(default="", max_length=100)
):
    try:
        with engine.connect() as conn:
            rows = conn.execute(
                text("""
                    SELECT
                        id,
                        kode_wilayah,
                        nama_kelurahan
                    FROM public.kelurahan
                    WHERE nama_kelurahan ILIKE :q
                    ORDER BY nama_kelurahan
                    LIMIT 100
                """),
                {"q": f"%{q}%"},
            ).mappings().all()

        return [dict(row) for row in rows]

    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail=f"Gagal mengambil data kelurahan: {str(exc)}",
        ) from exc


# =========================================================
# 5. GEOJSON BATAS ADMINISTRASI KELURAHAN
# =========================================================

@app.get("/api/kelurahan/geojson")
def get_kelurahan_geojson():
    query = text("""
        SELECT
            id,
            kode_wilayah,
            nama_kelurahan,
            ST_AsGeoJSON(geom)::json AS geometry
        FROM public.kelurahan
        WHERE geom IS NOT NULL
        ORDER BY nama_kelurahan
    """)

    try:
        with engine.connect() as conn:
            rows = conn.execute(query).mappings().all()

        features = []

        for row in rows:
            if row["geometry"] is None:
                continue

            features.append({
                "type": "Feature",
                "geometry": row["geometry"],
                "properties": {
                    "id": row["id"],
                    "kode_wilayah": row["kode_wilayah"],
                    "nama_kelurahan": row["nama_kelurahan"],
                },
            })

        return {
            "type": "FeatureCollection",
            "total": len(features),
            "features": features,
        }

    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail=f"Gagal mengambil GeoJSON kelurahan: {str(exc)}",
        ) from exc


# =========================================================
# 6. CURAH HUJAN TERBARU UNTUK SATU KELURAHAN
# =========================================================

@app.get("/api/kelurahan/{kelurahan_id}/hujan-terbaru")
def get_hujan_terbaru(kelurahan_id: int):
    query = text("""
        SELECT
            k.id AS kelurahan_id,
            k.kode_wilayah,
            k.nama_kelurahan,
            h.waktu_pengamatan,
            h.nilai_mm,
            h.jenis_data,
            h.sumber
        FROM public.kelurahan k
        LEFT JOIN LATERAL (
            SELECT
                waktu_pengamatan,
                nilai_mm,
                jenis_data,
                sumber
            FROM public.curah_hujan
            WHERE kelurahan_id = k.id
            ORDER BY waktu_pengamatan DESC
            LIMIT 1
        ) h ON TRUE
        WHERE k.id = :kelurahan_id
    """)

    try:
        with engine.connect() as conn:
            row = conn.execute(
                query,
                {"kelurahan_id": kelurahan_id},
            ).mappings().first()

        if row is None:
            raise HTTPException(
                status_code=404,
                detail="Kelurahan tidak ditemukan.",
            )

        item = dict(row)

        if item["waktu_pengamatan"] is not None:
            item["waktu_pengamatan"] = (
                item["waktu_pengamatan"].isoformat()
            )

        if item["nilai_mm"] is not None:
            item["nilai_mm"] = float(item["nilai_mm"])

        return item

    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail=f"Gagal mengambil data hujan: {str(exc)}",
        ) from exc


# =========================================================
# 7. DAFTAR CURAH HUJAN TERBARU UNTUK DASHBOARD
# =========================================================

@app.get("/api/hujan")
def get_hujan():
    # LEFT JOIN memastikan kelurahan tanpa data hujan tetap muncul.
    query = text("""
        SELECT
            k.id AS id,
            k.id AS kelurahan_id,
            k.kode_wilayah,
            k.nama_kelurahan,
            h.waktu_pengamatan,
            h.nilai_mm,
            h.jenis_data,
            h.sumber
        FROM public.kelurahan k
        LEFT JOIN LATERAL (
            SELECT
                waktu_pengamatan,
                nilai_mm,
                jenis_data,
                sumber
            FROM public.curah_hujan
            WHERE kelurahan_id = k.id
            ORDER BY waktu_pengamatan DESC
            LIMIT 1
        ) h ON TRUE
        ORDER BY k.nama_kelurahan
    """)

    try:
        with engine.connect() as conn:
            rows = conn.execute(query).mappings().all()

        data = []

        for row in rows:
            item = dict(row)

            if item["waktu_pengamatan"] is not None:
                item["waktu_pengamatan"] = (
                    item["waktu_pengamatan"].isoformat()
                )

            if item["nilai_mm"] is not None:
                item["nilai_mm"] = float(item["nilai_mm"])

            data.append(item)

        return {
            "total": len(data),
            "data": data,
        }

    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail=f"Gagal mengambil daftar curah hujan: {str(exc)}",
        ) from exc


# =========================================================
# 8. DATA ELEVASI KELURAHAN
# =========================================================

@app.get("/api/elevasi")
def get_elevasi():
    query = text("""
        SELECT
            k.id AS kelurahan_id,
            k.nama_kelurahan,
            k.kode_wilayah,
            e.latitude,
            e.longitude,
            e.elevasi_titik_m,
            e.sumber,
            e.diperbarui_pada
        FROM public.kelurahan k
        LEFT JOIN public.elevasi_kelurahan e
            ON e.kelurahan_id = k.id
        ORDER BY k.nama_kelurahan
    """)

    try:
        with engine.connect() as conn:
            rows = conn.execute(query).mappings().all()

        data = []

        for row in rows:
            item = dict(row)

            if item["diperbarui_pada"] is not None:
                item["diperbarui_pada"] = (
                    item["diperbarui_pada"].isoformat()
                )

            for kolom in (
                "latitude",
                "longitude",
                "elevasi_titik_m",
            ):
                if item[kolom] is not None:
                    item[kolom] = float(item[kolom])

            data.append(item)

        return {
            "total": len(data),
            "data": data,
        }

    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail=f"Gagal mengambil data elevasi: {str(exc)}",
        ) from exc


# =========================================================
# 9. HASIL ANALISIS BANJIR TERBARU
# =========================================================

@app.get("/api/banjir")
def get_banjir():
    query = text("""
        SELECT
            k.id AS kelurahan_id,
            k.kode_wilayah,
            k.nama_kelurahan,
            p.waktu_analisis,
            p.hujan_mm,
            p.elevasi_m,
            p.tingkat,
            p.metode
        FROM public.kelurahan k
        JOIN LATERAL (
            SELECT
                waktu_analisis,
                hujan_mm,
                elevasi_m,
                tingkat,
                metode
            FROM public.potensi_banjir
            WHERE kelurahan_id = k.id
            ORDER BY waktu_analisis DESC
            LIMIT 1
        ) p ON TRUE
        ORDER BY k.nama_kelurahan
    """)

    try:
        with engine.connect() as conn:
            rows = conn.execute(query).mappings().all()

        data = []

        for row in rows:
            item = dict(row)

            if item["waktu_analisis"] is not None:
                item["waktu_analisis"] = (
                    item["waktu_analisis"].isoformat()
                )

            for kolom in ("hujan_mm", "elevasi_m"):
                if item[kolom] is not None:
                    item[kolom] = float(item[kolom])

            data.append(item)

        return {
            "total": len(data),
            "data": data,
        }

    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail=f"Gagal mengambil hasil analisis banjir: {str(exc)}",
        ) from exc


# =========================================================
# 10. ANALISIS POTENSI BANJIR
# =========================================================

@app.post("/api/banjir/analisis")
def jalankan_analisis_banjir():
    """
    Analisis indikatif menggunakan hujan terbaru dan elevasi titik.
    Bukan model prediksi banjir tervalidasi.
    """

    sumber_query = text("""
        SELECT
            k.id AS kelurahan_id,
            k.nama_kelurahan,
            h.nilai_mm AS hujan_mm,
            e.elevasi_titik_m AS elevasi_m
        FROM public.kelurahan k
        JOIN LATERAL (
            SELECT
                nilai_mm
            FROM public.curah_hujan
            WHERE kelurahan_id = k.id
              AND nilai_mm IS NOT NULL
            ORDER BY waktu_pengamatan DESC
            LIMIT 1
        ) h ON TRUE
        JOIN public.elevasi_kelurahan e
            ON e.kelurahan_id = k.id
        WHERE e.elevasi_titik_m IS NOT NULL
        ORDER BY k.id
    """)

    metode = "Ambang hujan indikatif v1"

    try:
        with engine.begin() as conn:
            rows = conn.execute(
                sumber_query
            ).mappings().all()

            waktu_analisis = datetime.now(timezone.utc)
            processed = []

            for row in rows:
                hujan_mm = float(row["hujan_mm"])
                elevasi_m = float(row["elevasi_m"])

                if hujan_mm >= 50:
                    tingkat = "Tinggi"
                elif hujan_mm >= 20:
                    tingkat = "Sedang"
                else:
                    tingkat = "Rendah"

                # Simpan satu hasil terbaru per kelurahan.
                # ON CONFLICT memerlukan UNIQUE constraint pada
                # potensi_banjir.kelurahan_id.
                # Karena constraint tersebut belum dipastikan ada,
                # gunakan UPDATE lalu INSERT bila belum ada.
                existing = conn.execute(
                    text("""
                        SELECT id
                        FROM public.potensi_banjir
                        WHERE kelurahan_id = :kelurahan_id
                        LIMIT 1
                    """),
                    {"kelurahan_id": row["kelurahan_id"]},
                ).mappings().first()

                values = {
                    "kelurahan_id": row["kelurahan_id"],
                    "waktu_analisis": waktu_analisis,
                    "hujan_mm": hujan_mm,
                    "elevasi_m": elevasi_m,
                    "tingkat": tingkat,
                    "metode": metode,
                }

                if existing:
                    conn.execute(
                        text("""
                            UPDATE public.potensi_banjir
                            SET waktu_analisis = :waktu_analisis,
                                hujan_mm = :hujan_mm,
                                elevasi_m = :elevasi_m,
                                tingkat = :tingkat,
                                metode = :metode
                            WHERE id = :id
                        """),
                        {**values, "id": existing["id"]},
                    )
                else:
                    conn.execute(
                        text("""
                            INSERT INTO public.potensi_banjir (
                                kelurahan_id,
                                waktu_analisis,
                                hujan_mm,
                                elevasi_m,
                                tingkat,
                                metode
                            )
                            VALUES (
                                :kelurahan_id,
                                :waktu_analisis,
                                :hujan_mm,
                                :elevasi_m,
                                :tingkat,
                                :metode
                            )
                        """),
                        values,
                    )

                processed.append({
                    "kelurahan_id": row["kelurahan_id"],
                    "nama_kelurahan": row["nama_kelurahan"],
                    "hujan_mm": hujan_mm,
                    "elevasi_m": elevasi_m,
                    "tingkat": tingkat,
                })

            total_kelurahan = conn.execute(
                text("""
                    SELECT COUNT(*)
                    FROM public.kelurahan
                """)
            ).scalar_one()

        return {
            "status": "selesai",
            "waktu_analisis": waktu_analisis.isoformat(),
            "jumlah_kelurahan": total_kelurahan,
            "berhasil_dianalisis": len(processed),
            "belum_dianalisis": (
                total_kelurahan - len(processed)
            ),
            "metode": metode,
            "catatan": (
                "Klasifikasi indikatif berdasarkan ambang hujan. "
                "Belum merupakan prediksi banjir tervalidasi. "
                "Elevasi yang digunakan adalah elevasi satu titik "
                "representatif, bukan elevasi seluruh wilayah."
            ),
            "data": processed,
        }

    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail=f"Gagal menjalankan analisis banjir: {str(exc)}",
        ) from exc


# =========================================================
# 11. GEOJSON PETA POTENSI BANJIR
# =========================================================

@app.get("/api/banjir/geojson")
def get_banjir_geojson():
    query = text("""
        SELECT
            k.id,
            k.kode_wilayah,
            k.nama_kelurahan,
            ST_AsGeoJSON(k.geom)::json AS geometry,
            p.hujan_mm,
            p.elevasi_m,
            p.tingkat,
            p.waktu_analisis,
            p.metode
        FROM public.kelurahan k
        LEFT JOIN LATERAL (
            SELECT
                hujan_mm,
                elevasi_m,
                tingkat,
                waktu_analisis,
                metode
            FROM public.potensi_banjir
            WHERE kelurahan_id = k.id
            ORDER BY waktu_analisis DESC
            LIMIT 1
        ) p ON TRUE
        WHERE k.geom IS NOT NULL
        ORDER BY k.nama_kelurahan
    """)

    try:
        with engine.connect() as conn:
            rows = conn.execute(query).mappings().all()

        features = []

        for row in rows:
            if row["geometry"] is None:
                continue

            hujan_mm = row["hujan_mm"]
            elevasi_m = row["elevasi_m"]
            waktu_analisis = row["waktu_analisis"]

            features.append({
                "type": "Feature",
                "geometry": row["geometry"],
                "properties": {
                    "id": row["id"],
                    "kode_wilayah": row["kode_wilayah"],
                    "nama_kelurahan": row["nama_kelurahan"],
                    "hujan_mm": (
                        float(hujan_mm)
                        if hujan_mm is not None else None
                    ),
                    "elevasi_m": (
                        float(elevasi_m)
                        if elevasi_m is not None else None
                    ),
                    "tingkat": row["tingkat"],
                    "waktu_analisis": (
                        waktu_analisis.isoformat()
                        if waktu_analisis is not None else None
                    ),
                    "metode": row["metode"],
                },
            })

        return {
            "type": "FeatureCollection",
            "total": len(features),
            "features": features,
        }

    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail=f"Gagal mengambil GeoJSON banjir: {str(exc)}",
        ) from exc


# =========================================================
# 12. PRAKIRAAN CUACA LANGSUNG DARI BMKG
# =========================================================

@app.get("/api/bmkg/prakiraan/{kode_wilayah}")
def prakiraan_bmkg(kode_wilayah: str):
    """
    Mengambil prakiraan cuaca BMKG.
    Prakiraan ini tidak otomatis dimasukkan ke tabel hujan aktual.
    """

    try:
        with engine.connect() as conn:
            kelurahan = conn.execute(
                text("""
                    SELECT
                        kode_wilayah,
                        nama_kelurahan
                    FROM public.kelurahan
                    WHERE kode_wilayah = :kode
                    LIMIT 1
                """),
                {"kode": kode_wilayah},
            ).mappings().first()

    except Exception as exc:
        raise HTTPException(
            status_code=503,
            detail=f"Gagal memeriksa kode wilayah: {str(exc)}",
        ) from exc

    if kelurahan is None:
        raise HTTPException(
            status_code=404,
            detail="Kode wilayah tidak ditemukan.",
        )

    url = "https://api.bmkg.go.id/publik/prakiraan-cuaca"

    try:
        response = httpx.get(
            url,
            params={"adm4": kode_wilayah},
            timeout=20.0,
        )
        response.raise_for_status()
        hasil = response.json()

    except httpx.HTTPStatusError as exc:
        raise HTTPException(
            status_code=502,
            detail=(
                "API BMKG mengembalikan HTTP "
                f"{exc.response.status_code}."
            ),
        ) from exc

    except httpx.RequestError as exc:
        raise HTTPException(
            status_code=502,
            detail="Tidak dapat menghubungi API BMKG.",
        ) from exc

    except ValueError as exc:
        raise HTTPException(
            status_code=502,
            detail="Respons API BMKG bukan JSON yang valid.",
        ) from exc

    lokasi = hasil.get("lokasi") or {}
    prakiraan = []

    for kelompok in hasil.get("data", []):
        for rentang in kelompok.get("cuaca", []):
            for item in rentang:
                prakiraan.append({
                    "waktu_lokal": item.get("local_datetime"),
                    "suhu_c": item.get("t"),
                    "kelembapan_persen": item.get("hu"),
                    "parameter_tp": item.get("tp"),
                    "kondisi": item.get("weather_desc"),
                })

    return {
        "sumber": "BMKG",
        "jenis_data": (
            "Prakiraan cuaca, bukan observasi hujan harian"
        ),
        "kode_wilayah": kode_wilayah,
        "kelurahan": (
            lokasi.get("desa")
            or kelurahan["nama_kelurahan"]
        ),
        "kecamatan": lokasi.get("kecamatan"),
        "kota": lokasi.get("kotkab"),
        "jumlah_prakiraan": len(prakiraan),
        "prakiraan": prakiraan,
    }


# =========================================================
# 13. PRAKIRAAN BMKG YANG TERSIMPAN DI DATABASE
# =========================================================

@app.get("/api/prakiraan/{kode_wilayah}")
def get_prakiraan_tersimpan(kode_wilayah: str):
    query = text("""
        SELECT
            k.nama_kelurahan,
            p.kode_wilayah,
            p.waktu_prakiraan,
            p.suhu_c,
            p.kelembapan_persen,
            p.kondisi_cuaca,
            p.sumber
        FROM public.prakiraan_bmkg p
        JOIN public.kelurahan k
            ON k.id = p.kelurahan_id
        WHERE p.kode_wilayah = :kode
          AND p.waktu_prakiraan >= LOCALTIMESTAMP
        ORDER BY p.waktu_prakiraan
        LIMIT 24
    """)

    try:
        with engine.connect() as conn:
            rows = conn.execute(
                query,
                {"kode": kode_wilayah},
            ).mappings().all()

        return {
            "sumber": "BMKG",
            "jenis_data": (
                "Prakiraan cuaca tersimpan, bukan hujan aktual"
            ),
            "kode_wilayah": kode_wilayah,
            "kelurahan": (
                rows[0]["nama_kelurahan"] if rows else None
            ),
            "jumlah_prakiraan": len(rows),
            "prakiraan": [
                {
                    "waktu_lokal": (
                        row["waktu_prakiraan"].isoformat()
                        if row["waktu_prakiraan"] is not None
                        else None
                    ),
                    "suhu_c": (
                        float(row["suhu_c"])
                        if row["suhu_c"] is not None
                        else None
                    ),
                    "kelembapan_persen": (
                        row["kelembapan_persen"]
                    ),
                    "kondisi": row["kondisi_cuaca"],
                    "sumber": row["sumber"],
                }
                for row in rows
            ],
        }

    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail=(
                "Gagal mengambil prakiraan tersimpan: "
                f"{str(exc)}"
            ),
        ) from exc
