
import time
import sys
import httpx
from sqlalchemy import text
from app.database import engine

API_BASE = "http://127.0.0.1:8000"
JEDA_DETIK = 1.1


def main():
    with engine.begin() as conn:
        kelurahan = conn.execute(text("""
            SELECT id, kode_wilayah, nama_kelurahan
            FROM public.kelurahan
            WHERE kode_wilayah IS NOT NULL
              AND TRIM(kode_wilayah) <> ''
            ORDER BY id
        """)).mappings().all()

    print(f"Kelurahan dengan kode wilayah: {len(kelurahan)}")

    berhasil = 0
    gagal = []

    with httpx.Client(timeout=30.0) as client:
        for i, k in enumerate(kelurahan, start=1):
            kode = k["kode_wilayah"].strip()
            nama = k["nama_kelurahan"]

            try:
                response = client.get(
                    f"{API_BASE}/api/bmkg/prakiraan/{kode}"
                )

                if response.status_code != 200:
                    gagal.append(
                        (nama, kode, f"HTTP {response.status_code}")
                    )
                    print(
                        f"[{i}/{len(kelurahan)}] GAGAL "
                        f"{nama}: HTTP {response.status_code}"
                    )
                    time.sleep(JEDA_DETIK)
                    continue

                data = response.json()
                daftar = data.get("prakiraan", [])

                if not isinstance(daftar, list) or not daftar:
                    gagal.append((nama, kode, "Prakiraan kosong"))
                    print(f"[{i}/{len(kelurahan)}] KOSONG {nama}")
                    time.sleep(JEDA_DETIK)
                    continue

                with engine.begin() as conn:
                    for p in daftar:
                        waktu = p.get("waktu_lokal")
                        if not waktu:
                            continue

                        conn.execute(text("""
                            INSERT INTO public.prakiraan_bmkg (
                                kelurahan_id,
                                kode_wilayah,
                                waktu_prakiraan,
                                suhu_c,
                                kelembapan_persen,
                                kondisi_cuaca,
                                sumber
                            )
                            VALUES (
                                :kelurahan_id,
                                :kode_wilayah,
                                CAST(:waktu AS timestamp),
                                :suhu,
                                :kelembapan,
                                :kondisi,
                                'BMKG'
                            )
                            ON CONFLICT (
                                kode_wilayah, waktu_prakiraan
                            )
                            DO UPDATE SET
                                kelurahan_id = EXCLUDED.kelurahan_id,
                                suhu_c = EXCLUDED.suhu_c,
                                kelembapan_persen =
                                    EXCLUDED.kelembapan_persen,
                                kondisi_cuaca = EXCLUDED.kondisi_cuaca,
                                sumber = EXCLUDED.sumber,
                                diambil_pada = NOW()
                        """), {
                            "kelurahan_id": k["id"],
                            "kode_wilayah": kode,
                            "waktu": waktu,
                            "suhu": p.get("suhu_c"),
                            "kelembapan": p.get(
                                "kelembapan_persen"
                            ),
                            "kondisi": p.get("kondisi"),
                        })

                berhasil += 1
                print(
                    f"[{i}/{len(kelurahan)}] OK {nama}: "
                    f"{len(daftar)} prakiraan"
                )

            except Exception as e:
                gagal.append((nama, kode, str(e)))
                print(f"[{i}/{len(kelurahan)}] ERROR {nama}: {e}")

            time.sleep(JEDA_DETIK)

    with engine.begin() as conn:
        total = conn.execute(text("""
            SELECT COUNT(*) FROM public.prakiraan_bmkg
        """)).scalar_one()

        jumlah_kelurahan = conn.execute(text("""
            SELECT COUNT(DISTINCT kode_wilayah)
            FROM public.prakiraan_bmkg
        """)).scalar_one()

    print("\n=== RINGKASAN SINKRONISASI ===")
    print(f"Kelurahan berhasil diproses: {berhasil}")
    print(f"Kelurahan gagal: {len(gagal)}")
    print(f"Kelurahan dengan prakiraan tersimpan: {jumlah_kelurahan}")
    print(f"Total baris prakiraan dalam tabel: {total}")

    if gagal:
        print("\nKelurahan yang perlu diperiksa:")
        for nama, kode, alasan in gagal:
            print(f"- {nama} ({kode}): {alasan}")

    if len(kelurahan) != 153:
        print(
            "\nPERHATIAN: jumlah kode yang ditemukan bukan 153. "
            "Periksa tabel kelurahan sebelum menganggap sinkronisasi lengkap."
        )


if __name__ == "__main__":
    main()
