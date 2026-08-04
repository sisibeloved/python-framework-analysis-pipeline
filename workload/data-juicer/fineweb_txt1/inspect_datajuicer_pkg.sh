#!/usr/bin/env bash
set -euo pipefail

CONTAINER=${CONTAINER:-datajuicer-cinderx-jit-v101}

docker exec "$CONTAINER" bash -lc '
  set +e
  python -m pip show data-juicer data_juicer py-data-juicer
  python -m pip freeze \
    | grep -Ei "data|juice|dill|datasets|pyarrow|spacy|kenlm|fasttext|regex|selectolax|beautifulsoup|bs4" \
    | sed -n "1,200p"
  find /root/.cache/pip /opt/cinderx-wheel-cache /work -maxdepth 5 -type f \
    \( -iname "*data*juicer*" -o -iname "*dill*" -o -iname "*datasets*" -o -iname "*pyarrow*" \) \
    2>/dev/null | sed -n "1,240p"
  find /opt/python314/lib/python3.14/site-packages -maxdepth 1 \
    \( -iname "*data*juicer*" -o -iname "*dill*" -o -iname "*datasets*" -o -iname "*pyarrow*" \) \
    -print | sed -n "1,160p"
'
