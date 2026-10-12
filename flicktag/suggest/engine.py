"""Tag suggestions: zero-shot SigLIP scores, replaced per tag by a classifier
trained on the user's own validations once enough examples exist.
"""

from __future__ import annotations

import threading

import numpy as np

from flicktag.core.store import Store, Tag
from flicktag.suggest.classifier import LogisticRegression
from flicktag.suggest.client import SuggestClient

MIN_EXAMPLES = 10  # per class (positive and negative) before a tag gets a classifier
RETRAIN_EVERY = 25
DEFAULT_THRESHOLD = 0.5
EMBED_BATCH = 8


def default_prompt(tag: Tag) -> str:
    return tag.prompt or f"a photo of {tag.name}"


def _softmax(values: np.ndarray) -> np.ndarray:
    shifted = np.exp(values - values.max())
    return shifted / shifted.sum()


class SuggestionEngine:
    def __init__(self, store: Store, client: SuggestClient):
        self.store = store
        self.client = client
        self._lock = threading.Lock()
        self._texts: dict[str, np.ndarray] = {}
        self._classifiers: dict[int, LogisticRegression] = {}
        self._since_training = 0

    # ------------------------------------------------------------- embeddings

    @property
    def model(self) -> str:
        return self.client.info().model

    def embedding(self, image_id: int) -> np.ndarray | None:
        blob, model = self.store.get_embedding(image_id)
        if blob is None or model != self.model:
            return None
        return np.frombuffer(blob, dtype=np.float16).astype(np.float32)

    def ensure_embeddings(self, image_ids: list[int]) -> None:
        """Embed the images that have no embedding for the current model (batched)."""
        model = self.model
        missing = [i for i in image_ids if self.store.get_embedding(i)[1] != model]
        for start in range(0, len(missing), EMBED_BATCH):
            batch = missing[start : start + EMBED_BATCH]
            vectors = self.client.embed_images([self.store.image_path(i) for i in batch])
            for image_id, vector in zip(batch, vectors):
                if vector is not None:
                    self.store.update_image(
                        image_id, embedding=vector.astype(np.float16).tobytes(), embed_model=model
                    )

    def _text_vectors(self, prompts: list[str]) -> dict[str, np.ndarray]:
        with self._lock:
            missing = [p for p in dict.fromkeys(prompts) if p not in self._texts]
        if missing:
            vectors = self.client.embed_texts(missing)
            with self._lock:
                self._texts.update(zip(missing, vectors))
        with self._lock:
            return {p: self._texts[p] for p in prompts}

    # ---------------------------------------------------------------- scoring

    def scores(self, image_id: int) -> dict[int, float]:
        """Probability per tag id. Raises client.Offline when the service is down."""
        self.ensure_embeddings([image_id])
        vector = self.embedding(image_id)
        if vector is None:
            return {}
        info = self.client.info()
        tags = self.store.tags()
        texts = self._text_vectors([default_prompt(t) for t in tags])
        with self._lock:
            classifiers = dict(self._classifiers)

        result: dict[int, float] = {}
        by_category: dict[int, list[Tag]] = {}
        for tag in tags:
            by_category.setdefault(tag.category_id, []).append(tag)
        for category_tags in by_category.values():
            logits = np.array(
                [info.logit_scale * float(vector @ texts[default_prompt(t)]) + info.logit_bias for t in category_tags]
            )
            zero_shot = _softmax(logits)
            for tag, p in zip(category_tags, zero_shot):
                classifier = classifiers.get(tag.id)
                result[tag.id] = float(classifier.predict_proba(vector)[0]) if classifier else float(p)
        return result

    def preselect(self, scores: dict[int, float]) -> set[int]:
        threshold = float(self.store.get_setting("suggest_threshold", DEFAULT_THRESHOLD))
        return preselect(self.store, scores, threshold)

    # --------------------------------------------------------------- training

    def note_validation(self) -> bool:
        """Count a validation; True when it is time to retrain."""
        with self._lock:
            self._since_training += 1
            if self._since_training >= RETRAIN_EVERY:
                self._since_training = 0
                return True
        return False

    def training_set(self) -> tuple[list[int], np.ndarray, dict[int, set[int]]]:
        """Validated images with an embedding: (ids, matrix, tag ids per image)."""
        model = self.model
        rows = self.store.query(
            "SELECT id, embedding FROM images WHERE status = 'tagged' AND embed_model = ? "
            "AND embedding IS NOT NULL ORDER BY id",
            (model,),
        )
        if not rows:
            return [], np.zeros((0, 0), dtype=np.float32), {}
        ids = [r["id"] for r in rows]
        matrix = np.stack([np.frombuffer(r["embedding"], dtype=np.float16).astype(np.float32) for r in rows])
        tags = {i: self.store.image_tag_ids(i) for i in ids}
        return ids, matrix, tags

    def retrain(self) -> dict[int, int]:
        """Fit one classifier per tag with enough examples; return {tag_id: n_samples}.

        Every validated image is an example for every tag: the user saw the whole
        tag bar, so a tag left unchecked is a negative.
        """
        ids, matrix, image_tags = self.training_set()
        fitted: dict[int, LogisticRegression] = {}
        sizes: dict[int, int] = {}
        for tag in self.store.tags():
            labels = np.array([1.0 if tag.id in image_tags[i] else 0.0 for i in ids])
            positives = int(labels.sum())
            if positives < MIN_EXAMPLES or len(labels) - positives < MIN_EXAMPLES:
                continue
            fitted[tag.id] = LogisticRegression().fit(matrix, labels)
            sizes[tag.id] = len(labels)
        with self._lock:
            self._classifiers = fitted
        return sizes

    def trained_tags(self) -> set[int]:
        with self._lock:
            return set(self._classifiers)


def preselect(store: Store, scores: dict[int, float], threshold: float) -> set[int]:
    """Tags to pre-check: above threshold; in an exclusive category, only the best one."""
    chosen: set[int] = set()
    for category in store.categories():
        candidates = [(scores[t.id], t.id) for t in store.tags(category.id) if t.id in scores]
        above = [(p, tag_id) for p, tag_id in candidates if p >= threshold]
        if not above:
            continue
        if category.exclusive:
            chosen.add(max(above)[1])
        else:
            chosen.update(tag_id for _, tag_id in above)
    return chosen
