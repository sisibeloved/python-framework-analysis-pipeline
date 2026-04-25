#!/usr/bin/env bash
# build-flink-image.sh — Build Flink + Python + PyFlink Docker image
#
# Usage: ./build-flink-image.sh [ARCH]
#   ARCH = x86_64 (default) or aarch64
#
# All configurable values are passed via environment variables.
# The pipeline sets these from environment.yaml; standalone use falls back to defaults.
#
# Prerequisites: Docker installed, this script runs ON the target host.
#
# Expected total time: ~80 min (Python compile ~40 min, pip deps ~15 min, pyarrow C++ ~20 min)

set -euo pipefail

ARCH="${1:-x86_64}"
if [ "$ARCH" = "aarch64" ]; then
    ARCH_TAG="arm"
else
    ARCH_TAG="x86"
fi

# Configurable via environment variables (pipeline passes these from config)
BASE_IMAGE="${BASE_IMAGE:-flink:2.2.0-java17}"
IMAGE_NAME="${IMAGE_NAME:-flink-pyflink:2.2.0-py314-${ARCH_TAG}-final}"
NETWORK="${NETWORK:-flink-network}"
PYTHON_VERSION="${PYTHON_VERSION:-3.14.3}"
TM_COUNT="${TM_COUNT:-2}"
USE_TMPFS="${USE_TMPFS:-true}"
MAKEOPTS="${MAKEOPTS:--j$(($(nproc 2>/dev/null || echo 4) / 2))}"
PYENV_ROOT="/root/.pyenv"
PIP="$PYENV_ROOT/versions/$PYTHON_VERSION/bin/pip --trusted-host pypi.org --trusted-host files.pythonhosted.org --trusted-host pypi.python.org"
PYTHON="$PYENV_ROOT/versions/$PYTHON_VERSION/bin/python3"

# Forward proxy env vars into docker exec calls (container doesn't inherit host env)
DOCKER_PROXY_FLAGS=""
for var in http_proxy https_proxy no_proxy HTTP_PROXY HTTPS_PROXY NO_PROXY; do
    val="${!var:-}"
    if [ -n "$val" ]; then
        DOCKER_PROXY_FLAGS="$DOCKER_PROXY_FLAGS -e $var=$val"
    fi
done

echo "=== Building $IMAGE_NAME on $ARCH ==="
echo "  BASE_IMAGE=$BASE_IMAGE"
echo "  NETWORK=$NETWORK"
echo "  PYTHON_VERSION=$PYTHON_VERSION"
echo "  TM_COUNT=$TM_COUNT"

# ---------------------------------------------------------------------------
# Phase 1: Start base container
# ---------------------------------------------------------------------------
echo ""
echo "[Phase 1/7] Starting base container..."
docker network create "$NETWORK" 2>/dev/null || true
docker rm -f flink-jm 2>/dev/null || true
docker run -d --name flink-jm --hostname flink-jm --network "$NETWORK" -p 8081:8081 \
    "$BASE_IMAGE" jobmanager
sleep 3

# ---------------------------------------------------------------------------
# Phase 2: Install system deps + compile Python
# ---------------------------------------------------------------------------
echo ""
echo "[Phase 2/7] Installing build deps and compiling Python $PYTHON_VERSION..."
echo "  (This takes ~40 min with LTO+PGO on 4 cores)"

docker exec $DOCKER_PROXY_FLAGS -u root flink-jm bash -c "
set -e

# System build deps
apt-get update -qq && apt-get install -y \
    build-essential libssl-dev zlib1g-dev libbz2-dev \
    libreadline-dev libsqlite3-dev libffi-dev \
    liblzma-dev git curl \
    openjdk-17-jdk-headless || exit 1
echo '  System deps installed'

# Disable SSL verification for all network tools (proxy intercepts HTTPS)
git config --global http.sslVerify false
git config --global http.version HTTP/1.1
git config --global http.postBuffer 524288000
git config --global http.lowSpeedLimit 1000
git config --global http.lowSpeedTime 30
echo 'insecure' >> ~/.curlrc
echo 'check_certificate = off' >> ~/.wgetrc
echo 'Acquire::https::Verify-Peer \"false\";' > /etc/apt/apt.conf.d/99no-ssl-verify
echo '  SSL verification disabled, git retry configured'

# Retry wrapper for unreliable proxy connections
retry() {
    local attempts=5 delay=10 cmd=\"\$@\"
    for i in \$(seq 1 \$attempts); do
        echo \"    [retry \$i/\$attempts] \$cmd\"
        if eval \$cmd; then return 0; fi
        echo '    Failed, waiting '\$delay's...'
        sleep \$delay
    done
    echo '    All retries exhausted.'
    return 1
}

