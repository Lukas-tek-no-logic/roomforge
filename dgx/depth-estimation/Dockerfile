FROM nvcr.io/nvidia/pytorch:25.12-py3

WORKDIR /app

RUN pip install --no-cache-dir \
    fastapi \
    uvicorn[standard] \
    Pillow \
    transformers>=4.45.0 \
    huggingface_hub

COPY service.py .

ENV HF_HOME=/root/.cache/huggingface

CMD ["uvicorn", "service:app", "--host", "0.0.0.0", "--port", "8000"]
