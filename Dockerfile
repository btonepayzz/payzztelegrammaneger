FROM python:3.12-slim-bookworm

RUN apt-get update \
    && apt-get install -y --no-install-recommends curl ca-certificates \
    && curl -fsSL https://deb.nodesource.com/setup_20.x | bash - \
    && apt-get install -y --no-install-recommends nodejs \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY webpanel/package.json webpanel/package-lock.json ./webpanel/
RUN cd webpanel && npm ci

COPY . .

ENV PYTHONUNBUFFERED=1

CMD ["python", "main.py"]
