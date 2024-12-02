FROM python:3.10-slim

ARG TWINE_USERNAME
ARG TWINE_PASSWORD

# Set environment variables for Python
ENV PYTHONDONTWRITEBYTECODE 1
ENV PYTHONUNBUFFERED 1

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    curl \
    && rm -rf /var/lib/apt/lists/*

RUN pip install --upgrade pip setuptools wheel twine

COPY . .

RUN python setup.py sdist bdist_wheel && ls -l dist/
RUN twine upload dist/*