# Install pyenv via direct git clone (avoids pyenv.run which has its own
# uncontrolled git/curl calls that can't be individually retried)
echo '  Installing pyenv...'
export PYENV_ROOT=$PYENV_ROOT
export PATH=\$PYENV_ROOT/bin:\$PATH
if [ ! -d \$PYENV_ROOT/.git ]; then
    retry 'git clone https://github.com/pyenv/pyenv.git \$PYENV_ROOT'
fi
for plugin in pyenv-doctor pyenv-installer pyenv-update; do
    if [ ! -d \$PYENV_ROOT/plugins/\$plugin/.git ]; then
        retry \"git clone https://github.com/pyenv/\$plugin.git \$PYENV_ROOT/plugins/\$plugin\"
    fi
done
eval \"\$(pyenv init -)\"

# Compile Python with LTO+PGO
echo '  Compiling Python (this takes ~40 min)...'
CFLAGS='-fno-omit-frame-pointer -mno-omit-leaf-frame-pointer' \
PYTHON_CONFIGURE_OPTS='--enable-optimizations --with-lto' \
MAKEOPTS='$MAKEOPTS' \
pyenv install $PYTHON_VERSION

pyenv global $PYTHON_VERSION
echo '  Python compiled:' \$(python3 --version)
"

echo "[Phase 2/7] Done."

# ---------------------------------------------------------------------------
# Phase 3: Fix pip truststore + install build tools
# ---------------------------------------------------------------------------
echo ""
echo "[Phase 3/7] Fixing pip and installing build tools..."

docker exec $DOCKER_PROXY_FLAGS -u root flink-jm bash -c "
set -e
export PYENV_ROOT=$PYENV_ROOT
export PATH=\$PYENV_ROOT/bin:\$PYENV_ROOT/versions/$PYTHON_VERSION/bin:\$PATH
eval \"\$(pyenv init -)\"
pyenv global $PYTHON_VERSION

# Fix pip truststore (Python 3.14 compatibility)
$PYTHON << 'PYEOF'
import pip._internal.cli.index_command as m
path = m.__file__
with open(path) as f:
    lines = f.readlines()
new_lines = []
skip = False
for line in lines:
    if line.startswith('def _create_truststore_ssl_context'):
        new_lines.append('def _create_truststore_ssl_context() -> None:\n')
        new_lines.append('    return None\n')
        skip = True
        continue
    if skip:
        if line and not line[0].isspace() and line.strip():
            skip = False
            new_lines.append(line)
    else:
        new_lines.append(line)
with open(path, 'w') as f:
    f.writelines(new_lines)
print('  Patched pip truststore')

# Fix certifi — use system CA bundle (standalone certifi not yet installed)
import pip._vendor.certifi as pc, shutil, os
shutil.copy2('/etc/ssl/certs/ca-certificates.crt', os.path.join(os.path.dirname(pc.__file__), 'cacert.pem'))
print('  Fixed certifi cacert.pem')
PYEOF

# Build tools
$PIP install 'Cython>=3.2' setuptools==78.1.0 meson-python ninja meson
echo '  Build tools installed'
"

echo "[Phase 3/7] Done."

# ---------------------------------------------------------------------------
# Phase 4: Install Python dependencies in correct order
# ---------------------------------------------------------------------------
echo ""
echo "[Phase 4/7] Installing Python dependencies..."
echo "  Order matters: numpy first (beam Cythonize needs it), then beam, then flink"

docker exec $DOCKER_PROXY_FLAGS -u root flink-jm bash -c "
set -e
export PYENV_ROOT=$PYENV_ROOT
export PATH=\$PYENV_ROOT/bin:\$PYENV_ROOT/versions/$PYTHON_VERSION/bin:\$PATH

# Retry wrapper (redefined here since this is a new shell)
retry() {
    local attempts=5 delay=10 cmd=\"\$@\"
    for i in \$(seq 1 \$attempts); do
        echo \"    [retry \$i/\$attempts] \$cmd\"
        if eval \$cmd; then return 0; fi
        echo '    Failed, waiting '\$delay's...'
        sleep \$delay
    done
    echo '    All retries exhausted.'
    return 1
}

# 1. numpy (beam's Cythonize requires it at build time)
echo '  [4a] Installing numpy...'
$PIP install numpy

