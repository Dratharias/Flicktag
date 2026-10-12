"""Against the real container (`make up`); run with `make test-service`."""

import pytest
from conftest import make_image

from flicktag.suggest.client import SuggestClient

pytestmark = pytest.mark.service


def test_real_service_embeds(tmp_path):
    client = SuggestClient()
    info = client.info()
    red = make_image(tmp_path / "red.png", (255, 0, 0))
    broken = tmp_path / "broken.jpg"
    broken.write_bytes(b"nope")
    images = client.embed_images([red, broken])
    texts = client.embed_texts(["a red square", "a photo of a dog"])
    assert images[0].shape == (info.dim,) and images[1] is None
    assert float(images[0] @ texts[0]) > float(images[0] @ texts[1])
