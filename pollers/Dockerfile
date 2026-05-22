FROM python:3.12-slim

RUN apt-get update && apt-get install -y --no-install-recommends curl && rm -rf /var/lib/apt/lists/*

RUN curl -sSL https://github.com/dfinity/dre/releases/download/v0.7.7/dre-x86_64-unknown-linux \
    -o /usr/local/bin/dre && chmod +x /usr/local/bin/dre

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
CMD ["python", "runner.py"]
