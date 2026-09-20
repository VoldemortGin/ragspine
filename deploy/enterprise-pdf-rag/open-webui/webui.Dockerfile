# Official v0.11.3 slim multi-platform manifest, verified without pulling layers.
FROM ghcr.io/open-webui/open-webui:v0.11.3-slim@sha256:bb3633af77b35d97783affc9cd8097d8a6dc89fedd5a410ce9a50d85556a870c

# Keep upstream code/frontend unchanged; expose only the project's ASGI boundary.
COPY src/enterprise_pdf_rag/adapters/http/webui_gate.py /opt/enterprise-webui/webui_gate.py
ENTRYPOINT ["python", "/opt/enterprise-webui/webui_gate.py"]
CMD ["--data-dir", "/app/backend/data", "--profile", "aia-source-review"]
