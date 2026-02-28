# Databricks notebook source
"""
databricks_pipeline.py — Medallion Pipeline (Local reader + Cloud streaming)

Modes
─────
  1. LOCAL  : Lit les Parquet Silver/Gold deja produits en temps reel par
              le streaming pipeline inline (streaming_pipeline.py).
              Utile pour inspecter, requeter, ou re-exporter les donnees.

  2. CLOUD  : PySpark + Auto Loader → Silver managed Delta table.
              Utilise uniquement sur Databricks quand les Bronze NDJSON
              ont ete transferes vers un object store (ADLS/S3/DBFS).
"""

from __future__ import annotations

from pathlib import Path

from .._paths import SILVER_DIR, GOLD_DIR

# ─────────────────────────────────────────────────────────────────────────────
# Configuration
# ─────────────────────────────────────────────────────────────────────────────
SILVER_LOCAL_PATH: str = str(SILVER_DIR)
GOLD_LOCAL_PATH: str = str(GOLD_DIR)

BRONZE_CLOUD_PATH: str = "/mnt/network-analytics/bronze/data"
SILVER_TABLE: str = "network_analytics.silver.packet_events"
GOLD_TABLE: str = "network_analytics.gold.traffic_hourly"
CHECKPOINT_PATH: str = "/mnt/network-analytics/_checkpoints/silver_packets"


# ─────────────────────────────────────────────────────────────────────────────
# Local reader — reads the Parquet files already produced by the streaming
# pipeline (streaming_pipeline.py) embedded in capture_agent.py
# ─────────────────────────────────────────────────────────────────────────────
def run_local_pipeline() -> None:
    """
    Read Silver/Gold Parquet produced in real-time by the inline streaming
    pipeline.  No Spark, no JVM — pure PyArrow.
    """
    import pyarrow as pa
    import pyarrow.dataset as ds

    date_partitioning = ds.partitioning(
        pa.schema([("event_date", pa.date32())]), flavor="hive"
    )

    silver_path = Path(SILVER_LOCAL_PATH)
    gold_path = Path(GOLD_LOCAL_PATH)

    if not silver_path.exists():
        print("[SKIP] No Silver data found. Run capture_agent.py first.")
        return

    print(f"[Silver] Reading from: {silver_path}/")
    silver_ds = ds.dataset(silver_path, format="parquet", partitioning=date_partitioning)
    silver_table = silver_ds.to_table()
    print(f"[Silver] Records: {silver_table.num_rows}")
    print(silver_table.schema)
    print()

    if gold_path.exists():
        print(f"[Gold] Reading from: {gold_path}/")
        gold_ds = ds.dataset(gold_path, format="parquet", partitioning=date_partitioning)
        gold_table = gold_ds.to_table()
        print(f"[Gold] Rows: {gold_table.num_rows}")
        print(gold_table.slice(0, min(20, gold_table.num_rows)).to_pydict())
    else:
        print("[SKIP] No Gold data found yet.")


# ─────────────────────────────────────────────────────────────────────────────
# Cloud pipeline — PySpark + Auto Loader (Databricks only)
# ─────────────────────────────────────────────────────────────────────────────
def run_cloud_pipeline() -> None:
    """
    Pipeline Databricks cloud : Auto Loader -> Silver managed Delta table.
    DDL idempotent + streaming trigger(availableNow).
    """
    from pyspark.sql import DataFrame, SparkSession
    from pyspark.sql import functions as F
    from pyspark.sql.types import (
        IntegerType,
        LongType,
        StringType,
        StructField,
        StructType,
    )

    BRONZE_SCHEMA = StructType([
        StructField("timestamp", StringType(), nullable=False),
        StructField("src_ip", StringType(), nullable=False),
        StructField("dst_ip", StringType(), nullable=False),
        StructField("src_port", IntegerType(), nullable=True),
        StructField("dst_port", IntegerType(), nullable=True),
        StructField("protocol", StringType(), nullable=False),
        StructField("length", LongType(), nullable=False),
        StructField("ttl", IntegerType(), nullable=True),
        StructField("flags", StringType(), nullable=True),
        StructField("agent_id", StringType(), nullable=False),
    ])

    def _transform_to_silver(bronze_df: DataFrame) -> DataFrame:
        return (
            bronze_df
            .withColumn("event_ts", F.to_timestamp("timestamp"))
            .withColumn("event_hour", F.date_trunc("hour", F.col("event_ts")))
            .withColumn("event_date", F.to_date("event_ts"))
            .withColumn("protocol", F.upper(F.col("protocol")))
            .withColumn("ingested_at", F.current_timestamp())
            .withColumn(
                "src_network",
                F.concat_ws(
                    ".",
                    F.split(F.col("src_ip"), r"\.").getItem(0),
                    F.split(F.col("src_ip"), r"\.").getItem(1),
                    F.split(F.col("src_ip"), r"\.").getItem(2),
                    F.lit("0/24"),
                ),
            )
            .drop("timestamp")
            .filter(F.col("event_ts").isNotNull())
        )

    spark = SparkSession.builder.getOrCreate()

    spark.sql(f"""
        CREATE TABLE IF NOT EXISTS {SILVER_TABLE} (
            event_ts        TIMESTAMP   NOT NULL,
            event_hour      TIMESTAMP,
            event_date      DATE,
            src_ip          STRING      NOT NULL,
            dst_ip          STRING      NOT NULL,
            src_port        INT,
            dst_port        INT,
            protocol        STRING      NOT NULL,
            length          BIGINT      NOT NULL,
            ttl             INT,
            flags           STRING,
            agent_id        STRING      NOT NULL,
            ingested_at     TIMESTAMP,
            src_network     STRING,
            _rescued_data   STRING
        )
        USING DELTA
        PARTITIONED BY (event_date)
        TBLPROPERTIES (
            'delta.autoOptimize.optimizeWrite' = 'true',
            'delta.autoOptimize.autoCompact'   = 'true',
            'delta.tuneFileSizesForRewrites'   = 'true'
        )
    """)

    bronze_stream = (
        spark.readStream.format("cloudFiles")
        .option("cloudFiles.format", "json")
        .option("cloudFiles.schemaLocation", f"{CHECKPOINT_PATH}/_schema")
        .option("cloudFiles.inferColumnTypes", "false")
        .option("cloudFiles.schemaEvolutionMode", "rescue")
        .option("rescuedDataColumn", "_rescued_data")
        .schema(BRONZE_SCHEMA)
        .load(BRONZE_CLOUD_PATH)
    )

    silver_stream = _transform_to_silver(bronze_stream)

    (
        silver_stream.writeStream
        .format("delta")
        .outputMode("append")
        .option("checkpointLocation", CHECKPOINT_PATH)
        .option("mergeSchema", "true")
        .partitionBy("event_date")
        .trigger(availableNow=True)
        .toTable(SILVER_TABLE)
    )


# ─────────────────────────────────────────────────────────────────────────────
# Entry — python databricks_pipeline.py [--local | --cloud]
# ─────────────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    import sys

    mode = sys.argv[1] if len(sys.argv) > 1 else "--local"

    if mode == "--cloud":
        run_cloud_pipeline()
    else:
        run_local_pipeline()
