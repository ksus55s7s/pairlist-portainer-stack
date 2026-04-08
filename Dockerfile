FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --upgrade pip \
    && pip install -r requirements.txt

COPY pairlist_injector_nasos_v5_V10.py /app/pairlist_injector_nasos_v5_V10.py

RUN mkdir -p /app/data

EXPOSE 9999

CMD ["python", "/app/pairlist_injector_nasos_v5_V10.py"]
