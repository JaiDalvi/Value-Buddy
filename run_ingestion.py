"""Launch ingestion using existing Secret Manager keys, without printing values."""
import argparse
import os
from pathlib import Path
import subprocess

parser = argparse.ArgumentParser()
parser.add_argument('--bucket', required=True)
args = parser.parse_args()
root = Path(__file__).resolve().parent
adc = Path.home() / '.config/gcloud/application_default_credentials.json'
if not adc.is_file():
    raise SystemExit('Run gcloud auth application-default login first.')
env = os.environ.copy()
for key, secret in [('OPENAI_API_KEY','openai-api-key'), ('COHERE_API_KEY','cohere-api-key'),
                    ('LLAMA_CLOUD_API_KEY','llama-cloud-api-key')]:
    result = subprocess.run(['gcloud','secrets','versions','access','1',
                             '--secret='+secret,'--project=valuebuddy-demo'],
                            check=True, capture_output=True, text=True)
    env[key] = result.stdout.strip()
command = ['docker','run','--rm','--platform','linux/amd64',
           '--mount',f'type=bind,source={adc},target=/tmp/adc.json,readonly',
           '--mount',f'type=bind,source={root},target=/work',
           '-w','/work','-e','GOOGLE_APPLICATION_CREDENTIALS=/tmp/adc.json',
           '-e','OPENAI_API_KEY','-e','COHERE_API_KEY','-e','LLAMA_CLOUD_API_KEY',
           '--entrypoint','python',
           'us-central1-docker.pkg.dev/valuebuddy-demo/valuebuddy-images/valuebuddy:saved-reports-v1',
           '/app/ingest_reports.py','--bucket',args.bucket,'--reports','/work/reports']
subprocess.run(command, env=env, check=True)
