# AgentCore Runtime requires linux/arm64 containers listening on the configured port.
FROM --platform=linux/arm64 public.ecr.aws/docker/library/python:3.13-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

COPY requirements.txt .
RUN pip install -r requirements.txt

COPY app.py .

# AgentCore Runtime injects PORT (default 8080 for HTTP, 9000 for A2A).
EXPOSE 9000

CMD ["python", "app.py"]
