# 与当前 Python 候选使用相同的固定 Linux arm64 Python 基础镜像。
ARG PYTHON_IMAGE=python:3.12.13-slim-bookworm@sha256:3f9c7f75b916e8279e24136d7a57769aba9e193715be52570417698c52d6eed0
FROM ${PYTHON_IMAGE}

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_PYTHON_DOWNLOADS=never \
    UV_LINK_MODE=copy \
    LITELLM_LOCAL_MODEL_COST_MAP=True \
    HF_HUB_OFFLINE=1 \
    DO_NOT_TRACK=1 \
    OTEL_SDK_DISABLED=true \
    TIKTOKEN_CACHE_DIR=/opt/tiktoken-cache

COPY harness-lab/bootstrap-requirements.txt /opt/materials/bootstrap-requirements.txt
RUN python -m pip install --no-cache-dir --require-hashes -r /opt/materials/bootstrap-requirements.txt

COPY sources/sdk /opt/materials/sources/sdk
COPY SOURCE_MANIFEST.json LAB_ADAPTER_MANIFEST.json /opt/materials/
COPY harness-lab /opt/materials/harness-lab
WORKDIR /opt/materials/harness-lab
RUN --mount=type=cache,id=harness-lab-openhands-uv-linux-arm64,target=/root/.cache/uv,sharing=locked \
    python verify_source.py --source-only \
    && uv pip install --system --require-hashes --only-binary :all: -r requirements-linux-aarch64.txt \
    && uv pip install --system --no-deps --no-build-isolation /opt/materials/sources/sdk/openhands-sdk \
    && python verify_source.py \
    && uv pip check --system \
    && uv pip freeze --system > /opt/materials/INSTALLED_DISTRIBUTIONS.txt \
    && dpkg-query -W > /opt/materials/SYSTEM_PACKAGES.txt

# tiktoken 固定 wheel 使用内置 SHA256 固定值核验官方编码下载。
# 在运行前准备两种标准编码。
RUN python -c 'import tiktoken; tiktoken.get_encoding("cl100k_base"); tiktoken.get_encoding("o200k_base")' \
    && python -c 'import hashlib,json,pathlib; p=pathlib.Path("/opt/tiktoken-cache"); print(json.dumps({x.name:hashlib.sha256(x.read_bytes()).hexdigest() for x in sorted(p.iterdir())},sort_keys=True))' > /opt/materials/TOKENIZER_CACHE_SHA256.json

ENTRYPOINT ["python", "/opt/materials/harness-lab/entrypoint.py"]
