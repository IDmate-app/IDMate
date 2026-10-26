FROM python:3.12-slim

WORKDIR /app

# Tesseract liest Ladebelege (Screenshots der Lade-Apps) für die externe
# Ladung; deu+eng, ohne Empfehlungen ~40 MB (beleg.py). DejaVu ist die
# Schrift des Fahrtenbuch-PDFs (fahrtenbuch.py): alle Ortsnamen, nicht nur
# Latin-1.
RUN apt-get update \
    && apt-get install -y --no-install-recommends tesseract-ocr tesseract-ocr-deu fonts-dejavu-core \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

RUN mkdir -p /data

EXPOSE 5000

CMD ["python", "app.py"]
