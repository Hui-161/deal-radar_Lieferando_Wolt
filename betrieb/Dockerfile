FROM python:3.14-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app.py area.py ./
COPY providers ./providers
COPY static ./static

RUN useradd --system --uid 10020 --no-create-home --shell /usr/sbin/nologin dealradar
USER dealradar

EXPOSE 8765
# One process only: menu index, Wolt scan and caches live in memory
CMD ["waitress-serve", "--host=0.0.0.0", "--port=8765", "--threads=8", "app:app"]
