# Streamlit front end for the NL -> Snowflake Lambda.
#
# This container needs NO AWS permissions: it only makes an outbound HTTPS call
# to API Gateway. Bedrock and Snowflake access belong to the Lambda's own role,
# not here. Do not attach an ECS task role with Snowflake or Bedrock rights.
FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# Dependencies first so image layers cache across app-code changes.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app.py api_client.py ./

# Run unprivileged. Created after pip install so site-packages stays root-owned.
RUN adduser --system --group --home /app appuser
USER appuser

EXPOSE 8501

# Streamlit's own health endpoint. `slim` has no curl, so use urllib.
HEALTHCHECK --interval=30s --timeout=5s --start-period=25s --retries=3 \
    CMD python -c "import sys,urllib.request; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8501/_stcore/health', timeout=4).status == 200 else 1)"

# The API URL is injected at run time (SNOWFLAKE_NL_API_URL), never baked in.
CMD ["streamlit", "run", "app.py", \
     "--server.port=8501", \
     "--server.address=0.0.0.0", \
     "--server.headless=true", \
     "--browser.gatherUsageStats=false"]
