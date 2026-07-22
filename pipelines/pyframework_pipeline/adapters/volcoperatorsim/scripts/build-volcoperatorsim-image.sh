#!/usr/bin/env bash
set -euo pipefail

: "${IMAGE_NAME:?IMAGE_NAME is required}"
: "${VOLC_OPERATOR_SIM_REVISION:?VOLC_OPERATOR_SIM_REVISION is required}"
: "${VOLC_BUILD_CONFIG_HASH:?VOLC_BUILD_CONFIG_HASH is required}"

VOLC_OPERATOR_SIM_REPO="${VOLC_OPERATOR_SIM_REPO:-https://gitcode.com/XuanYuL5/volc_operator_sim.git}"
DAFT_CONDA_ENV="${DAFT_CONDA_ENV:-xarch}"
DATAJUICER_CONDA_ENV="${DATAJUICER_CONDA_ENV:-xdj}"
BASE_IMAGE="${VOLC_BASE_IMAGE:-debian:bookworm-slim}"
DEBIAN_MIRROR_HOST="${VOLC_DEBIAN_MIRROR_HOST:-}"
MINIFORGE_URL_TEMPLATE="${VOLC_MINIFORGE_URL_TEMPLATE:-https://github.com/conda-forge/miniforge/releases/download/24.11.3-2/Miniforge3-24.11.3-2-Linux-__ARCH__.sh}"
MINIFORGE_SHA256="${VOLC_MINIFORGE_SHA256:-}"
PYTORCH_CPU_INDEX_URL="${VOLC_PYTORCH_CPU_INDEX_URL:-https://download.pytorch.org/whl/cpu}"
REQUESTED_ARCH="${1:-$(uname -m)}"

if [[ ! "$VOLC_OPERATOR_SIM_REVISION" =~ ^[0-9a-fA-F]{40}$ ]]; then
  echo "VOLC_OPERATOR_SIM_REVISION must be a full 40-character commit SHA" >&2
  exit 2
fi

case "$REQUESTED_ARCH" in
  aarch64 | arm64)
    DOCKER_TARGETARCH="arm64"
    EXPECTED_HOST_ARCH="aarch64"
    ;;
  x86_64 | amd64)
    DOCKER_TARGETARCH="amd64"
    EXPECTED_HOST_ARCH="x86_64"
    ;;
  *)
    echo "unsupported architecture: $REQUESTED_ARCH" >&2
    exit 2
    ;;
esac

HOST_ARCH="$(uname -m)"
case "$HOST_ARCH" in
  arm64) HOST_ARCH="aarch64" ;;
  amd64) HOST_ARCH="x86_64" ;;
esac
if [[ "$HOST_ARCH" != "$EXPECTED_HOST_ARCH" ]]; then
  echo "requested architecture $REQUESTED_ARCH does not match Host architecture $HOST_ARCH" >&2
  exit 2
fi

BUILD_CONTEXT="$(mktemp -d)"
trap 'rm -rf "$BUILD_CONTEXT"' EXIT

cat >"$BUILD_CONTEXT/Dockerfile" <<'DOCKERFILE'
ARG BASE_IMAGE=debian:bookworm-slim
FROM ${BASE_IMAGE}

ARG TARGETARCH
ARG DEBIAN_MIRROR_HOST
ARG MINIFORGE_URL_TEMPLATE
ARG MINIFORGE_SHA256
ARG VOLC_OPERATOR_SIM_REPO
ARG VOLC_OPERATOR_SIM_REVISION
ARG DAFT_CONDA_ENV
ARG DATAJUICER_CONDA_ENV
ARG http_proxy
ARG https_proxy
ARG no_proxy
ARG HTTP_PROXY
ARG HTTPS_PROXY
ARG NO_PROXY

