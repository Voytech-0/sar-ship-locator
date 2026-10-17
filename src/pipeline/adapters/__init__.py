"""Adapters between the pipeline and the outside world (CDSE, DMA, object storage)."""

from .cdse import CdseCatalogue
from .dma import DmaAisSource
from .objectstore import MinioObjectStore, create_minio_client
from .s1_fetcher import FetchDiagnostics, Sentinel1S3Fetcher, create_s3_client

__all__ = [
    "CdseCatalogue",
    "DmaAisSource",
    "FetchDiagnostics",
    "MinioObjectStore",
    "Sentinel1S3Fetcher",
    "create_minio_client",
    "create_s3_client",
]
