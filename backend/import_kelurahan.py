from pathlib import Path

import geopandas as gpd
import pandas as pd
from sqlalchemy import text

from app.database import engine

# Path folder 'backend' (folder tempat skrip ini berada)
BACKEND_DIR = Path(__file__).resolve().parent

# Path akar repositori (satu tingkat di atas folder 'backend')
REPO_ROOT = BACKEND_DIR.parent

# Path relatif menuju file kelurahan.geojson
FILE_INPUT = REPO_ROOT / "data" / "batas_kelurahan" / "kelurahan.geojson"

if not FILE_INPUT.exists():
    raise FileNotFoundError(
        f"File tidak ditemukan: {FILE_INPUT}"
    )

print("Membaca GeoJSON...")
gdf = gpd.read_file(FILE_INPUT)

if gdf.empty:
    raise ValueError("GeoJSON tidak memiliki fitur.")

if gdf.crs is None:
    raise ValueError(
        "CRS tidak tersedia. Jangan menebak CRS sumber."
    )

print("Kolom sumber:", list(gdf.columns))

# Pastikan kolom nama kelurahan tersedia.
if "WADMKD" not in gdf.columns:
    raise ValueError(
        "Kolom WADMKD tidak ditemukan. "
        "Periksa hasil download_batas.py."
    )

# Jika tersedia, filter hanya Kota Makassar.
if "WADMKK" in gdf.columns:
    gdf = gdf[
        gdf["WADMKK"].astype(str).str.contains(
            "Makassar",
            case=False,
            na=False,
        )
    ].copy()

if gdf.empty:
    raise ValueError(
        "Tidak ada fitur yang cocok dengan Kota Makassar."
    )

# Ambil kode PUM; jika kosong, gunakan kode BPS.
if "KDEPUM" in gdf.columns:
    kode = gdf["KDEPUM"].astype("string")
else:
    kode = pd.Series(
        [pd.NA] * len(gdf),
        index=gdf.index,
        dtype="string",
    )

if "KDEBPS" in gdf.columns:
    kode_bps = gdf["KDEBPS"].astype("string")
    kode = kode.fillna(kode_bps)

# Bersihkan nama dan kode wilayah.
gdf["nama_kelurahan"] = (
    gdf["WADMKD"].astype("string").str.strip()
)

gdf["kode_wilayah"] = kode.str.strip()

# Pertahankan kolom yang dibutuhkan.
gdf = gdf[
    ["kode_wilayah", "nama_kelurahan", "geometry"]
].copy()

gdf = gdf.dropna(
    subset=[
        "kode_wilayah",
        "nama_kelurahan",
        "geometry",
    ]
)

gdf = gdf[
    gdf["kode_wilayah"].ne("")
    & gdf["nama_kelurahan"].ne("")
    & ~gdf.geometry.is_empty
].copy()

if gdf.empty:
    raise ValueError(
        "Tidak ada fitur dengan nama, kode, "
        "dan geometri lengkap."
    )

# Ubah CRS untuk peta web.
gdf = gdf.to_crs("EPSG:4326")

# Perbaiki geometri yang tidak valid.
invalid = ~gdf.geometry.is_valid

if invalid.any():
    gdf.loc[invalid, "geometry"] = (
        gdf.loc[invalid, "geometry"].make_valid()
    )

# Pertahankan hanya geometri poligon.
gdf = gdf[
    gdf.geometry.geom_type.isin(
        ["Polygon", "MultiPolygon"]
    )
].copy()

# Hilangkan duplikasi kode wilayah.
gdf = gdf.drop_duplicates(
    subset=["kode_wilayah"]
)

if gdf.empty:
    raise ValueError(
        "Tidak ada poligon valid untuk diimpor."
    )

print(
    "Jumlah fitur yang akan diimpor:",
    len(gdf),
)

print(
    gdf[
        ["kode_wilayah", "nama_kelurahan"]
    ].head(10).to_string(index=False)
)

# Buat tabel staging.
gdf.to_postgis(
    name="kelurahan_import",
    con=engine,
    schema="public",
    if_exists="replace",
    index=False,
)

# Validasi staging dan masukkan data ke tabel utama.
with engine.begin() as conn:
    jumlah = conn.execute(
        text("""
            SELECT COUNT(*)
            FROM public.kelurahan_import
            WHERE geometry IS NOT NULL
        """)
    ).scalar_one()

    if jumlah == 0:
        raise ValueError(
            "Tabel staging kosong."
        )

    # Masukkan geometri 2D ke tabel utama.
    conn.execute(
        text("""
            INSERT INTO public.kelurahan (
                kode_wilayah,
                nama_kelurahan,
                geom
            )
            SELECT
                kode_wilayah,
                nama_kelurahan,
                ST_Multi(
                    ST_CollectionExtract(
                        ST_MakeValid(
                            ST_Force2D(geometry)
                        ),
                        3
                    )
                )::geometry(MultiPolygon, 4326)
            FROM public.kelurahan_import
            WHERE geometry IS NOT NULL
              AND NOT ST_IsEmpty(geometry)
            ON CONFLICT (kode_wilayah)
            DO UPDATE SET
                nama_kelurahan = EXCLUDED.nama_kelurahan,
                geom = EXCLUDED.geom
        """)
    )

print("Impor selesai.")
print("Jumlah fitur staging:", jumlah)
