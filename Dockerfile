FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    NEWSNEST_CONFIG_DIR=/config \
    NEWSNEST_DATA_DIR=/data

WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt

COPY newsnest/ newsnest/
COPY config.example/ config.example/

# Compose overrides the user with PUID:PGID so bind-mounted files stay owned by the host user
RUN useradd --uid 1000 --create-home newsnest
# A named volume takes the owner of the image path it mounts over; without these, Docker creates
# them root-owned and the app exits because it can't write the config or the database
RUN mkdir -p /config /data && chown newsnest:newsnest /config /data
USER newsnest

EXPOSE 8080
HEALTHCHECK --interval=60s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/health', timeout=4)"

CMD ["python", "-m", "newsnest"]
