import psycopg2
from psycopg2.extensions import ISOLATION_LEVEL_AUTOCOMMIT

conn = psycopg2.connect(
    host="postgresql-db-dev-1.postgres.database.azure.com",
    port=5432,
    user="carwash_admin_dev",
    password="Postgre@2026",
    dbname="postgres",
    sslmode="require",
)
conn.set_isolation_level(ISOLATION_LEVEL_AUTOCOMMIT)
cur = conn.cursor()
cur.execute("SELECT 1 FROM pg_database WHERE datname = 'HMSstage'")
if cur.fetchone():
    print("HMSstage already exists")
else:
    cur.execute('CREATE DATABASE "HMSstage"')
    print("Created HMSstage database")
cur.close()
conn.close()
