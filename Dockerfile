# InPro Copilot as one container: the public demo runs it on Render (free); it works on any Docker host.
FROM python:3.11-slim

# Tesseract reads scanned and photographed invoices
RUN apt-get update \
 && apt-get install -y --no-install-recommends tesseract-ocr \
 && rm -rf /var/lib/apt/lists/*

# run as a normal user, never as root
RUN useradd -m -u 1000 user
USER user
ENV HOME=/home/user PATH=/home/user/.local/bin:$PATH PYTHONUNBUFFERED=1 PYTHONPATH=/home/user/app/src
WORKDIR /home/user/app

COPY --chown=user requirements.txt .
RUN pip install --no-cache-dir --user -r requirements.txt

COPY --chown=user . .
RUN mkdir -p /home/user/data

# Public-demo settings. AI keys are NOT in the image: the host passes them in as secret environment variables.
ENV INPRO_PUBLIC=1 \
    INPRO_DEMO_MODE=1 \
    INPRO_INTAKE=0 \
    INPRO_COOKIE_SECURE=1 \
    INPRO_NO_DOTENV=1 \
    INPRO_DB=/home/user/data/inpro.db \
    INPRO_UPLOADS=/home/user/data/uploads \
    INPRO_INBOX_DIR=/home/user/data/inbox

# the host tells the app which port to use in $PORT (Render: 10000); 7860 otherwise
EXPOSE 7860
HEALTHCHECK --interval=60s --timeout=5s --start-period=60s CMD python -c "import os, urllib.request; urllib.request.urlopen('http://127.0.0.1:%s/api/health' % os.getenv('PORT', '7860'), timeout=4)"
CMD ["sh", "-c", "exec uvicorn inpro_copilot.api:app --host 0.0.0.0 --port ${PORT:-7860} --proxy-headers --forwarded-allow-ips '*'"]
