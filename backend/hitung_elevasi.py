from pathlib import Path

import geopandas as gpd
import pandas as pd
import rasterio
from rasterstats import zonal_stats
from sqlalchemy import text

from app.database import engine


BASE_DIR = Path(__file__).resolve().parent.parent
DEM_PATH = BASE_DIR / "data" / "elevasi" / "dem_makassar.tif"
OUTPUT_CSV = BASE_DIR / "data" / "elevasi" / "hasil_elevasi.csv"

if not DEM_PATH.exists():
    raise FileNotFoundError(
        f"File DEM tidak ditemukan: {DEM_PATH}"
    )

print("Membaca batas kelurahan dari PostGIS...")

with engine.connect() as conn:
    zones = gpd.read_postgis(
        """
        SELECT id, nama_kelurahan, geom
        FROM public.kelurahan
        WHERE geom IS NOT NULL
        ORDER BY id
        """,
        conn,
        geom_col="geom",
    )

if zones.empty:
    raise RuntimeError("Tidak ada geometri kelurahan.")

with rasterio.open(DEM_PATH) as src:
    if src.crs is None:
        raise ValueError("DEM tidak memiliki informasi CRS.")

    print("CRS DEM:", src.crs)
    print("CRS batas awal:", zones.crs)

    if zones.crs is None:
        raise ValueError("Batas kelurahan tidak memiliki CRS.")

    # Geometri diproyeksikan ke CRS raster agar cocok.
    zones_raster = zones.to_crs(src.crs)

    nodata_value = src.nodata

    if nodata_value is None:
        print(
            "PERINGATAN: DEM tidak mendefinisikan nodata. "
            "Periksa metadata raster sebelum memakai hasil."
        )

    print("Menghitung statistik elevasi...")

    stats = zonal_stats(
        zones_raster.geometry,
        str(DEM_PATH),
        stats=["min", "mean", "max", "count"],
        nodata=nodata_value,
        all_touched=False,
    )

result = pd.DataFrame(stats)
result.insert(0, "kelurahan_id", zones["id"].to_numpy())
result.insert(
    1, "nama_kelurahan", zones["nama_kelurahan"].to_numpy()
)

result = result.rename(
    columns={
        "min": "elevasi_min_m",
        "mean": "elevasi_rata_m",
        "max": "elevasi_maks_m",
        "count": "jumlah_piksel",
    }
)

OUTPUT_CSV.parent.mkdir(parents=True, exist_ok=True)
result.to_csv(OUTPUT_CSV, index=False)

print("Hasil disimpan ke:", OUTPUT_CSV)
print(result.head(10).to_string(index=False))
print(
    "Kelurahan tanpa piksel valid:",
    int((result["jumlah_piksel"].fillna(0) == 0).sum()),
)
