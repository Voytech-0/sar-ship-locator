"""Adapters between the pipeline and the outside world (CDSE, DMA, object storage)."""

from .cdse import CdseCatalogue
from .dma import DmaAisSource
from .objectstore import S3ObjectStore, create_store_client
from .s1_fetcher import FetchDiagnostics, Sentinel1S3Fetcher, create_s3_client

__all__ = [
    "CdseCatalogue",
    "DmaAisSource",
    "FetchDiagnostics",
    "S3ObjectStore",
    "Sentinel1S3Fetcher",
    "create_s3_client",
    "create_store_client",
]
