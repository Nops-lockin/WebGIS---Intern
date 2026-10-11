WebGIS Banjir Makassar - Frontend Multi-Layer

Cara memakai:
1. Download ZIP ini.
2. Ekstrak isi ZIP ke folder frontend yang sudah digunakan untuk `py -m http.server 5500`.
3. Jika Windows bertanya apakah index.html harus diganti, pilih Replace/Ganti file.
4. Pastikan backend FastAPI/Uvicorn masih berjalan di http://127.0.0.1:8000.
5. Buka atau muat ulang http://127.0.0.1:5500 dengan Ctrl+F5.
6. Frontend mencoba memilih layer utama secara otomatis. Centang/matikan layer di panel kiri untuk mengatur tampilan bersama.

Frontend membutuhkan koneksi internet untuk memuat Leaflet dan basemap OpenStreetMap.
Tidak ada perubahan ke database atau tabel PostGIS yang dilakukan oleh frontend.
