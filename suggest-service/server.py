"""SigLIP 2 embedding service for Flicktag.

Endpoints:
    GET  /health        -> {model, device, dim, logit_scale, logit_bias}
    POST /embed/images  -> multipart files[] -> {"vectors": [[...], ...]}
    POST /embed/texts   -> {"texts": [...]}  -> {"vectors": [[...], ...]}

All vectors are L2-normalised. Images that fail to decode yield null.
"""

import io
import os

import torch
from fastapi import FastAPI, File, UploadFile
from PIL import Image
from pydantic import BaseModel
from transformers import AutoModel, AutoProcessor

MODEL_ID = os.environ.get("FLICKTAG_MODEL", "google/siglip2-base-patch16-224")
DEVICE = "cuda" if torch.cuda.is_available() else "cpu"
DTYPE = torch.float16 if DEVICE == "cuda" else torch.float32

model = AutoModel.from_pretrained(MODEL_ID, torch_dtype=DTYPE).to(DEVICE).eval()
processor = AutoProcessor.from_pretrained(MODEL_ID)

app = FastAPI(title="flicktag-suggest")


class Texts(BaseModel):
    texts: list[str]


def _normalise(features: torch.Tensor) -> list[list[float]]:
    features = features.float()
    features = features / features.norm(dim=-1, keepdim=True)
    return features.cpu().tolist()


@app.get("/health")
def health():
    return {
        "model": MODEL_ID,
        "device": DEVICE,
        "dim": model.config.text_config.hidden_size,
        "logit_scale": float(model.logit_scale.exp().item()),
        "logit_bias": float(model.logit_bias.item()),
    }


@app.post("/embed/images")
async def embed_images(files: list[UploadFile] = File(...)):
    images, slots = [], []
    for index, upload in enumerate(files):
        try:
            image = Image.open(io.BytesIO(await upload.read()))
            image.seek(0)
            images.append(image.convert("RGB"))
            slots.append(index)
        except Exception:
            pass
    vectors: list[list[float] | None] = [None] * len(files)
    if images:
        inputs = processor(images=images, return_tensors="pt").to(DEVICE)
        with torch.inference_mode():
            features = model.get_image_features(pixel_values=inputs["pixel_values"].to(DTYPE))
        for slot, vector in zip(slots, _normalise(features)):
            vectors[slot] = vector
    return {"vectors": vectors}


@app.post("/embed/texts")
def embed_texts(body: Texts):
    if not body.texts:
        return {"vectors": []}
    inputs = processor(
        text=[t.lower() for t in body.texts],
        padding="max_length",
        max_length=64,
        truncation=True,
        return_tensors="pt",
    ).to(DEVICE)
    with torch.inference_mode():
        features = model.get_text_features(**inputs)
    return {"vectors": _normalise(features)}
