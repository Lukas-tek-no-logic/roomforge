FROM nvcr.io/nvidia/pytorch:25.12-py3

WORKDIR /app

# CHORD dependencies
RUN pip install --no-cache-dir \
    fastapi \
    uvicorn[standard] \
    python-multipart \
    Pillow \
    huggingface_hub[hf_xet] \
    diffusers>=0.32.0 \
    transformers==4.57.1 \
    tokenizers==0.22.1 \
    omegaconf \
    imageio \
    typer \
    tqdm

# Clone CHORD repo
RUN git clone --depth 1 https://github.com/ubisoft/ubisoft-laforge-chord.git /app/chord_repo

COPY service.py .

ENV HF_HOME=/root/.cache/huggingface
ENV CHORD_REPO=/app/chord_repo

CMD ["uvicorn", "service:app", "--host", "0.0.0.0", "--port", "8000"]
