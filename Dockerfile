FROM python:3.11-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    bluez \
    bluetooth \
    libbluetooth-dev \
    wireless-tools \
    iw \
    iproute2 \
    gpsd \
    gcc \
    make \
    pkg-config \
    libglib2.0-dev \
    python3-dev \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Patch gps/client.py: json.loads() dropped the encoding= kwarg in Python 3.9+
RUN sed -i 's/json.loads(buf.strip(), encoding="ascii")/json.loads(buf.strip())/g' \
    /usr/local/lib/python3.11/site-packages/gps/client.py

COPY . .

RUN mkdir -p /app/data

EXPOSE 8080

CMD ["python", "manager/app.py"]
