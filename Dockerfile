# 充能面包 bot
#
# 两个环境变量是踩坑换来的，不是可选项：
#   PYTHONUNBUFFERED=1 —— 日志被块缓冲时 `docker logs` 看不到实时输出。
#                         这个坑在本地验证时真的踩到过：进程在跑，日志一行不见。
#   TZ=Asia/Shanghai   —— 容器默认 UTC。不钉死的话"每日 0 点重置"会在
#                         北京时间早上 8 点发生，签到日界整个错位。
FROM python:3.13-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    TZ=Asia/Shanghai \
    PIP_NO_CACHE_DIR=1

# tzdata 让 TZ 生效；sqlite3 的时区依赖系统 tz 数据库
RUN apt-get update \
    && apt-get install -y --no-install-recommends tzdata \
    && rm -rf /var/lib/apt/lists/* \
    && ln -snf /usr/share/zoneinfo/$TZ /etc/localtime \
    && echo $TZ > /etc/timezone

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY chargebread/ ./chargebread/
COPY 充能面包.png ./

# 数据库落在挂载卷上；用非 root 跑
RUN useradd --create-home --uid 10001 bread \
    && mkdir -p /app/data \
    && chown -R bread:bread /app
USER bread

VOLUME ["/app/data"]

# 只依赖出站长连接，不监听端口 —— 所以没有 EXPOSE
CMD ["python", "-m", "chargebread"]
