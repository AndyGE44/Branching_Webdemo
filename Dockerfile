FROM python:3.12-slim

WORKDIR /

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1
ENV PYTHONPATH=/src

RUN apt-get update \
    && apt-get install -y --no-install-recommends bash ca-certificates procps \
    && rm -rf /var/lib/apt/lists/*

COPY . /

RUN pip install --no-cache-dir -e .

ENV DEMO_APP_UVICORN_TARGET=agent_safe_demo.app_plane.email_service.app:app
CMD ["sh", "-c", "python -m uvicorn ${DEMO_APP_UVICORN_TARGET} --host 0.0.0.0 --port 8000"]
