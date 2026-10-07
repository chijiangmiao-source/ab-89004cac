FROM python:3.11-slim

WORKDIR /srv

COPY app ./app
COPY tests ./tests
COPY verify ./verify

ENV PYTHONUNBUFFERED=1 \
    HOST=0.0.0.0 \
    PORT=8080 \
    DB=/data/decisions.db

EXPOSE 8080
VOLUME ["/data"]

# 纯标准库实现，无第三方依赖；构建检查即编译通过
RUN python -m compileall -q app tests verify

CMD ["python", "-m", "app.server"]
