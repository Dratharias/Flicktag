# Flicktag — dev shortcuts (Python 3.12 via uv) and the suggestion container (Podman).
VENV := .venv
PY := $(VENV)/bin/python
APPS := $(HOME)/.local/share/applications

IMAGE := localhost/flicktag-suggest:latest
CONTAINER := flicktag-suggest
VOLUME := flicktag-models
MODEL ?= google/siglip2-base-patch16-224

$(VENV): pyproject.toml
	uv venv --allow-existing --python 3.12 $(VENV)
	uv pip install --python $(PY) -e '.[test]'
	touch $(VENV)

.PHONY: run test test-service install uninstall build up down restart logs
run: $(VENV)
	$(VENV)/bin/flicktag $(ARGS)

test: $(VENV)
	QT_QPA_PLATFORM=offscreen $(PY) -m pytest -q $(ARGS)

test-service: $(VENV)
	QT_QPA_PLATFORM=offscreen $(PY) -m pytest -q -m service $(ARGS)

install:
	uv tool install --python 3.12 --force .
	install -Dm644 packaging/flicktag.desktop $(APPS)/flicktag.desktop
	update-desktop-database $(APPS) || true

uninstall:
	uv tool uninstall flicktag || true
	rm -f $(APPS)/flicktag.desktop

build:
	podman build -t $(IMAGE) suggest-service

up:
	podman image exists $(IMAGE) || $(MAKE) build
	podman run -d --replace --name $(CONTAINER) \
		--device nvidia.com/gpu=all --security-opt=label=disable \
		-p 127.0.0.1:8765:8765 \
		-v $(VOLUME):/models \
		-e FLICKTAG_MODEL=$(MODEL) \
		$(IMAGE)

down:
	podman stop --ignore $(CONTAINER)
	podman rm --ignore $(CONTAINER)

restart: down up

logs:
	podman logs -f $(CONTAINER)