LABEL org.opencontainers.image.source="https://gitcode.com/XuanYuL5/volc_operator_sim" \
      pyframework.volc.revision="${VOLC_OPERATOR_SIM_REVISION}" \
      pyframework.volc.daft-conda-env="${DAFT_CONDA_ENV}" \
      pyframework.volc.datajuicer-conda-env="${DATAJUICER_CONDA_ENV}"

ENV DEBIAN_FRONTEND=noninteractive \
    CONDA_DIR=/opt/conda \
    PATH=/opt/conda/bin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PYTHONDONTWRITEBYTECODE=1

SHELL ["/bin/bash", "-o", "pipefail", "-c"]

RUN if [[ -n "$DEBIAN_MIRROR_HOST" ]]; then \
      while IFS= read -r source; do \
        sed -i \
          -e "s#deb.debian.org#$DEBIAN_MIRROR_HOST#g" \
          -e "s#security.debian.org#$DEBIAN_MIRROR_HOST#g" \
          "$source"; \
      done < <(grep -RIlE 'deb\.debian\.org|security\.debian\.org' /etc/apt 2>/dev/null || true); \
    fi \
    && apt-get -o Acquire::Retries=5 -o Acquire::http::Timeout=30 update \
    && apt-get -o Acquire::Retries=5 -o Acquire::http::Timeout=30 install -y --no-install-recommends \
       binutils \
       build-essential \
       ca-certificates \
       cmake \
       curl \
       ffmpeg \
       git \
       jq \
       libsamplerate0-dev \
       libsndfile1-dev \
       linux-perf \
       make \
       pkg-config \
       poppler-utils \
       procps \
       tesseract-ocr \
       wget \
    && rm -rf /var/lib/apt/lists/*

RUN case "$TARGETARCH" in \
      amd64) miniforge_arch="x86_64" ;; \
      arm64) miniforge_arch="aarch64" ;; \
      *) echo "unsupported Docker TARGETARCH: $TARGETARCH" >&2; exit 2 ;; \
    esac \
    && miniforge_url="$(printf '%s\n' "$MINIFORGE_URL_TEMPLATE" | sed "s/__ARCH__/$miniforge_arch/g")" \
    && curl -fL --retry 5 --retry-delay 3 \
       "$miniforge_url" \
       -o /tmp/miniforge.sh \
    && if [[ -n "$MINIFORGE_SHA256" ]]; then \
         echo "${MINIFORGE_SHA256}  /tmp/miniforge.sh" | sha256sum -c -; \
       fi \
    && bash /tmp/miniforge.sh -b -p /opt/conda \
    && rm -f /tmp/miniforge.sh \
    && conda config --system --set auto_activate_base false \
    && conda config --system --set channel_priority flexible \
    && conda clean -afy

RUN git clone "$VOLC_OPERATOR_SIM_REPO" /opt/volc_operator_sim \
    && cd /opt/volc_operator_sim \
    && git checkout --detach "$VOLC_OPERATOR_SIM_REVISION" \
    && test "$(git rev-parse HEAD)" = "$VOLC_OPERATOR_SIM_REVISION"

ARG PYTORCH_CPU_INDEX_URL
RUN source /opt/conda/etc/profile.d/conda.sh \
    && cd /opt/volc_operator_sim \
    && PIP_EXTRA_INDEX_URL="$PYTORCH_CPU_INDEX_URL" \
       ENV_NAME="$DAFT_CONDA_ENV" FORCE_RECREATE=1 \
       bash scripts/env/rebuild_xarch.sh

RUN source /opt/conda/etc/profile.d/conda.sh \
    && cd /opt/volc_operator_sim \
    && PIP_EXTRA_INDEX_URL="$PYTORCH_CPU_INDEX_URL" \
       SRC="$DAFT_CONDA_ENV" DST="$DATAJUICER_CONDA_ENV" FORCE_RECREATE=1 \
       bash scripts/env/setup_dj_av11_env.sh

RUN /opt/conda/envs/${DATAJUICER_CONDA_ENV}/bin/python -m pip install \
      --no-cache-dir \
      -i https://mirrors.aliyun.com/pypi/simple/ \
      --trusted-host mirrors.aliyun.com \
      selectolax==0.4.11

RUN /opt/conda/envs/${DATAJUICER_CONDA_ENV}/bin/python -m pip install \
      --no-cache-dir --no-deps \
      --index-url "$PYTORCH_CPU_INDEX_URL" \
      torchcodec==0.15.0+cpu

RUN mkdir -p /opt/volc_operator_sim/.pyframework \
    && conda list -n "$DAFT_CONDA_ENV" --explicit \
       > /opt/volc_operator_sim/.pyframework/xarch-conda-explicit.txt \
    && conda list -n "$DATAJUICER_CONDA_ENV" --explicit \
       > /opt/volc_operator_sim/.pyframework/xdj-conda-explicit.txt \
    && git -C /opt/volc_operator_sim rev-parse HEAD \
       > /opt/volc_operator_sim/.pyframework/revision.txt \
    && ln -sf "/opt/conda/envs/${DAFT_CONDA_ENV}/bin/py-spy" /usr/local/bin/py-spy \
    && test -x "/opt/conda/envs/${DAFT_CONDA_ENV}/bin/python" \
    && test -x "/opt/conda/envs/${DATAJUICER_CONDA_ENV}/bin/python" \
    && command -v perf \
    && command -v objdump \
    && command -v readelf \
    && command -v py-spy

ENV DAFT_CONDA_ENV=${DAFT_CONDA_ENV} \
    DATAJUICER_CONDA_ENV=${DATAJUICER_CONDA_ENV} \
    DAFT_PY=/opt/conda/envs/${DAFT_CONDA_ENV}/bin/python \
    DJ_PY=/opt/conda/envs/${DATAJUICER_CONDA_ENV}/bin/python

WORKDIR /opt/volc_operator_sim
CMD ["sleep", "infinity"]
DOCKERFILE

proxy_args=()
for name in http_proxy https_proxy no_proxy HTTP_PROXY HTTPS_PROXY NO_PROXY; do
  if [[ -n "${!name:-}" ]]; then
    proxy_args+=(--build-arg "$name=${!name}")
  fi
done

docker build \
  --label "pyframework.volc.revision=$VOLC_OPERATOR_SIM_REVISION" \
  --label "pyframework.volc.build-config=$VOLC_BUILD_CONFIG_HASH" \
  --build-arg "BASE_IMAGE=$BASE_IMAGE" \
  --build-arg "TARGETARCH=$DOCKER_TARGETARCH" \
  --build-arg "DEBIAN_MIRROR_HOST=$DEBIAN_MIRROR_HOST" \
  --build-arg "MINIFORGE_URL_TEMPLATE=$MINIFORGE_URL_TEMPLATE" \
  --build-arg "MINIFORGE_SHA256=$MINIFORGE_SHA256" \
  --build-arg "PYTORCH_CPU_INDEX_URL=$PYTORCH_CPU_INDEX_URL" \
  --build-arg "VOLC_OPERATOR_SIM_REPO=$VOLC_OPERATOR_SIM_REPO" \
  --build-arg "VOLC_OPERATOR_SIM_REVISION=$VOLC_OPERATOR_SIM_REVISION" \
  --build-arg "DAFT_CONDA_ENV=$DAFT_CONDA_ENV" \
  --build-arg "DATAJUICER_CONDA_ENV=$DATAJUICER_CONDA_ENV" \
  "${proxy_args[@]}" \
  -t "$IMAGE_NAME" \
  "$BUILD_CONTEXT"

docker image inspect "$IMAGE_NAME" \
  --format '{{index .Config.Labels "pyframework.volc.revision"}}' \
  | grep -Fx "$VOLC_OPERATOR_SIM_REVISION"
docker image inspect "$IMAGE_NAME" \
  --format '{{index .Config.Labels "pyframework.volc.build-config"}}' \
  | grep -Fx "$VOLC_BUILD_CONFIG_HASH"
