FROM python:3.12-slim

WORKDIR /

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1
ENV PYTHONPATH=/src

RUN apt-get update \
    && apt-get install -y --no-install-recommends bash ca-certificates procps \
    && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir fastapi uvicorn pydantic PyMySQL

RUN mkdir -p /src/agent_safe_demo/app_plane/inventory_service
COPY app.py /src/agent_safe_demo/app_plane/inventory_service/app.py
COPY store.py /src/agent_safe_demo/app_plane/inventory_service/store.py
COPY __init__.py /src/agent_safe_demo/app_plane/inventory_service/__init__.py
RUN touch /src/agent_safe_demo/__init__.py /src/agent_safe_demo/app_plane/__init__.py
