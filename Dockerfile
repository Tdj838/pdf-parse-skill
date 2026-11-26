FROM python:3.12-slim

# The extractor is standard-library only, so there is nothing to install.
# The image exists to pin the interpreter the run was measured on.
WORKDIR /app
COPY . /app

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

# Re-derives every number in characterization.json from the bundled inputs.
CMD ["python3", "src/reproduce.py", "--out", "execution"]
