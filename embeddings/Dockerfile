# CPU-only on purpose: fastembed uses ONNX Runtime, so no torch, no CUDA, and
# an image that stays under a few hundred MB. At this app's scale the GPU
# would be idle anyway.
FROM python:3.12-slim

WORKDIR /app
RUN pip install --no-cache-dir \
      "fastembed==0.5.1" \
      "fastapi==0.115.6" \
      "uvicorn[standard]==0.34.0"

COPY server.py .

# Bake the model into the image so the first request isn't a 130MB download
# and the container works offline.
ARG EMBEDDING_MODEL=BAAI/bge-small-en-v1.5
ENV EMBEDDING_MODEL=${EMBEDDING_MODEL}
RUN python -c "from fastembed import TextEmbedding; TextEmbedding(model_name='${EMBEDDING_MODEL}')"

EXPOSE 8080
CMD ["uvicorn", "server:app", "--host", "0.0.0.0", "--port", "8080"]
