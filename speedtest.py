import requests
import urllib3
import time
import csv
import os
import datetime
import threading

# Wyłącz ostrzeżenia dotyczące niezweryfikowanych żądań HTTPS
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# Link do pliku, który będziemy pobierać
file_url = "https://webmail.psm.pulawy.pl/debian-edu-11.5.0-amd64-BD-1.iso"

# Utwórz katalog tymczasowy, jeśli nie istnieje
temp_dir = "temp_download"
os.makedirs(temp_dir, exist_ok=True)

# Zmienna file_path
file_path = os.path.join(temp_dir, os.path.basename(file_url))

# Flag do kontrolowania działania wątku
stop_thread = False

# Funkcja do pobierania pliku
def download_file(file_url):
    try:
        response = requests.get(file_url, stream=True, verify=False)
        with open(file_path, 'wb') as file:
            for chunk in response.iter_content(chunk_size=1024):
                if stop_thread:
                    break
                if chunk:
                    file.write(chunk)
    except Exception as e:
        print(f"Błąd podczas pobierania pliku: {e}")

# Funkcja do pomiaru prędkości pobierania
def measure_download_speed(file_url):
    start_time = time.time()
    download_thread = threading.Thread(target=download_file, args=(file_url,))
    download_thread.start()

    download_thread.join(timeout=30)  # Przerwij pobieranie po 30 sekundach
    if download_thread.is_alive():
        global stop_thread
        stop_thread = True  # Ustaw flagę na zatrzymanie wątku

    download_thread.join()  # Poczekaj, aż wątek zakończy pracę

    end_time = time.time()
    file_size = os.path.getsize(file_path)
    download_time = end_time - start_time
    download_speed = (file_size / download_time) / (1024 * 1024)  # Prędkość w MB/s
    return download_speed

# Funkcja do zapisywania wyników do pliku CSV
def save_to_csv(data):
    current_time = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with open("download_speed.csv", "a", newline="") as csv_file:
        csv_writer = csv.writer(csv_file)
        csv_writer.writerow([current_time, data])

# Główna pętla
while True:
    download_speed = measure_download_speed(file_url)
    if download_speed is not None:
        print(f"Średnia prędkość pobierania: {download_speed:.2f} MB/s")
        save_to_csv(download_speed)

    # Usuń plik tymczasowy, jeśli istnieje
    if os.path.exists(file_path):
        os.remove(file_path)

    # Zresetuj flagę i poczekaj 15 minut przed kolejnym pomiarem
    stop_thread = False
    time.sleep(900 - 30)
