from fastapi import FastAPI
import time
import math

app = FastAPI(title="Sys-AIMS API")

@app.get("/")
def health_check():
    return {"status": "ok", "service": "Sys-AIMS API"}

@app.post("/predict")
def predict():
    start = time.time()
    total = 0.0
    for i in range(2_000_000):
        total += math.sqrt(i)
    elapsed = time.time() - start
    return {"result": "inference complete", "elapsed_seconds": round(elapsed, 3)}

@app.get("/api/stats")
def get_stats():
    return {"cpu_usage": "placeholder", "workloads": 0}