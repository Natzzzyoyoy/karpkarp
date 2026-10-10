# Karp — Pemeriksa Kemiripan Teks

Ini adalah versi full-stack dari HTML Karp yang kamu kirim. Frontend dan backend dijalankan dari satu aplikasi supaya tombol **Internet & jurnal** bisa memanggil `/api/check` tanpa perlu menulis alamat server secara manual.

## Fitur yang ditambahkan

- **Riwayat cek**: menyimpan hingga 15 pengecekan terakhir di browser yang sedang dipakai. Bisa membuka hasil lama, memulihkan teks, mengunduh riwayat menjadi JSON, dan menghapus seluruh riwayat.
- **Backend pencarian**: mencari metadata artikel melalui OpenAlex dan Crossref, serta hasil web melalui DuckDuckGo. Kalau kamu punya API key Brave Search, backend bisa menggunakannya untuk pencarian web.
- **Pemeriksaan halaman terbuka**: saat opsi “Baca teks lengkap” aktif, Karp mencoba membaca sebagian teks laman atau PDF publik yang dapat diakses, lalu mencocokkan frasa.
- **Satu server untuk semuanya**: website dan API dijalankan dengan FastAPI.

## Cara menjalankan di Windows

1. Pastikan Python 3.11 atau 3.12 sudah terpasang.
2. Ekstrak ZIP ini ke folder biasa.
3. Klik dua kali **JALANKAN_KARP.bat**.
4. Tunggu sampai terminal menampilkan alamat `http://127.0.0.1:8000`.
5. Buka alamat itu di browser. Jangan buka `static/index.html` dengan klik dua kali karena fitur internet membutuhkan backend.

Kalau Windows menampilkan peringatan keamanan, pastikan kamu mengunduh/menjalankan file dari folder yang kamu percaya. Kamu juga bisa menjalankan perintah berikut secara manual dari folder proyek:

```bash
python -m pip install -r requirements.txt
python -m uvicorn main:app --reload --host 127.0.0.1 --port 8000
```

Status backend bisa dites di `http://127.0.0.1:8000/api/health`.

## Cara membuat link publik dengan Render

Aku menyiapkan `render.yaml`, tetapi link internet permanen perlu dibuat dari akun hosting milikmu. Ikuti langkah ini:

1. Buat repository baru di GitHub.
2. Unggah **isi folder** proyek ini ke repository, jangan unggah file rahasia/API key.
3. Masuk ke https://render.com dan hubungkan akun GitHub.
4. Pilih **New → Web Service**, lalu pilih repository Karp.
5. Bila diminta, gunakan:
   - **Build command:** `pip install -r requirements.txt`
   - **Start command:** `uvicorn main:app --host 0.0.0.0 --port $PORT`
6. Pilih paket yang tersedia, kemudian klik **Deploy**. Setelah statusnya **Live**, Render akan memberikan alamat HTTPS publik berakhiran `onrender.com`. Bagikan alamat itu agar orang lain bisa mengakses websitenya.

Paket gratis Render dapat tidur setelah tidak aktif, sehingga kunjungan pertama sesudah lama tidak digunakan bisa lebih lambat. Lihat batasan terbaru Render sebelum mengandalkannya untuk penggunaan ramai/produksi.

### Struktur folder yang harus dipertahankan

```text
karp-deploy/
├── main.py
├── requirements.txt
├── render.yaml
├── README.md
├── JALANKAN_KARP.bat
└── static/
    ├── index.html
    └── HAMS.png  (opsional: tambahkan gambar hamster kamu di sini)
```

Kalau gambar `static/HAMS.png` belum ada, situs tetap berjalan dan menampilkan emoji hamster sebagai pengganti.

## API key opsional

Pencarian dasar sudah disiapkan tanpa API key, tetapi tergantung ketersediaan dan batas layanan pencarian publik. Jika butuh, atur variabel berikut di halaman **Environment** pada Render, bukan di dalam HTML atau repository:

- `KARP_BRAVE_API_KEY`: API key Brave Search untuk hasil web.
- `KARP_OPENALEX_API_KEY`: API key OpenAlex untuk kuota pencarian yang lebih besar.
- `KARP_CONTACT_EMAIL`: email untuk mengidentifikasi permintaan ke layanan metadata akademik.

Setelah menambahkan variabel, lakukan redeploy.

## Privasi dan batasan

- Riwayat hanya tersimpan di browser/perangkat yang digunakan. Riwayat tidak tersinkron otomatis ke perangkat lain dan bisa hilang jika data situs browser dihapus. File hasil ekspor JSON dapat berisi teks yang diperiksa, jadi simpan secara pribadi.
- Saat mengecek internet, teks dikirim ke backend Karp. Potongan teks juga dikirim ke layanan pencarian eksternal. Hindari memasukkan dokumen rahasia atau data pribadi.
- Karp hanya bisa memeriksa sumber yang ditemukan layanan pencarian dan laman/PDF yang dapat dijangkau. Jurnal berbayar, basis data privat, dan parafrasa tidak selalu terdeteksi. Angka persentase adalah **estimasi kecocokan frasa**, bukan vonis plagiarisme resmi.

## Endpoint backend

- `GET /api/health` — memeriksa apakah server berjalan.
- `POST /api/check` — mencari dan mencocokkan frasa. Minimal 15 kata.
