"""Read-only GCS artifacts. JSON vectors avoid loading pickled FAISS metadata."""
import hashlib
import json
import os
import tempfile
from pathlib import Path
from google.cloud import storage

PIPELINE_VERSION = 'saved-reports-v1-embed-english-v3.0-chunk3300'

def sha256(data):
    return hashlib.sha256(data).hexdigest()

def get_bucket():
    return storage.Client(project=os.environ.get('GOOGLE_CLOUD_PROJECT', 'valuebuddy-demo')).bucket(
        os.environ['VALUEBUDDY_BUCKET'])

def get_catalog():
    catalog = json.loads(get_bucket().blob('valuebuddy/catalog.json').download_as_bytes())
    if not isinstance(catalog.get('reports'), list):
        raise ValueError('Invalid report catalog')
    return catalog

def load_report(report):
    bucket = get_bucket()
    cache = Path(tempfile.gettempdir()) / 'valuebuddy-saved-reports'
    # Hash identifiers to keep catalog values out of local filesystem paths.
    folder = cache / sha256((report['id'] + report['revision']).encode())
    folder.mkdir(parents=True, exist_ok=True)
    for field, name in [('pdf', 'report.pdf'), ('index', 'index.json')]:
        path = folder / name
        expected = report[field + '_sha256']
        if not path.exists() or sha256(path.read_bytes()) != expected:
            data = bucket.blob(report[field]).download_as_bytes()
            if sha256(data) != expected:
                raise ValueError('Artifact checksum mismatch')
            fd, temporary = tempfile.mkstemp(dir=folder)
            with os.fdopen(fd, 'wb') as stream:
                stream.write(data)
            os.replace(temporary, path)
    index = json.loads((folder / 'index.json').read_text())
    records = index.get('records', [])
    if not records or index.get('model') != 'embed-english-v3.0':
        raise ValueError('Empty or unsupported saved index')
    dimension = len(records[0]['embedding'])
    if not dimension or any(len(r['embedding']) != dimension for r in records):
        raise ValueError('Invalid embedding dimensions')
    return index, folder / 'report.pdf'
