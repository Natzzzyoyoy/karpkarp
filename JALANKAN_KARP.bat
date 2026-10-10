@echo off
setlocal
cd /d "%~dp0"
echo =========================================
echo   Karp - Plagiarism Phrase Checker
echo =========================================
echo.
where python >nul 2>nul
if errorlevel 1 (
  echo Python belum terdeteksi. Instal Python 3.11 atau 3.12 terlebih dahulu dari python.org.
  echo Pastikan opsi Add Python to PATH dicentang saat instalasi.
  pause
  exit /b 1
)
echo [1/2] Memasang dependensi (pertama kali bisa memerlukan internet)...
python -m pip install -r requirements.txt
if errorlevel 1 (
  echo Gagal memasang dependensi. Periksa koneksi internet dan instalasi Python.
  pause
  exit /b 1
)
echo.
echo [2/2] Menjalankan server Karp...
echo Buka browser dan kunjungi http://127.0.0.1:8000
echo Tekan CTRL+C di jendela ini untuk menghentikan server.
echo.
python -m uvicorn main:app --host 127.0.0.1 --port 8000
pause
