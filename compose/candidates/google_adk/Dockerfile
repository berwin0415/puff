ARG PYTHON_IMAGE=python:3.12.13-slim-bookworm@sha256:3f9c7f75b916e8279e24136d7a57769aba9e193715be52570417698c52d6eed0
FROM ${PYTHON_IMAGE}

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_PROJECT_ENVIRONMENT=/opt/venv \
    UV_PYTHON_DOWNLOADS=never \
    UV_LINK_MODE=copy \
    LITELLM_TELEMETRY=false \
    LITELLM_LOCAL_MODEL_COST_MAP=true \
    DO_NOT_TRACK=1 \
    OTEL_SDK_DISABLED=true

COPY harness-lab/bootstrap-requirements.txt /opt/materials/bootstrap-requirements.txt
RUN python -m pip install --no-cache-dir --require-hashes -r /opt/materials/bootstrap-requirements.txt

COPY sources/main /opt/materials/sources/main
COPY SOURCE_MANIFEST.json /opt/materials/
COPY harness-lab/verify_source.py /opt/materials/source-verifier/verify_source.py
COPY harness-lab/pyproject.toml harness-lab/uv.lock /opt/materials/harness-lab/
WORKDIR /opt/materials/harness-lab
RUN --mount=type=cache,id=harness-lab-google-adk-uv-linux-arm64,target=/root/.cache/uv,sharing=locked \
    python /opt/materials/source-verifier/verify_source.py --source-only \
    && uv sync --frozen --no-dev --no-install-local --no-build \
    && uv sync --frozen --no-dev --no-editable --no-build-isolation \
    && python /opt/materials/source-verifier/verify_source.py --source-only

COPY LAB_ADAPTER_MANIFEST.json /opt/materials/
COPY harness-lab /opt/materials/harness-lab
RUN /opt/venv/bin/python /opt/materials/harness-lab/verify_source.py \
    && uv pip freeze --python /opt/venv/bin/python > /opt/materials/INSTALLED_DISTRIBUTIONS.txt \
    && dpkg-query -W > /opt/materials/SYSTEM_PACKAGES.txt

ENTRYPOINT ["/opt/venv/bin/python", "/opt/materials/harness-lab/entrypoint.py"]
