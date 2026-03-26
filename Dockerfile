FROM python:3.12-slim

WORKDIR /app

# Системные зависимости для matplotlib
RUN apt-get update && \
    apt-get install -y --no-install-recommends gcc libffi-dev && \
    rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

RUN mkdir -p data

VOLUME ["/app/data"]

ENTRYPOINT ["python", "main.py"]
