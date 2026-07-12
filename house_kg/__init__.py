"""house.kg — scraper and HuggingFace dataset builder for Kyrgyz real estate.

Layout
------
    config.py        typed configuration (dataclasses, loaded from config.yaml)
    constants.py     domain constants: deals, regions, label map, CSS selectors
    http_client.py   thread-safe session pool with retries
    models.py        dataset records (Listing, User, Company/Complex, Review, Photo)
    storage.py       append-only JSONL tables + photo store  (resumability)
    parsers/         one parser per page type
    crawler/         crawl stages and the pipeline that sequences them
    dataset/         HuggingFace packaging (Parquet subsets, embedded images)
    logging_utils/   rich console + file logging, multi-track progress bars
    utils/           transliteration, Russian dates, number extraction
"""

from .config import Config
from .crawler import Pipeline
from .dataset import HFDatasetBuilder
from .storage import Storage
from .validate import Validator

__version__ = "1.0.0"

__all__ = ["Config", "HFDatasetBuilder", "Pipeline", "Storage", "Validator", "__version__"]
