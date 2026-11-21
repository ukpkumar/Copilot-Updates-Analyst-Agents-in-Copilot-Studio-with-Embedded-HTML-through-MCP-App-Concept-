FROM python:3.12-slim
WORKDIR /app
COPY server.py ./
COPY widget ./widget
ENV PORT=8000
EXPOSE 8000
USER 1000
CMD ["python", "server.py"]
