FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# The catalog is read-only, so the SQLite database is built from the fixtures
# at image build time and shipped inside the image. Each deploy rebuilds it.
RUN DJANGO_SECRET_KEY=build-only DEBUG=False \
    sh -c "python manage.py collectstatic --noinput && \
           python manage.py migrate --noinput && \
           python manage.py loaddata catalog/fixtures/*.json"

EXPOSE 8080
CMD ["gunicorn", "duke_planner.wsgi:application", "--bind", "0.0.0.0:8080", "--workers", "3"]
