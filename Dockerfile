FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app.py db.py package.py restore.py ./
COPY templates/ templates/

ENV GAZETTEER_DATABASE=/app/data/app.db \
    GAZETTEER_UPLOAD_DIR=/app/data/uploads \
    PYTHONUNBUFFERED=1

EXPOSE 8000
VOLUME ["/app/data"]

CMD ["gunicorn", "--bind", "0.0.0.0:8000", "--workers", "2", "app:app"]
