FROM --platform=linux/arm64 public.ecr.aws/docker/library/python:3.13-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

COPY requirements.txt .
RUN pip install -r requirements.txt

COPY app.py .

# BedrockAgentCoreApp listens on 8080 (HTTP protocol) by default.
EXPOSE 8080

CMD ["python", "app.py"]
