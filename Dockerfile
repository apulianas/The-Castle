FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

COPY requirements.txt .
# RapidOCR depends on the GUI build of OpenCV, which needs libGL; the headless
# build does the same image work without it.
RUN pip install --no-cache-dir -r requirements.txt \
    && pip uninstall -y opencv-python \
    && pip install --no-cache-dir "opencv-python-headless<6"

COPY ravens_bot ./ravens_bot

RUN mkdir -p /data
VOLUME ["/data"]

CMD ["python", "-m", "ravens_bot"]
