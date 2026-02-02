FROM python:3.9-slim-buster

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Cloud Run will set the PORT environment variable
ENV FLASK_APP=breeze_proxy_app.py
ENV FLASK_RUN_HOST=0.0.0.0

CMD ["python", "breeze_proxy_app.py"]
