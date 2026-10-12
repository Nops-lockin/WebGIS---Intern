# Backend awal WebGIS Banjir Makassar

Backend FastAPI yang membaca tabel spasial PostGIS dan menyalurkannya sebagai GeoJSON.

## 1. Prasyarat
- Python 3.10 atau lebih baru
- PostgreSQL berjalan di `localhost:5432`
- Database `webgis2` dan ekstensi PostgreSQL sudah aktif

## 2. Instalasi di Windows (PowerShell)
Buka PowerShell di folder ini, lalu jalankan:

```powershell
py -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
pip install -r requirements.txt
Copy-Item .env.example .env
```

Buka file `.env` dan ganti `DB_PASSWORD` dengan password user PostgreSQL yang benar. Jangan kirimkan password tersebut di chat dan jangan unggah file `.env` ke GitHub.

Jika PowerShell memblokir aktivasi environment, jalankan perintah berikut hanya untuk sesi terminal saat ini, lalu aktifkan ulang:

```powershell
Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass
.\.venv\Scripts\Activate.ps1
```

## 3. Jalankan API

```powershell
uvicorn main:app --reload --host 127.0.0.1 --port 8000
```

Biarkan terminal ini tetap terbuka selama API dipakai.

## 4. Tes di browser
1. Buka `http://127.0.0.1:8000/health` untuk mengecek koneksi database.
2. Buka `http://127.0.0.1:8000/docs` untuk melihat dan mencoba endpoint.
3. Coba `GET /api/layers` untuk melihat tabel spasial yang tersedia.
4. Untuk tabel jalan yang disebut dalam konfigurasi proyek, coba `GET /api/layers/referensi/JalanMakassarlines?limit=10&offset=0`.

Nama schema dan tabel di URL harus sama persis dengan hasil `GET /api/layers`. Nama tabel yang menggunakan huruf besar-kecil campuran tetap harus ditulis dengan kapitalisasi yang sama.

## Catatan
- Endpoint layer mengembalikan maksimal 5.000 fitur per permintaan. Gunakan `limit` dan `offset` untuk mengambil halaman berikutnya.
- Parameter `bbox` menerima `min_lon,min_lat,max_lon,max_lat` dalam EPSG:4326, misalnya `?limit=1000&bbox=119.3,-5.3,119.6,-5.0`.
- Endpoint ini untuk pengembangan lokal. Sebelum deployment, batasi CORS dan gunakan konfigurasi kredensial/database yang sesuai.
