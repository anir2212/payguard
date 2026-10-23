FROM python:3.12-slim
WORKDIR /opt/payguard
RUN apt-get update && apt-get install -y --no-install-recommends libgomp1 \
    && rm -rf /var/lib/apt/lists/*
COPY pyproject.toml uv.lock ./
COPY src ./src
RUN pip install --no-cache-dir uv==0.11.21 \
    && uv sync --locked --extra serve --extra export --extra agent --extra postgres --no-dev \
    && .venv/bin/python -m payguard_fraud.train --output artifacts \
    && .venv/bin/python -m payguard_fraud.export_onnx --artifacts artifacts
ENV PAYGUARD_MODEL_DIR=/opt/payguard/artifacts
EXPOSE 18086
CMD [".venv/bin/uvicorn", "payguard_fraud.score_api:app", "--host", "0.0.0.0", "--port", "18086"]
