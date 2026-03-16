FROM nvcr.io/nvidia/pytorch:25.12-py3

WORKDIR /app

RUN pip install --no-cache-dir \
    diffusers>=0.32.0 \
    transformers>=4.48.0 \
    accelerate>=0.34.0 \
    sentencepiece \
    protobuf \
    fastapi \
    uvicorn[standard] \
    python-multipart \
    Pillow \
    peft

COPY service.py .

ENV HF_HOME=/root/.cache/huggingface
ENV HUGGING_FACE_HUB_TOKEN=""
ENV FLUX_MODEL="lodestones/Chroma"
ENV FLUX_DEPTH_MODEL="black-forest-labs/FLUX.1-Depth-V3"
ENV FLUX_PREVIEW_MODEL="black-forest-labs/FLUX.2-klein"

CMD ["uvicorn", "service:app", "--host", "0.0.0.0", "--port", "8000"]
