FROM python:3.12-alpine
# ffmpeg: audio extraction, video remux, ad/backfill decoding. tesseract: screen clock OCR.
RUN apk add --no-cache ffmpeg tzdata tesseract-ocr tesseract-ocr-data-eng
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY fone ./fone
ENV PYTHONUNBUFFERED=1
EXPOSE 8788
CMD ["uvicorn", "fone.app:app", "--host", "0.0.0.0", "--port", "8788", "--no-access-log"]
