FROM python:3.11-slim

WORKDIR /app

# Copy requirements first for build caching
COPY requirements.txt ./

RUN pip install --no-cache-dir -r requirements.txt

# Now copy your Python modules in one go
COPY . .

EXPOSE 8000

CMD ["python", "collector.py"]
