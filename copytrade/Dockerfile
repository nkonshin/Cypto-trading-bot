FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 PIP_ROOT_USER_ACTION=ignore
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg libgl1 libglib2.0-0 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY copytrade/requirements.txt /tmp/requirements.txt
RUN pip install -r /tmp/requirements.txt
COPY copytrade /app/copytrade

CMD ["python", "-m", "copytrade.main"]
