from fastapi import FastAPI
import time
import math
import os
import psycopg2

app = FastAPI(title="Sys-AIMS API")

DB_HOST = os.environ.get("POSTGRES_HOST", "localhost")
DB_USER = os.environ.get("POSTGRES_USER", "sysaims")
DB_PASSWORD = os.environ.get("POSTGRES_PASSWORD", "")
DB_NAME = os.environ.get("POSTGRES_DB", "sysaims")


def get_connection():
    return psycopg2.connect(
        host=DB_HOST, user=DB_USER, password=DB_PASSWORD, dbname=DB_NAME, connect_timeout=3
    )


def init_db():
    try:
        conn = get_connection()
        cur = conn.cursor()
        cur.execute("""
            CREATE TABLE IF NOT EXISTS prediction_logs (
                id SERIAL PRIMARY KEY,
                elapsed_seconds REAL NOT NULL,
                created_at TIMESTAMP NOT NULL DEFAULT NOW()
            )
        """)
        conn.commit()
        cur.close()
        conn.close()
        print("DB 연결 및 테이블 준비 완료")
    except Exception as e:
        print(f"DB 초기화 실패 (DB 없이 계속 실행): {e}")


@app.on_event("startup")
def on_startup():
    init_db()


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

    try:
        conn = get_connection()
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO prediction_logs (elapsed_seconds) VALUES (%s)", (elapsed,)
        )
        conn.commit()
        cur.close()
        conn.close()
    except Exception as e:
        print(f"DB 기록 실패: {e}")

    return {"result": "inference complete", "elapsed_seconds": round(elapsed, 3)}


@app.get("/api/stats")
def get_stats():
    try:
        conn = get_connection()
        cur = conn.cursor()
        cur.execute("SELECT COUNT(*), AVG(elapsed_seconds) FROM prediction_logs")
        count, avg_elapsed = cur.fetchone()
        cur.close()
        conn.close()
        return {
            "workloads": count or 0,
            "avg_elapsed_seconds": round(avg_elapsed, 3) if avg_elapsed else 0,
        }
    except Exception as e:
        return {"error": f"DB 연결 실패: {e}", "workloads": 0}