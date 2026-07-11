FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Cache bust — đổi số này để ép Docker build lại hoàn toàn
ARG CACHE_BUST=20260711_v99
RUN echo "Cache bust: $CACHE_BUST"

COPY . .

# Verify các router mới có mặt
RUN python3 -c "from routers.update import router_public; print('update OK')"
RUN python3 -c "from routers.notify import router_public; print('notify OK')"
RUN python3 -c "from routers.refund import router_public; print('refund OK')"

CMD ["sh", "-c", "uvicorn main:app --host 0.0.0.0 --port ${PORT:-8000}"]