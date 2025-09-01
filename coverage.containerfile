# vim: set filetype=dockerfile
# Coverage-enabled container for code coverage assessment
# Based on the existing Containerfile but modified for coverage collection

ARG LIGHTSPEED_RAG_CONTENT_IMAGE=quay.io/redhat-user-workloads/crt-nshift-lightspeed-tenant/own-app-lightspeed-rag-content@sha256:338e5ec469754d1ac190852ab0d997a99892acbe262bf75971ed91234e3d184a
ARG HERMETIC=false

FROM --platform=linux/amd64 ${LIGHTSPEED_RAG_CONTENT_IMAGE} as lightspeed-rag-content

# Use the base image you specified for coverage testing
FROM quay.io/redhat-user-workloads/crt-nshift-lightspeed-tenant/ols/lightspeed-service@sha256:3907578c627487dfc9cf79785bbaebdc261d50c290205c22d7acfb0966dcab87 as base

# Switch to root to install coverage tools
USER 0

# Install coverage and any additional tools needed
RUN pip3.11 install --no-cache-dir coverage pytest-cov

# Create coverage data directory with proper permissions
RUN mkdir -p /coverage-data && chmod 777 /coverage-data

# Copy coverage configuration if it exists
COPY pyproject.toml /app-root/

# Set coverage environment variables
ENV COVERAGE_FILE=/coverage-data/.coverage
ENV COVERAGE_PROCESS_START=/app-root/pyproject.toml

# Switch back to non-root user
USER 1001

# Override the CMD to use coverage with the original runner
CMD ["coverage", "run", "--data-file=/coverage-data/.coverage", "runner.py"]

LABEL io.k8s.display-name="OpenShift LightSpeed Service (Coverage)" \
      io.k8s.description="AI-powered OpenShift Assistant Service with Coverage Collection." \
      io.openshift.tags="openshift-lightspeed,ols,coverage" \
      description="Red Hat OpenShift Lightspeed Service with Coverage" \
      summary="Red Hat OpenShift Lightspeed Service with Coverage Collection" \
      com.redhat.component=openshift-lightspeed-service-coverage \
      name=openshift-lightspeed-service-coverage \
      vendor="Red Hat, Inc."