# 2. apache-beam from source (Cython 3.2.4 generates 3.14-compatible C)
echo '  [4b] Installing apache-beam 2.61.0 (from source, ~5 min)...'
$PIP install apache-beam==2.61.0 --no-build-isolation --no-deps

# 3. apache-flink from source
echo '  [4c] Installing apache-flink 2.2.0 (from source)...'
$PIP install py4j==0.10.9.7
$PIP install apache-flink==2.2.0 apache-flink-libraries==2.2.0 --no-build-isolation --no-deps

# 4. Runtime deps (versions verified on kunpeng ARM)
echo '  [4d] Installing runtime dependencies...'
$PIP install \
    dill==0.4.1 \
    sortedcontainers==2.4.0 \
    zstandard==0.25.0 \
    crcmod==1.7 \
    PyYAML==6.0.3 \
    regex==2026.4.4 \
    proto-plus==1.27.2 \
    objsize==0.8.0 \
    jsonpickle==4.1.1 \
    packaging==26.1 \
    protobuf==6.33.6 \
    httplib2==0.31.2 \
    cloudpickle==3.1.2 \
    python-dateutil==2.9.0.post0 \
    pytz==2026.1.post1 \
    requests==2.33.1 \
    fastavro==1.12.1 \
    fasteners==0.20 \
    jsonschema==4.26.0 \
    orjson==3.11.8 \
    pydot==4.0.1 \
    typing-extensions==4.15.0 \
    avro==1.12.1 \
    find_libpython==0.5.1 \
    grpcio==1.80.0 \
    grpcio-tools==1.80.0 \
    ruamel.yaml==0.19.1 \
    pandas

# 5. pyarrow 23 (Python 3.14 requires >=23, source build against system Arrow C++)
echo '  [4e] Installing Apache Arrow C++ dev packages...'
apt-get install -y lsb-release wget
retry wget -q https://apache.jfrog.io/artifactory/arrow/\$(lsb_release --id --short | tr A-Z a-z)/apache-arrow-apt-source-latest-\$(lsb_release --codename --short).deb -O /tmp/arrow-apt.deb
dpkg -i /tmp/arrow-apt.deb
apt-get update -qq
apt-get install -y libarrow-dev libparquet-dev libarrow-dataset-dev libarrow-acero-dev
echo '  Arrow C++ dev packages installed'

# Install cmake (required for pyarrow source build)
$PIP install cmake ninja

# Download pyarrow source manually (pip install fails due to dynamic version 0.0.0)
echo '  [4f] Downloading pyarrow 23.0.1 source...'
cd /tmp && mkdir -p pyarrow-build && cd pyarrow-build
retry curl -sSL https://files.pythonhosted.org/packages/88/22/134986a4cc224d593c1afde5494d18ff629393d74cc2eddb176669f234a4/pyarrow-23.0.1.tar.gz -o pyarrow-23.0.1.tar.gz
tar xzf pyarrow-23.0.1.tar.gz
cd pyarrow-23.0.1

echo '  [4f] Building pyarrow 23.0.1 (~15 min, C++ compilation)...'
PYARROW_WITH_CUDA=0 \
PYARROW_WITH_FLIGHT=0 \
PYARROW_WITH_GANDIVA=0 \
PYARROW_WITH_ORC=0 \
PYARROW_WITH_SUBSTRAIT=0 \
PYARROW_WITH_AZURE=0 \
PYARROW_WITH_GCS=0 \
PYARROW_WITH_S3=0 \
PYARROW_WITH_HDFS=0 \
PYARROW_WITH_PARQUET=1 \
PYARROW_WITH_DATASET=1 \
PYARROW_WITH_ACERO=1 \
CMAKE_BUILD_PARALLEL_LEVEL=$(($(nproc 2>/dev/null || echo 4) / 2)) \
$PIP install . --no-build-isolation
echo '  pyarrow installed'

echo '  All Python deps installed.'
"

echo "[Phase 4/7] Done."

# ---------------------------------------------------------------------------
# Phase 5: Verify + copy flink-python.jar + commit
# ---------------------------------------------------------------------------
echo ""
echo "[Phase 5/7] Verifying and committing image..."

# Determine libpython path based on architecture
if [ "$ARCH" = "aarch64" ]; then
    LIB_DIR="/usr/lib/aarch64-linux-gnu"
else
    LIB_DIR="/usr/lib/x86_64-linux-gnu"
fi
# Extract major.minor from PYTHON_VERSION for .so names
PY_MM="${PYTHON_VERSION%.*}"

