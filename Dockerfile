# ===== base image pindah ke bullseye (wkhtmltopdf available) =====
FROM python:3.11-bullseye AS app

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

# System deps:
# - wkhtmltopdf: untuk export PDF via pdfkit
# - fontconfig & fonts: rendering PDF rapi
RUN apt-get update && apt-get install -y --no-install-recommends \
    wkhtmltopdf \
    fontconfig \
    fonts-liberation \
    ca-certificates \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# install deps python lebih dulu (cache-friendly)
COPY requirements.txt /app/
RUN pip install --no-cache-dir -r requirements.txt \
    && pip install --no-cache-dir gunicorn

# copy source
COPY app.py falcon_wrapper.py ./
COPY templates ./templates

# user non-root
RUN useradd -m -u 10001 appuser && chown -R appuser:appuser /app
USER appuser

ENV FLASK_ENV=production \
    PORT=5000

EXPOSE 5000

CMD ["gunicorn", "-b", "0.0.0.0:5000", "app:app", "--workers", "3", "--threads", "8", "--timeout", "120"]
