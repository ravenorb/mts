FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN apt-get update \
 && apt-get install -y git \
 && rm -rf /var/lib/apt/lists/*
RUN pip install --no-cache-dir -r requirements.txt
COPY app ./app
ENV SQL_DATA_PATH=/data/sql/mts.db \
    DRAWING_DATA_PATH=/data/drawings \
    PDF_DATA_PATH=/data/pdfs \
    SECRET_KEY=change-me
RUN mkdir -p /data/sql /data/drawings /data/pdfs
EXPOSE 80
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "80"]
