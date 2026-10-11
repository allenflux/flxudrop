FROM python:3.13-slim

ENV PYTHONUNBUFFERED=1
ENV FLUXDROP_HOST=0.0.0.0
ENV FLUXDROP_PORT=8090
ENV FLUXDROP_STORAGE_DIR=/data
ENV FLUXDROP_PUBLIC_URL=http://allenflux.tech:8090

WORKDIR /app

COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

COPY app.py fluxdrop_multipart.py fluxdrop_directories.py fluxdrop_tar.py fluxdrop_storage.py /app/
COPY static /app/static

EXPOSE 8090

CMD ["python3", "/app/app.py"]
