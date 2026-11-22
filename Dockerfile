FROM python:3.13-slim@sha256:e48f59cdba69a3cbd9ed2cf0f348d90d45a8d350d98134cf1070bc106ee9ed90
WORKDIR /app
RUN python3 -c "import io, shutil, tarfile, urllib.request; \
    url='https://github.com/tobymao/sqlglot/archive/eb11aec4be8cb2122afe713a4816662c3f0fb2fa.tar.gz'; \
    data=urllib.request.urlopen(url, timeout=120).read(); \
    tf=tarfile.open(fileobj=io.BytesIO(data), mode='r:gz'); \
    tf.extractall('/app'); \
    shutil.move('/app/sqlglot-eb11aec4be8cb2122afe713a4816662c3f0fb2fa', '/app/sqlglot_src')" \
    && chmod -R a+rX /app
COPY maps /app/maps
COPY selector.py /app/selector.py
RUN chmod -R a+rX /app
CMD ["python", "/app/selector.py"]