docker exec $DOCKER_PROXY_FLAGS -u root flink-jm bash -c "
set -e
export PATH=$PYENV_ROOT/versions/$PYTHON_VERSION/bin:\$PATH

# Verify PyFlink
$PYTHON -c '
from pyflink.table import TableEnvironment, EnvironmentSettings
t = TableEnvironment.create(EnvironmentSettings.in_streaming_mode())
print(\"  PyFlink OK\")
'

# Copy flink-python.jar to lib/ (needed for remote Python UDF submission)
cp /opt/flink/opt/flink-python-*.jar /opt/flink/lib/ 2>/dev/null && echo '  Copied flink-python.jar to lib/' || echo '  flink-python.jar already in lib/'

# Verify javac
javac -version 2>&1 | head -1

# Fix flink user (uid 9999) access to Python
chmod o+x /root
chmod -R o+rX /root/.pyenv
rm -f $LIB_DIR/libpython${PY_MM}.so* $LIB_DIR/libpython3.so
cp /root/.pyenv/versions/$PYTHON_VERSION/lib/libpython${PY_MM}.so.1.0 $LIB_DIR/
cp /root/.pyenv/versions/$PYTHON_VERSION/lib/libpython${PY_MM}.so $LIB_DIR/
cp /root/.pyenv/versions/$PYTHON_VERSION/lib/libpython3.so $LIB_DIR/
ln -sf /root/.pyenv/versions/$PYTHON_VERSION/bin/python3 /usr/local/bin/python3
ldconfig
echo '  Fixed flink user Python access'

# Clean up build artifacts
rm -rf /tmp/pip-* /tmp/python-build.* /tmp/*.whl /tmp/pyarrow-src /tmp/fix_pip*.py /tmp/verify*.py /tmp/pyarrow-build /tmp/arrow-apt.deb
$PYENV_ROOT/versions/$PYTHON_VERSION/bin/pip cache purge
echo '  Cleaned up build artifacts'
"

# Commit the image
docker commit flink-jm "$IMAGE_NAME"
echo "  Committed image: $IMAGE_NAME"

echo "[Phase 5/7] Done."

# ---------------------------------------------------------------------------
# Phase 6: Start TaskManagers
# ---------------------------------------------------------------------------
echo ""
echo "[Phase 6/7] Starting TaskManagers..."

TMPFS_FLAG=""
if [ "$USE_TMPFS" = "true" ]; then
    TMPFS_FLAG="--tmpfs /tmp:rw,exec"
fi

for i in $(seq 1 "$TM_COUNT"); do
    docker rm -f "flink-tm$i" 2>/dev/null || true
    docker run -d --name "flink-tm$i" --network "$NETWORK" \
        -e FLINK_PROPERTIES='jobmanager.rpc.address: flink-jm' \
        $TMPFS_FLAG \
        --privileged \
        -e PYTHONPERFSUPPORT=1 \
        "$IMAGE_NAME" taskmanager
    echo "  Started flink-tm$i"
done

# Wait for TMs to register
echo "  Waiting for TaskManagers to register..."
sleep 15

# ---------------------------------------------------------------------------
# Phase 7: Verify cluster health
# ---------------------------------------------------------------------------
echo ""
echo "[Phase 7/7] Verifying cluster health..."

docker exec $DOCKER_PROXY_FLAGS flink-jm bash -c " | python3 -c 'import sys,json; d=json.load(sys.stdin); print(len(d.get(\"taskmanagers\",[])))')
echo \"  TaskManagers registered: \$TM_COUNT\"
if [ \"\$TM_COUNT\" -lt 2 ]; then
    echo '  WARNING: Less than 2 TMs registered!'
fi
curl -sf http://localhost:8081/overview | python3 -c 'import sys,json; d=json.load(sys.stdin); print(f\"  Slots: {d[\"slots-number\"]}, TMs: {d[\"taskmanagers\"]}\")'
"

# Install perf inside containers
echo "  Installing profiling tools..."
for c in flink-jm $(for i in $(seq 1 "$TM_COUNT"); do echo "flink-tm$i"; done); do
    docker exec $DOCKER_PROXY_FLAGS -u root "$c" bash -c 'apt-get update -qq && apt-get install -y linux-tools-common linux-tools-generic' || true
done

echo ""
echo "=== BUILD COMPLETE ==="
echo "Image: $IMAGE_NAME"
echo "Cluster: flink-jm + flink-tm(1..$TM_COUNT)"
echo "Python: $PYTHON_VERSION"
echo "Ready for benchmark."
