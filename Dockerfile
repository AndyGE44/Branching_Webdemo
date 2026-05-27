FROM python:3.12-slim

WORKDIR /

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1
ENV PYTHONPATH=/src

COPY . /

RUN pip install --no-cache-dir -e .

CMD ["python", "-m", "uvicorn", "agent_safe_demo.mailbox_app:app", "--host", "0.0.0.0", "--port", "8000"]
