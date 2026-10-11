FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY alertmonitor ./alertmonitor
RUN useradd --create-home --uid 10001 monitor && mkdir -p /app/data && chown -R monitor /app/data
USER monitor

EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s \
  CMD python -c "import urllib.request as u; u.urlopen('http://127.0.0.1:8080/health', timeout=4)" || exit 1

ENTRYPOINT ["python", "-m", "alertmonitor"]
CMD ["--config", "/app/config.yaml"]
