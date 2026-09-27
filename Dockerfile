# DocWork：一个镜像，Web、各类 worker、维护进程共用，只是启动命令不同。
# 在服务器（ARM64）上直接构建：docker compose build
FROM python:3.11-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    DEBIAN_FRONTEND=noninteractive \
    LANG=C.UTF-8 \
    LC_ALL=C.UTF-8 \
    MPLCONFIGDIR=/data/tmp/mpl \
    XDG_CACHE_HOME=/data/tmp/cache \
    HOME=/data/tmp/home \
    SAL_USE_VCLPLUGIN=svp

# 系统组件：LibreOffice（含 Math 以渲染 Word 公式）、中文与替代字体、Ghostscript、qpdf、Pandoc、
# Tesseract（OCR 备用引擎）、bubblewrap（可用时作为额外沙箱）、restic（可选备份）
RUN apt-get update && apt-get install -y --no-install-recommends \
        libreoffice-core libreoffice-writer libreoffice-calc libreoffice-impress libreoffice-draw libreoffice-math \
        fonts-noto-cjk fonts-noto-cjk-extra fonts-liberation2 fonts-crosextra-carlito fonts-crosextra-caladea \
        fonts-dejavu-core fonts-arphic-ukai fontconfig \
        ghostscript qpdf pandoc \
        tesseract-ocr tesseract-ocr-chi-sim tesseract-ocr-eng \
        bubblewrap restic tini curl sqlite3 ca-certificates \
        libgl1 libglib2.0-0 \
    && rm -rf /var/lib/apt/lists/*

COPY deploy/fonts.conf /etc/fonts/local.conf
RUN fc-cache -f

WORKDIR /app
COPY requirements.txt requirements-optional.txt ./
RUN pip install -r requirements.txt \
    && (pip install -r requirements-optional.txt || echo "可选组件安装失败，将使用内置备用实现")

# 预热：确认 OCR 模型可以加载（模型随 rapidocr 包提供，运行时不需要联网）
RUN python -c "from rapidocr_onnxruntime import RapidOCR; RapidOCR(); print('RapidOCR 可用')" || echo "RapidOCR 不可用，使用 Tesseract"

COPY app ./app
COPY static ./static
COPY tests ./tests

RUN useradd --uid 1000 --create-home --home-dir /home/docwork docwork \
    && mkdir -p /data && chown docwork:docwork /data
USER docwork

VOLUME ["/data"]
EXPOSE 8000
ENTRYPOINT ["/usr/bin/tini", "--"]
# 不让 Uvicorn 改写客户端地址（--forwarded-allow-ips "*" 会信任客户端自己伪造的 X-Forwarded-For）；
# 真实地址由应用只从可信代理（DW_TRUSTED_PROXIES）的 X-Real-IP 读取，见 app/web/common.py:client_ip
CMD ["python", "-m", "uvicorn", "app.web.app:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "2", "--no-proxy-headers"]
