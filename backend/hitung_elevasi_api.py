import os
import time

import httpx
from dotenv import load_dotenv
from sqlalchemy import create_engine, text

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
load_dotenv(os.path.join(BASE_DIR, ".env"))

DATABASE_URL = os.getenv("DATABASE_URL")
if not DATABASE_URL:
    raise RuntimeError("DATABASE_URL tidak ditemukan di backend/.env")

engine = create_engine(DATABASE_URL, pool_pre_ping=True)

API_URL = "https://api.open-meteo.com/v1/elevation"
SUMBER = "Open-Meteo / Copernicus DEM GLO-90"
UKURAN_BATCH = 100

QUERY_TITIK = text("""
    SELECT
        id AS kelurahan_id,
        nama_kelurahan,
        ST_Y(ST_PointOnSurface(ST_Transform(geom, 4326))) AS latitude,
        ST_X(ST_PointOnSurface(ST_Transform(geom, 4326))) AS longitude
    FROM public.kelurahan
    WHERE geom IS NOT NULL
      AND NOT ST_IsEmpty(geom)
    ORDER BY id
""")

SIMPAN = text("""
    INSERT INTO public.elevasi_kelurahan
        (kelurahan_id, latitude, longitude, elevasi_titik_m,
         sumber, diperbarui_pada)
    VALUES
        (:kelurahan_id, :latitude, :longitude, :elevasi_titik_m,
         :sumber, NOW())
    ON CONFLICT (kelurahan_id) DO UPDATE SET
        latitude = EXCLUDED.latitude,
        longitude = EXCLUDED.longitude,
        elevasi_titik_m = EXCLUDED.elevasi_titik_m,
        sumber = EXCLUDED.sumber,
        diperbarui_pada = NOW()
""")

def main():
    with engine.connect() as conn:
        kelurahan = conn.execute(QUERY_TITIK).mappings().all()

    if not kelurahan:
        raise RuntimeError("Data kelurahan atau geometri tidak ditemukan.")

    print(f"Jumlah kelurahan: {len(kelurahan)}")
    berhasil = 0
    gagal = []

    with httpx.Client(timeout=60.0) as client:
        for mulai in range(0, len(kelurahan), UKURAN_BATCH):
            batch = kelurahan[mulai:mulai + UKURAN_BATCH]

            params = {
                "latitude": ",".join(
                    str(float(row["latitude"])) for row in batch
                ),
                "longitude": ",".join(
                    str(float(row["longitude"])) for row in batch
                ),
            }

            try:
                response = client.get(API_URL, params=params)
                response.raise_for_status()
                elevations = response.json().get("elevation")

                if not isinstance(elevations, list):
                    raise ValueError("Respons API tidak memiliki array elevation.")

                if len(elevations) != len(batch):
                    raise ValueError("Jumlah hasil elevasi tidak sesuai.")

                records = []
                for row, elevasi in zip(batch, elevations):
                    if elevasi is None:
                        gagal.append(row["nama_kelurahan"])
                        continue

                    records.append({
                        "kelurahan_id": row["kelurahan_id"],
                        "latitude": row["latitude"],
                        "longitude": row["longitude"],
                        "elevasi_titik_m": float(elevasi),
                        "sumber": SUMBER,
                    })

                if records:
                    with engine.begin() as conn:
                        conn.execute(SIMPAN, records)
                    berhasil += len(records)

                print(
                    f"Proses {min(mulai + len(batch), len(kelurahan))}"
                    f"/{len(kelurahan)}; tersimpan: {berhasil}"
                )
                time.sleep(0.3)

            except Exception as exc:
                nama = ", ".join(
                    str(row["nama_kelurahan"]) for row in batch
                )
                gagal.append(f"{nama}: {exc}")
                print(f"Batch gagal: {exc}")

    with engine.connect() as conn:
        total_db = conn.execute(text("""
            SELECT COUNT(*)
            FROM public.elevasi_kelurahan
            WHERE elevasi_titik_m IS NOT NULL
        """)).scalar_one()

    print("\n=== RINGKASAN ===")
    print(f"Kelurahan ditemukan: {len(kelurahan)}")
    print(f"Berhasil diproses: {berhasil}")
    print(f"Batch/data gagal: {len(gagal)}")
    print(f"Data elevasi tersimpan di database: {total_db}")

    if gagal:
        print("Data atau batch yang perlu diperiksa:")
        for item in gagal:
            print("-", item)

if __name__ == "__main__":
    main()
