# GigaAM Transcriber v0.1.0

Local Russian speech-to-text with GigaAM v3 E2E, FastAPI, Docker and DOCX export.
Локальная транскрибация русской речи с экспортом в DOCX.

## Highlights / Возможности

- GigaAM v3 E2E RNNT/CTC through `onnx-asr` + ONNX Runtime GPU
- Streaming `ffmpeg → Silero VAD → batched ASR`, long files up to 3 hours
- SQLite queue, SSE progress, worker restart recovery
- Valid DOCX: Cyrillic, timestamps, metadata, page numbers
- One Docker image for web + worker, GPU only in the worker
- Offline model volume, CPU mode, optional Caddy TLS profile
- 56 tests, `ruff`/`mypy` clean

## Quick start

```bash
cp .env.example .env
make init
make up
# http://localhost:8000
```

## Measured on RTX 4070 Ti Super 16 GB

- 1-hour MP4, RNNT fp32: **39.5 s**, **91× real time**, 4.0 GB peak VRAM
- 3-hour WAV (silence): 26.6 s, RSS 1.1–1.9 GB, valid DOCX
- Image without weights: 9.05 GB uncompressed; `docker save` archive 3.38 GB

## Documentation

- [README](README.md)
- [Bilingual overview](OVERVIEW.md)
- [Validation report](VALIDATION.md)
- [Architecture decisions](DECISIONS.md)
- [Example DOCX](samples/example.docx)

## License

MIT. GigaAM v3, onnx-asr, Silero VAD and other components are attributed in [NOTICE](NOTICE).
