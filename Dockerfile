FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 DJANGO_DEBUG=0
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
RUN useradd -m app && mkdir -p /data && chown -R app /app /data
USER app
ENV DB_PATH=/data/db.sqlite3
# Build-time secret only so collectstatic can import settings; the real one comes from the environment at run time.
RUN DJANGO_SECRET_KEY=build-only python manage.py collectstatic --noinput
EXPOSE 8000
CMD ["sh", "-c", "python manage.py migrate --noinput && gunicorn quietmorning.wsgi:application --bind 0.0.0.0:8000 --workers 2 --access-logfile -"]
