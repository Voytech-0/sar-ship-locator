"""Pure detection, masking, validation and AIS-parsing algorithms behind the contracts' Protocols."""

from pipeline.algorithms.ais_dma import ParseStats, iter_dma_positions
from pipeline.algorithms.cfar import CaCfarDetector, TooManyHitsError
from pipeline.algorithms.landmask import PolygonLandMask
from pipeline.algorithms.validator import NearestNeighbourValidator

__all__ = [
    "CaCfarDetector",
    "NearestNeighbourValidator",
    "ParseStats",
    "PolygonLandMask",
    "TooManyHitsError",
    "iter_dma_positions",
]
