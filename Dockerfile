FROM python:3.12-slim

WORKDIR /app
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 FHR_STORE=/app/.fhr/controlplane.db FHR_VIEW_STORE=/app/.fhr/views.db

COPY pyproject.toml README.md LICENSE ./
COPY src ./src
COPY catalogue ./catalogue
COPY data ./data
COPY onboarding ./onboarding
RUN pip install --no-cache-dir -e .

EXPOSE 8000
CMD ["fhr", "serve", "--host", "0.0.0.0", "--port", "8000"]
