#!/usr/bin/env bash
set -euo pipefail

ARCH_TAG="${1:-$(uname -m)}"
IMAGE_NAME="${IMAGE_NAME:-udf-benchmarking-bench:py311-${ARCH_TAG}}"
BASE_IMAGE="${BASE_IMAGE:-python:3.11-slim}"
UDF_BENCHMARKING_REPO="${UDF_BENCHMARKING_REPO:-https://gitcode.com/stone31415/UDF_Benchmarking.git}"
UDF_BENCHMARKING_REVISION="${UDF_BENCHMARKING_REVISION:-}"
NUMPY_VERSION="${NUMPY_VERSION:-2.4.3}"
PY_SPY_VERSION="${PY_SPY_VERSION:-}"

tmpdir="$(mktemp -d)"
trap 'rm -rf "$tmpdir"' EXIT

cat >"$tmpdir/Dockerfile" <<'DOCKERFILE'
ARG BASE_IMAGE
FROM ${BASE_IMAGE}

ARG UDF_BENCHMARKING_REPO
ARG UDF_BENCHMARKING_REVISION
ARG NUMPY_VERSION
ARG PY_SPY_VERSION
ARG APT_MIRROR
ARG APT_SECURITY_MIRROR
ARG PIP_INDEX_URL
ARG PIP_EXTRA_INDEX_URL
ARG PIP_TRUSTED_HOST
ARG PIP_TIMEOUT
ARG PIP_RETRIES
ARG http_proxy
ARG https_proxy
ARG no_proxy
ARG HTTP_PROXY
ARG HTTPS_PROXY
ARG NO_PROXY

ENV PYTHONUNBUFFERED=1 \
    PIP_INDEX_URL=${PIP_INDEX_URL} \
    PIP_EXTRA_INDEX_URL=${PIP_EXTRA_INDEX_URL} \
    PIP_TRUSTED_HOST=${PIP_TRUSTED_HOST} \
    PIP_TIMEOUT=${PIP_TIMEOUT} \
    PIP_RETRIES=${PIP_RETRIES}

