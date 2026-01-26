FROM ghcr.io/zhayujie/chatgpt-on-wechat:latest

USER root

# 用本仓库代码覆盖基础镜像内置代码，确保本地改动（如 config.py）在构建后的镜像中生效
WORKDIR /app
COPY . /app

ENTRYPOINT ["/entrypoint.sh"]
