import logging
from datetime import datetime, timezone
import httpx
from sqlalchemy import text
from app.database import engine

logger = logging.getLogger("rain_sync")
logging.basicConfig(level=logging.INFO)

OPEN_METEO_URL = "https://api.open-meteo.com/v1/forecast"
BATCH_SIZE = 50

# Status pencatatan terakhir
last_sync_info = {
    "status": "belum_berjalan",
    "waktu_mulai": None,
    "waktu_selesai": None,
    "total_kelurahan": 0,
    "berhasil": 0,
    "gagal": 0,
    "pesan": "Belum ada sinkronisasi yang dijalankan.",
}


def sync_curah_hujan_dan_banjir():
    """
    Mengambil data curah hujan terkini (mm) per kelurahan dari API Open-Meteo
    berdasarkan koordinat latitude/longitude kelurahan, menyimpannya ke tabel
    public.curah_hujan, dan memperbarui analisis potensi banjir.
    """
    global last_sync_info
    waktu_mulai = datetime.now(timezone.utc)
    logger.info("Memulai sinkronisasi curah hujan otomatis...")

    last_sync_info["status"] = "sedang_berjalan"
    last_sync_info["waktu_mulai"] = waktu_mulai.isoformat()

    try:
        with engine.connect() as conn:
            kelurahan_rows = conn.execute(
                text("""
                    SELECT
                        k.id AS kelurahan_id,
                        k.kode_wilayah,
                        k.nama_kelurahan,
                        e.latitude,
                        e.longitude,
                        e.elevasi_titik_m
                    FROM public.kelurahan k
                    LEFT JOIN public.elevasi_kelurahan e
                        ON e.kelurahan_id = k.id
                    WHERE e.latitude IS NOT NULL
                      AND e.longitude IS NOT NULL
                    ORDER BY k.id
                """)
            ).mappings().all()

        total_kel = len(kelurahan_rows)
        last_sync_info["total_kelurahan"] = total_kel

        if total_kel == 0:
            msg = "Tidak ada kelurahan dengan koordinat latitude/longitude yang valid."
            logger.warning(msg)
            last_sync_info.update({
                "status": "selesai",
                "waktu_selesai": datetime.now(timezone.utc).isoformat(),
                "pesan": msg,
            })
            return last_sync_info

        berhasil = 0
        hujan_records = []

        # Kirim secara batch ke Open-Meteo
        with httpx.Client(timeout=30.0) as client:
            for i in range(0, total_kel, BATCH_SIZE):
                batch = kelurahan_rows[i:i + BATCH_SIZE]
                lats = [float(row["latitude"]) for row in batch]
                lons = [float(row["longitude"]) for row in batch]

                params = {
                    "latitude": lats,
                    "longitude": lons,
                    "daily": ["precipitation_sum"],
                    "current": ["precipitation"],
                    "timezone": "Asia/Makassar",
                }

                resp = client.get(OPEN_METEO_URL, params=params)
                if resp.status_code != 200:
                    logger.error(
                        f"Gagal mengambil data Open-Meteo batch {i}: HTTP {resp.status_code}"
                    )
                    continue

                hasil_json = resp.json()
                # Jika batch cuma 1 item, respons berupa dict tunggal; jika multi, berupa list of dict
                items = hasil_json if isinstance(hasil_json, list) else [hasil_json]

                for row, data_cuaca in zip(batch, items):
                    # Ambil akumulasi hujan hari ini (precipitation_sum)
                    # atau curah hujan terkini (current precipitation)
                    nilai_mm = 0.0
                    daily = data_cuaca.get("daily", {})
                    precip_sum_list = daily.get("precipitation_sum", [])

                    if precip_sum_list and precip_sum_list[0] is not None:
                        nilai_mm = float(precip_sum_list[0])
                    else:
                        current = data_cuaca.get("current", {})
                        if current.get("precipitation") is not None:
                            nilai_mm = float(current.get("precipitation"))

                    hujan_records.append({
                        "kelurahan_id": row["kelurahan_id"],
                        "waktu_pengamatan": waktu_mulai,
                        "nilai_mm": nilai_mm,
                        "jenis_data": "Harian Otomatis",
                        "sumber": "Open-Meteo API",
                        "elevasi_m": float(row["elevasi_titik_m"] or 0),
                        "nama_kelurahan": row["nama_kelurahan"],
                    })
                    berhasil += 1

        # Simpan ke tabel curah_hujan dan update potensi_banjir dalam transaksi database
        waktu_analisis = datetime.now(timezone.utc)
        metode = "Ambang hujan otomatis (Open-Meteo)"

        with engine.begin() as conn:
            for rec in hujan_records:
                # 1. Simpan rekaman hujan
                conn.execute(
                    text("""
                        INSERT INTO public.curah_hujan (
                            kelurahan_id,
                            waktu_pengamatan,
                            nilai_mm,
                            jenis_data,
                            sumber,
                            created_at
                        )
                        VALUES (
                            :kelurahan_id,
                            :waktu_pengamatan,
                            :nilai_mm,
                            :jenis_data,
                            :sumber,
                            NOW()
                        )
                    """),
                    {
                        "kelurahan_id": rec["kelurahan_id"],
                        "waktu_pengamatan": rec["waktu_pengamatan"],
                        "nilai_mm": rec["nilai_mm"],
                        "jenis_data": rec["jenis_data"],
                        "sumber": rec["sumber"],
                    },
                )

                # 2. Klasifikasi potensi banjir
                hujan_val = rec["nilai_mm"]
                elevasi_val = rec["elevasi_m"]

                if hujan_val >= 50:
                    tingkat = "Tinggi"
                elif hujan_val >= 20:
                    tingkat = "Sedang"
                else:
                    tingkat = "Rendah"

                # Update jika sudah ada, atau Insert
                existing = conn.execute(
                    text("""
                        SELECT id FROM public.potensi_banjir
                        WHERE kelurahan_id = :kelurahan_id
                        LIMIT 1
                    """),
                    {"kelurahan_id": rec["kelurahan_id"]},
                ).mappings().first()

                pb_params = {
                    "kelurahan_id": rec["kelurahan_id"],
                    "waktu_analisis": waktu_analisis,
                    "hujan_mm": hujan_val,
                    "elevasi_m": elevasi_val,
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
                        {**pb_params, "id": existing["id"]},
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
                        pb_params,
                    )

        waktu_selesai = datetime.now(timezone.utc)
        last_sync_info.update({
            "status": "selesai",
            "waktu_selesai": waktu_selesai.isoformat(),
            "total_kelurahan": total_kel,
            "berhasil": berhasil,
            "gagal": total_kel - berhasil,
            "pesan": f"Berhasil memperbarui data curah hujan & risiko banjir untuk {berhasil} kelurahan.",
        })
        logger.info(last_sync_info["pesan"])
        return last_sync_info

    except Exception as exc:
        logger.exception("Terjadi kesalahan saat sinkronisasi hujan:")
        last_sync_info.update({
            "status": "gagal",
            "waktu_selesai": datetime.now(timezone.utc).isoformat(),
            "pesan": f"Kesalahan: {str(exc)}",
        })
        return last_sync_info
