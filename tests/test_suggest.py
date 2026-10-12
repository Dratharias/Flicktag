import json

import httpx
import numpy as np
import pytest
from conftest import tag_id

from flicktag.core import library
from flicktag.core.tagging import validate
from flicktag.suggest.classifier import LogisticRegression
from flicktag.suggest.client import Offline, SuggestClient
from flicktag.suggest.engine import MIN_EXAMPLES, SuggestionEngine, preselect

DIM = 8


def test_logistic_regression_separates_classes():
    rng = np.random.default_rng(0)
    x = np.vstack([rng.normal(1.0, 0.3, (40, DIM)), rng.normal(-1.0, 0.3, (60, DIM))])
    y = np.array([1] * 40 + [0] * 60)
    model = LogisticRegression().fit(x, y)
    p = model.predict_proba(x)
    assert (p[:40] > 0.9).all() and (p[40:] < 0.1).all()


def test_logistic_regression_handles_imbalance():
    rng = np.random.default_rng(1)
    x = np.vstack([rng.normal(0.5, 0.4, (10, DIM)), rng.normal(-0.5, 0.4, (190, DIM))])
    y = np.array([1] * 10 + [0] * 190)
    p = LogisticRegression().fit(x, y).predict_proba(x)
    assert p[:10].mean() > 0.5


def test_unfitted_classifier_raises():
    with pytest.raises(RuntimeError):
        LogisticRegression().predict_proba(np.zeros(DIM))


class FakeService:
    """In-process stand-in for the container: deterministic vectors."""

    def __init__(self):
        self.up = True
        self.image_calls = 0
        self.image_vectors: dict[str, np.ndarray] = {}
        self.text_vectors: dict[str, np.ndarray] = {}

    def handler(self, request: httpx.Request) -> httpx.Response:
        if not self.up:
            raise httpx.ConnectError("refused")
        if request.url.path == "/health":
            return httpx.Response(
                200, json={"model": "fake", "device": "cpu", "dim": DIM, "logit_scale": 10.0, "logit_bias": -5.0}
            )
        if request.url.path == "/embed/texts":
            texts = json.loads(request.content)["texts"]
            return httpx.Response(200, json={"vectors": [self.text_vectors[t].tolist() for t in texts]})
        if request.url.path == "/embed/images":
            self.image_calls += 1
            body = request.read().decode("latin-1")
            names = [part.split('filename="')[1].split('"')[0] for part in body.split("Content-Disposition")[1:]]
            return httpx.Response(200, json={"vectors": [self.image_vectors[n].tolist() for n in names]})
        return httpx.Response(404)


def unit(i: int) -> np.ndarray:
    v = np.zeros(DIM, dtype=np.float32)
    v[i] = 1.0
    return v


@pytest.fixture
def service():
    return FakeService()


@pytest.fixture
def client(service):
    return SuggestClient(transport=httpx.MockTransport(service.handler))


@pytest.fixture
def lib(seeded, photos):
    lib = seeded.add_library(photos)
    library.scan(seeded, lib)
    return lib


def prompts_to_axes(store, service):
    """Give every tag prompt its own axis so zero-shot is predictable."""
    from flicktag.suggest.engine import default_prompt

    for i, tag in enumerate(store.tags()):
        service.text_vectors[default_prompt(tag)] = unit(i % DIM)


def test_zero_shot_scores_and_embedding_cache(seeded, lib, service, client):
    prompts_to_axes(seeded, service)
    figure = seeded.tags()[0]  # first tag (subject/figure) -> axis 0
    for image in seeded.images(lib.id):
        service.image_vectors[seeded.image_path(image).name] = unit(0)
    engine = SuggestionEngine(seeded, client)
    image = seeded.images(lib.id)[0]
    scores = engine.scores(image.id)
    subject = [t.id for t in seeded.tags(figure.category_id)]
    assert max(subject, key=scores.get) == figure.id
    assert scores[figure.id] > 0.99  # softmax within the category
    engine.scores(image.id)
    assert service.image_calls == 1  # embedding cached in the DB


def test_preselect_respects_threshold_and_exclusivity(seeded):
    standing = tag_id(seeded, "pose", "standing")
    sitting = tag_id(seeded, "pose", "sitting")
    figure = tag_id(seeded, "subject", "figure")
    hand = tag_id(seeded, "subject", "hand")
    animal = tag_id(seeded, "subject", "animal")
    scores = {standing: 0.8, sitting: 0.9, figure: 0.7, hand: 0.6, animal: 0.2}
    assert preselect(seeded, scores, 0.5) == {sitting, figure, hand}
    assert preselect(seeded, scores, 0.95) == set()


def test_classifier_takes_over_after_enough_examples(seeded, tmp_path, service, client):
    from conftest import make_image

    root = tmp_path / "many"
    for i in range(2 * MIN_EXAMPLES + 2):
        make_image(root / f"{i:02}.png", (i * 9, 0, 0))
    lib = seeded.add_library(root)
    library.scan(seeded, lib)
    prompts_to_axes(seeded, service)
    hand = tag_id(seeded, "subject", "hand")
    engine = SuggestionEngine(seeded, client)
    images = seeded.images(lib.id)
    for n, image in enumerate(images):
        # Images with a "hand" are on axis 7, the others on axis 6.
        has_hand = n % 2 == 0
        service.image_vectors[seeded.image_path(image).name] = unit(7 if has_hand else 6)
    engine.ensure_embeddings([i.id for i in images[:-1]])
    for n, image in enumerate(images[:-1]):
        validate(seeded, image.id, {hand} if n % 2 == 0 else set(), set())
    sizes = engine.retrain()
    assert hand in sizes and hand in engine.trained_tags()
    last = images[-1]  # n = 21 -> no hand, axis 6
    assert engine.scores(last.id)[hand] < 0.2
    service.image_vectors[seeded.image_path(last).name] = unit(7)
    seeded.update_image(last.id, embed_model=None)
    assert engine.scores(last.id)[hand] > 0.8


def test_retrain_counter(seeded, client):
    engine = SuggestionEngine(seeded, client)
    results = [engine.note_validation() for _ in range(50)]
    assert results.count(True) == 2 and results[24] and results[49]


def test_offline_backoff(service):
    now = [0.0]
    client = SuggestClient(transport=httpx.MockTransport(service.handler), clock=lambda: now[0])
    service.up = False
    with pytest.raises(Offline):
        client.info()
    service.up = True
    with pytest.raises(Offline):
        client.info()  # still backing off, no request made
    now[0] = 1.5
    assert client.info().model == "fake"
    assert client.online