RUN set -eux; \
    write_debian_sources() { \
        mirror="$1"; \
        security_mirror="$2"; \
        codename="$(. /etc/os-release && printf '%s' "${VERSION_CODENAME:-bookworm}")"; \
        printf 'Types: deb\nURIs: %s\nSuites: %s %s-updates\nComponents: main\nSigned-By: /usr/share/keyrings/debian-archive-keyring.gpg\n\nTypes: deb\nURIs: %s\nSuites: %s-security\nComponents: main\nSigned-By: /usr/share/keyrings/debian-archive-keyring.gpg\n' "$mirror" "$codename" "$codename" "$security_mirror" "$codename" > /etc/apt/sources.list.d/debian.sources; \
        rm -f /etc/apt/sources.list; \
    }; \
    if [ -n "${APT_MIRROR}" ]; then \
        security_mirror="${APT_SECURITY_MIRROR:-${APT_MIRROR%/}-security}"; \
        write_debian_sources "${APT_MIRROR}" "${security_mirror}"; \
    fi; \
    printf '%s\n' \
        'Acquire::http::Timeout "30";' \
        'Acquire::https::Timeout "30";' \
        'Acquire::https::Verify-Peer "false";' \
        'Acquire::https::Verify-Host "false";' \
        'Acquire::Retries "5";' \
        > /etc/apt/apt.conf.d/99pyframework-timeouts; \
    if ! apt-get update; then \
        echo "Configured apt mirror failed; retrying default Debian mirror"; \
        write_debian_sources "https://deb.debian.org/debian" "https://deb.debian.org/debian-security"; \
        apt-get update; \
    fi; \
    DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
        bash \
        binutils \
        ca-certificates \
        curl \
        git \
        numactl \
        procps \
        libglib2.0-0 \
        libgl1 \
        libgomp1; \
    if ! DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends linux-perf; then \
        DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends linux-tools-common linux-tools-generic; \
        perf_real="$(find /usr/lib/linux-tools -name perf 2>/dev/null | sort -V | tail -1 || true)"; \
        if [ -n "$perf_real" ]; then \
            ln -sf "$perf_real" /usr/local/bin/perf; \
        fi; \
    fi; \
    command -v perf; \
    rm -rf /var/lib/apt/lists/*

RUN set -eux; \
    pip_trusted_hosts="${PIP_TRUSTED_HOST:-} pypi.org files.pythonhosted.org"; \
    pip_trusted_args=""; \
    for host in $pip_trusted_hosts; do \
        pip_trusted_args="$pip_trusted_args --trusted-host $host"; \
    done; \
    python -m pip install $pip_trusted_args --upgrade pip setuptools wheel; \
    python -m pip install $pip_trusted_args \
        getdaft \
        "numpy==${NUMPY_VERSION}" \
        opencv-python-headless \
        psutil \
        pyyaml \
        scikit-image; \
    if [ -n "${PY_SPY_VERSION}" ]; then \
        python -m pip install $pip_trusted_args "py-spy==${PY_SPY_VERSION}"; \
    else \
        python -m pip install $pip_trusted_args py-spy; \
    fi; \
    python -c "import daft, cv2, skimage, psutil, yaml; print('udfbenchmarking dependencies ready')"; \
    py-spy --version

RUN set -eux; \
    export GIT_SSL_NO_VERIFY=true; \
    git clone --depth 1 "${UDF_BENCHMARKING_REPO}" /opt/UDF_Benchmarking; \
    if [ -n "${UDF_BENCHMARKING_REVISION}" ]; then \
        cd /opt/UDF_Benchmarking; \
        checkout_revision=0; \
        git checkout "${UDF_BENCHMARKING_REVISION}" && checkout_revision=1 || true; \
        if [ "$checkout_revision" = "0" ]; then \
            git fetch --depth 1 origin "${UDF_BENCHMARKING_REVISION}" && git checkout "${UDF_BENCHMARKING_REVISION}" && checkout_revision=1 || true; \
        fi; \
        if [ "$checkout_revision" = "0" ]; then \
            echo "Unable to checkout ${UDF_BENCHMARKING_REVISION}; continuing with default checkout $(git rev-parse HEAD)"; \
        fi; \
    fi; \
    cd /opt/UDF_Benchmarking; \
    python -c "import pathlib; assert pathlib.Path('main.py').is_file(); print('UDF_Benchmarking source ready')"

WORKDIR /workspace/benchmark
CMD ["sleep", "infinity"]
DOCKERFILE

build_args=(
  --build-arg "BASE_IMAGE=${BASE_IMAGE}"
  --build-arg "UDF_BENCHMARKING_REPO=${UDF_BENCHMARKING_REPO}"
  --build-arg "UDF_BENCHMARKING_REVISION=${UDF_BENCHMARKING_REVISION:-}"
  --build-arg "NUMPY_VERSION=${NUMPY_VERSION:-2.4.3}"
  --build-arg "PY_SPY_VERSION=${PY_SPY_VERSION:-}"
  --build-arg "APT_MIRROR=${APT_MIRROR:-}"
  --build-arg "APT_SECURITY_MIRROR=${APT_SECURITY_MIRROR:-}"
  --build-arg "PIP_INDEX_URL=${PIP_INDEX_URL:-}"
  --build-arg "PIP_EXTRA_INDEX_URL=${PIP_EXTRA_INDEX_URL:-}"
  --build-arg "PIP_TRUSTED_HOST=${PIP_TRUSTED_HOST:-}"
  --build-arg "PIP_TIMEOUT=${PIP_TIMEOUT:-}"
  --build-arg "PIP_RETRIES=${PIP_RETRIES:-}"
  --build-arg "http_proxy=${http_proxy:-}"
  --build-arg "https_proxy=${https_proxy:-}"
  --build-arg "no_proxy=${no_proxy:-}"
  --build-arg "HTTP_PROXY=${HTTP_PROXY:-}"
  --build-arg "HTTPS_PROXY=${HTTPS_PROXY:-}"
  --build-arg "NO_PROXY=${NO_PROXY:-}"
)

echo "Building ${IMAGE_NAME} from ${BASE_IMAGE} for ${ARCH_TAG}"
docker build "${build_args[@]}" -t "${IMAGE_NAME}" "$tmpdir"

echo "Built ${IMAGE_NAME}"
