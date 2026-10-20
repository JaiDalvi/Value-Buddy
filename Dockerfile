FROM us-central1-docker.pkg.dev/valuebuddy-demo/valuebuddy-images/app@sha256:1445a30216bc361c87f8ded1c863a7b0c706519ab695d1995d54d2a6eaf2b273
RUN python -m pip install --no-cache-dir 'google-cloud-storage>=2.18,<4'
COPY full_width.py report_store.py ingest_reports.py /app/
