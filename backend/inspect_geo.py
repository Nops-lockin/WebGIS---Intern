
from pathlib import Path
import geopandas as gpd

folder = Path(r"E:\webgis2mbc\data\batas_kelurahan")
files = list(folder.glob("*.geojson")) + list(folder.glob("*.shp"))

if not files:
    raise FileNotFoundError(
        "Belum ada file .geojson atau .shp di folder batas_kelurahan"
    )

path = files[0]
gdf = gpd.read_file(path)

print("File:", path.name)
print("Jumlah fitur:", len(gdf))
print("CRS:", gdf.crs)
print("Kolom:", list(gdf.columns))
print("Tipe geometri:")
print(gdf.geom_type.value_counts())
print("\nContoh data:")
print(gdf.head(3).to_string())
