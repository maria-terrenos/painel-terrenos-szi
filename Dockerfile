FROM python:3.11-slim

WORKDIR /app

# Instala dependências do sistema necessárias para psycopg2
RUN apt-get update && apt-get install -y --no-install-recommends \
    libpq-dev \
    && rm -rf /var/lib/apt/lists/*

# Copia e instala dependências Python
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copia o código da aplicação
COPY backend/ ./backend/
COPY frontend/ ./frontend/

# Cria diretório para cache (volume pode ser montado aqui)
RUN mkdir -p /app/data

# Variáveis de ambiente padrão
ENV APP_PORT=8000
ENV PYTHONUNBUFFERED=1

EXPOSE 8000

# Roda a partir da pasta backend
WORKDIR /app/backend

CMD ["python", "-m", "uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8000"]
