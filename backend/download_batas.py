
from pathlib import Path
import json
import httpx

BASE_DIR = Path(__file__).resolve().parent
folder = BASE_DIR / "data" / "batas_kelurahan"
folder.mkdir(parents=True, exist_ok=True)

output = folder / "kelurahan.geojson"

url = (
    "https://geoservices.big.go.id/gis/rest/services/"
    "BAPANAS/Batas_Administrasi/MapServer/2/query"
)

params = {
    "where": "WADMKK LIKE '%Makassar%'",
    "outFields": "*",
    "returnGeometry": "true",
    "outSR": "4326",
    "f": "geojson",
}

print("Menghubungi layanan data geospasial BIG...")

try:
    response = httpx.get(
        url,
        params=params,
        timeout=90,
    )
    print("Status HTTP:", response.status_code)
    response.raise_for_status()
    data = response.json()

except Exception as e:
    print("Unduhan gagal:", repr(e))
    raise SystemExit(1)

if "error" in data:
    print("Layanan mengembalikan error:")
    print(json.dumps(data["error"], indent=2, ensure_ascii=False))
    raise SystemExit(1)

if data.get("type") != "FeatureCollection":
    print("Respons bukan GeoJSON FeatureCollection.")
    print(json.dumps(data, indent=2, ensure_ascii=False)[:3000])
    raise SystemExit(1)

features = data.get("features", [])

if not features:
    print("Tidak ada fitur yang ditemukan untuk Makassar.")
    print("Kita perlu memeriksa layer dan nama atribut sumber.")
    raise SystemExit(1)

output.write_text(
    json.dumps(data, ensure_ascii=False),
    encoding="utf-8",
)

print("\nUnduhan berhasil!")
print("Lokasi file:", output)
print("Jumlah fitur:", len(features))
print("Kolom atribut:", list(features[0].get("properties", {}).keys()))
print("Ukuran file:", output.stat().st_size, "byte")
