# InPro Copilot as one container: used by Hugging Face Spaces (the public demo), works on any Docker host.
FROM python:3.11-slim

# Tesseract reads scanned and photographed invoices
RUN apt-get update \
 && apt-get install -y --no-install-recommends tesseract-ocr \
 && rm -rf /var/lib/apt/lists/*

# Hugging Face runs containers as a normal user with id 1000
RUN useradd -m -u 1000 user
USER user
ENV HOME=/home/user PATH=/home/user/.local/bin:$PATH PYTHONUNBUFFERED=1 PYTHONPATH=/home/user/app/src
WORKDIR /home/user/app

COPY --chown=user requirements.txt .
RUN pip install --no-cache-dir --user -r requirements.txt

COPY --chown=user . .
RUN mkdir -p /home/user/data

# Public-demo settings. AI keys are NOT in the image: they come from the Space's secrets at runtime.
ENV INPRO_PUBLIC=1 \
    INPRO_DEMO_MODE=1 \
    INPRO_INTAKE=0 \
    INPRO_COOKIE_SECURE=1 \
    INPRO_NO_DOTENV=1 \
    INPRO_DB=/home/user/data/inpro.db \
    INPRO_UPLOADS=/home/user/data/uploads \
    INPRO_INBOX_DIR=/home/user/data/inbox

EXPOSE 7860
HEALTHCHECK --interval=60s --timeout=5s --start-period=40s CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:7860/api/health', timeout=4)"
CMD ["uvicorn", "inpro_copilot.api:app", "--host", "0.0.0.0", "--port", "7860", "--proxy-headers", "--forwarded-allow-ips", "*"]
