# Code Coverage Assessment for OLS Lightspeed Service

This guide provides step-by-step instructions for assessing code coverage of the OLS lightspeed service running in Kubernetes.

## Overview

The coverage setup includes:
- **Coverage-enabled container**: Modified container image that collects coverage data during runtime
- **Persistent storage**: PVC to preserve coverage data across pod restarts  
- **Collection scripts**: Automated scripts to generate and extract coverage reports
- **Kubernetes manifests**: Deployments, services, and jobs for coverage testing

## Files Created

- `coverage.containerfile` - Dockerfile for coverage-enabled OLS image
- `k8s-coverage-pvc.yaml` - PVC and ConfigMap for coverage data persistence
- `k8s-coverage-deployment.yaml` - Deployment, Service, and Route for coverage-enabled OLS
- `coverage-job.yaml` - Jobs for coverage collection and extraction
- `README-coverage.md` - This instructions file

## Prerequisites

- Access to OpenShift/Kubernetes cluster where OLS is deployed
- `kubectl` or `oc` CLI tools configured
- Container registry access to build and push the coverage image
- Running e2e test suite

## Step-by-Step Instructions

### Step 1: Build Coverage-Enabled Container Image

```bash
# Build the coverage-enabled image
podman build -f coverage.containerfile -t ols-coverage:latest .

# Tag and push to your registry (replace with your registry URL)
podman tag ols-coverage:latest <YOUR_REGISTRY>/ols-coverage:latest
podman push <YOUR_REGISTRY>/ols-coverage:latest
```

### Step 2: Update Kubernetes Manifests

Edit the image URLs in the following files:
- `k8s-coverage-deployment.yaml` (line 36)
- `coverage-job.yaml` (lines 25 and 73)

Replace `<COVERAGE_IMAGE_URL>` with your actual image URL:
```yaml
image: <YOUR_REGISTRY>/ols-coverage:latest
```

### Step 3: Deploy Coverage Infrastructure

```bash
# Create the PVC and ConfigMap
kubectl apply -f k8s-coverage-pvc.yaml

# Verify PVC is bound
kubectl get pvc ols-coverage-data -n openshift-lightspeed
```

### Step 4: Deploy Coverage-Enabled OLS Service

```bash
# Deploy the coverage-enabled OLS service
kubectl apply -f k8s-coverage-deployment.yaml

# Wait for deployment to be ready
kubectl wait --for=condition=available deployment/ols-coverage -n openshift-lightspeed --timeout=300s

# Check pod status
kubectl get pods -l app=ols-coverage -n openshift-lightspeed
```

### Step 5: Run E2E Tests

Run your e2e tests against the coverage-enabled OLS service:

```bash
# If using the existing test suite, update the target URL to point to the coverage service
# Example: Update test configuration to use ols-coverage-route URL

# Run e2e tests using the existing make target
make test-e2e

# Or run specific e2e test suites
pdm run pytest tests/e2e --target-url=https://ols-coverage-route-openshift-lightspeed.apps.your-cluster.com
```

### Step 6: Collect Coverage Data

```bash
# Deploy the coverage collector job
kubectl apply -f coverage-job.yaml

# Monitor the coverage collection job
kubectl logs job/ols-coverage-collector -n openshift-lightspeed -f

# Wait for job completion
kubectl wait --for=condition=complete job/ols-coverage-collector -n openshift-lightspeed --timeout=600s
```

### Step 7: Extract Coverage Reports

```bash
# Method 1: Use the extraction script from local machine
kubectl cp openshift-lightspeed/ols-coverage-extractor:/scripts/extract-coverage.sh ./extract-coverage.sh
chmod +x ./extract-coverage.sh
./extract-coverage.sh ols-coverage openshift-lightspeed ./coverage-results

# Method 2: Manual extraction
mkdir -p ./coverage-results
kubectl cp openshift-lightspeed/ols-coverage:/coverage-data ./coverage-results

# View coverage summary
kubectl exec deployment/ols-coverage -n openshift-lightspeed -- \
  coverage report -m --data-file=/coverage-data/.coverage
```

### Step 8: View Coverage Results

```bash
# Open HTML coverage report
cd coverage-results
python3 -m http.server 8000
# Navigate to http://localhost:8000/htmlcov in your browser

# View JSON coverage data
cat coverage-results/coverage.json | python3 -m json.tool

# Check coverage percentage
python3 -c "
import json
with open('coverage-results/coverage.json') as f:
    data = json.load(f)
    print(f'Total coverage: {data[\"totals\"][\"percent_covered\"]:.2f}%')
    print(f'Lines covered: {data[\"totals\"][\"covered_lines\"]}/{data[\"totals\"][\"num_statements\"]}')
"
```

## Coverage Thresholds

Based on the existing project configuration (`pyproject.toml`):
- **Unit test coverage threshold**: 90%
- **Combined coverage threshold**: 94%

## Cleanup

```bash
# Remove coverage resources
kubectl delete -f coverage-job.yaml
kubectl delete -f k8s-coverage-deployment.yaml
kubectl delete -f k8s-coverage-pvc.yaml

# Note: This will delete the PVC and all coverage data
```

## Troubleshooting

### Coverage Data Not Generated
- Check if the coverage runner is working: `kubectl logs deployment/ols-coverage -n openshift-lightspeed`
- Verify PVC is mounted: `kubectl describe pod <ols-coverage-pod> -n openshift-lightspeed`
- Ensure coverage tools are installed: `kubectl exec deployment/ols-coverage -n openshift-lightspeed -- coverage --version`

### E2E Tests Failing
- Verify the coverage-enabled service is responding: `kubectl port-forward service/ols-coverage-service 8080:8080 -n openshift-lightspeed`
- Check service logs: `kubectl logs deployment/ols-coverage -n openshift-lightspeed`
- Ensure all required environment variables and config maps are present

### Low Coverage Numbers
- Verify e2e tests are actually hitting the coverage-enabled service
- Check that coverage collection is starting before tests run
- Review test coverage configuration in `pyproject.toml`

## Integration with Existing Makefile

The existing Makefile already includes comprehensive coverage targets:
- `make test-unit` - Unit tests with coverage
- `make test-integration` - Integration tests with coverage  
- `make coverage-report` - Generate HTML coverage reports
- `make check-coverage` - Combined coverage validation

This coverage setup complements the existing tooling by providing runtime coverage assessment during e2e testing scenarios.