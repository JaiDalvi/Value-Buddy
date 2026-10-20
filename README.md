# Value Buddy: saved report library

Includes Galligan, JB Tile & Stone, and Pace Pool and Spa. Add any further PDFs
under reports/ before ingestion. The reports are excluded from the Docker image.
Cloud Run and the existing HTTPS/IAP configuration remain unchanged.

## 1. Build the updated image

In this extracted folder, with Docker running:

```bash
gcloud auth configure-docker us-central1-docker.pkg.dev
docker buildx build --platform linux/amd64 -t us-central1-docker.pkg.dev/valuebuddy-demo/valuebuddy-images/valuebuddy:saved-reports-v1 --push .
```

## 2. Create a private report bucket

Use a globally unique bucket name; the example can be changed consistently.

```bash
gcloud storage buckets create gs://valuebuddy-reports-jai-552898460688 --project=valuebuddy-demo --location=us-central1 --uniform-bucket-level-access --public-access-prevention
gcloud auth application-default login
gcloud auth application-default set-quota-project valuebuddy-demo
```

The ingestion operator needs object read/write permissions on this bucket.
Existing project-owner credentials generally already provide them. Do not grant
public access. Keep financial reports limited to authorized demo users.

## 3. Process once on your Mac, inside the working image

First pull the image you built:

```bash
docker pull --platform linux/amd64 us-central1-docker.pkg.dev/valuebuddy-demo/valuebuddy-images/valuebuddy:saved-reports-v1
```

The following script reads the three API keys from Secret Manager version 1
without printing them. It passes them to Docker via environment variables;
credentials and API keys are not written into the image. The ADC credential file
is mounted read-only. This step makes paid LlamaParse, OpenAI and Cohere calls.

```bash
python3 run_ingestion.py --bucket valuebuddy-reports-jai-552898460688
```

Wait for `Done. Reports are ready in the dropdown.` Completed reports are skipped
when rerun with unchanged PDF bytes and pipeline version. Local .ingestion-cache
contains a checkpoint of each fully processed report; keep it private. A failure
mid-report can require repeating some calls for that report. Change
PIPELINE_VERSION in report_store.py when changing parsing or chunking rules.

## 4. Give only this app read access using GKE Workload Identity

Run in zsh/bash on the Mac:

```bash
export PATH="$(gcloud info --format='value(installation.sdk_root)')/bin:$PATH"
PROJECT_NUMBER=$(gcloud projects describe valuebuddy-demo --format='value(projectNumber)')
kubectl --context=gke_valuebuddy-demo_us-central1_servicing-agent-cluster -n valuebuddy-demo create serviceaccount valuebuddy-reader --dry-run=client -o yaml | kubectl --context=gke_valuebuddy-demo_us-central1_servicing-agent-cluster apply -f -
gcloud storage buckets add-iam-policy-binding gs://valuebuddy-reports-jai-552898460688 --member="principal://iam.googleapis.com/projects/${PROJECT_NUMBER}/locations/global/workloadIdentityPools/valuebuddy-demo.svc.id.goog/subject/ns/valuebuddy-demo/sa/valuebuddy-reader" --role=roles/storage.objectViewer
```

Autopilot has Workload Identity Federation enabled. No service account key file is
needed. Do not mount your local ADC credentials into Kubernetes.

## 5. Update your EXISTING valuebuddy-kubernetes.yaml

Keep its 2 CPU / 8Gi resources and probes. Under spec.template.spec add:

```yaml
serviceAccountName: valuebuddy-reader
```

Keep automountServiceAccountToken: false. Under the web container change image to:

```yaml
image: us-central1-docker.pkg.dev/valuebuddy-demo/valuebuddy-images/valuebuddy:saved-reports-v1
```

Under the container's existing env list add/update these entries (no duplicates):

```yaml
- name: VALUEBUDDY_BUCKET
  value: valuebuddy-reports-jai-552898460688
- name: GOOGLE_CLOUD_PROJECT
  value: valuebuddy-demo
- name: STREAMLIT_BROWSER_SERVER_ADDRESS
  value: valuebuddy.136.68.63.17.sslip.io
- name: STREAMLIT_BROWSER_SERVER_PORT
  value: "443"
```

Then, from the folder containing your existing YAML files:

```bash
kubectl --context=gke_valuebuddy-demo_us-central1_servicing-agent-cluster -n valuebuddy-demo apply -f valuebuddy-kubernetes.yaml
kubectl --context=gke_valuebuddy-demo_us-central1_servicing-agent-cluster apply -f valuebuddy-https.yaml
kubectl --context=gke_valuebuddy-demo_us-central1_servicing-agent-cluster -n valuebuddy-demo rollout status deployment/valuebuddy --timeout=10m
```

Reapplying valuebuddy-https.yaml preserves the Service's NEG/IAP annotations.
The URL remains https://valuebuddy.136.68.63.17.sslip.io/ . Refresh it after rollout;
existing sessions reset. No ingestion occurs from the web app.

## Verify

Select each report, ask a number question, and inspect the source page against the
PDF. Switch reports and confirm history resets. Refresh and select again: only
saved artifacts load, with no LlamaParse/vision/document-embedding calls. Query
embedding, reranking and answer generation still call paid APIs. A pod restart
redownloads artifacts but does not reparse them. Each report has its own index.

The original 3300-character chunking, MMR retrieval, reranker and answer prompt
are retained. Selected financial tables receive the existing vision extraction;
all pages are sent to LlamaParse. Source-page selection remains a best-match
heuristic and must be checked, especially for answers spanning multiple pages.

## Finish the existing HTTPS setup

After confirming HTTPS works, add this annotation to the Ingress in
valuebuddy-https.yaml and apply it:

```yaml
kubernetes.io/ingress.allow-http: "false"
```

Verify an incognito visit requires Google login, an unapproved account is denied,
and PDF upload is no longer present. IAP protects the external load balancer;
this patch does not add application-level IAP JWT verification or restrict other
workloads' internal access. This is a protected demo, not a complete production
security assessment.

## Rollback

Revert the deployment image and env/serviceAccount edits to your prior manifest
and apply it. GCS reports remain stored. Cloud Run is untouched.
