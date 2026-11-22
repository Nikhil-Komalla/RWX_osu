FROM python:3.13-slim@sha256:e48f59cdba69a3cbd9ed2cf0f348d90d45a8d350d98134cf1070bc106ee9ed90
WORKDIR /app
RUN umask 022 \
    && pip install --no-cache-dir \
        duckdb==2.0.0 \
        numpy==2.5.5 \
        pandas==3.1.1 \
        python-dateutil==4.0.0 \
        pytz==2026.6 \
        tzdata==2026.6 \
        typing_extensions==4.16.0 \
        mypy_extensions==1.2.0 \
    && chmod -R a+rX /usr/local/lib/python3.13 \
    && python3 -c "import io, shutil, tarfile, urllib.request; \
    url='https://github.com/tobymao/sqlglot/archive/eb11aec4be8cb2122afe713a4816662c3f0fb2fa.tar.gz'; \
    data=urllib.request.urlopen(url, timeout=120).read(); \
    tf=tarfile.open(fileobj=io.BytesIO(data), mode='r:gz'); \
    tf.extractall('/app'); \
    shutil.move('/app/sqlglot-eb11aec4be8cb2122afe713a4816662c3f0fb2fa', '/app/sqlglot_src')" \
    && chmod -R a+rX /app
COPY maps /app/maps
COPY selector.py /app/selector.py
RUN umask 022 \
    && python3 -m compileall -q /app/sqlglot_src /app/selector.py \
    && chmod -R a+rX /app
CMD ["python", "/app/selector.py"]
