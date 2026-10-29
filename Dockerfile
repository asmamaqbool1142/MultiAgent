FROM python:3.11-slim

WORKDIR /app

# Install dependencies from project metadata so the image stays reproducible.
COPY pyproject.toml README.md ./
COPY backend ./backend
RUN python -m pip install --no-cache-dir .

COPY . .

CMD ["uvicorn", "backend.main:app", "--host", "0.0.0.0", "--port", "8000", "--no-access-log"]
