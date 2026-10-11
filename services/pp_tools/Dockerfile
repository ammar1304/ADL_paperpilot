# pp-tools: deterministic helpers for the PaperPilot pipeline.
# Python 3.13 matches the interpreter the test suite was run with.
FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /srv

COPY requirements.txt .
RUN pip install -r requirements.txt

COPY app ./app

# Run as an unprivileged user.
RUN useradd --system --uid 10001 pptools
USER pptools

EXPOSE 8000

# python is used for the healthcheck because the slim image has no curl or wget.
HEALTHCHECK --interval=10s --timeout=5s --start-period=20s --retries=5 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=4).status == 200 else 1)"

CMD ["uvicorn", "app.main:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000"]
