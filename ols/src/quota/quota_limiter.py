"""Abstract class that is parent for all quota limiter implementations."""

from abc import ABC, abstractmethod
from typing import TYPE_CHECKING, Optional

import psycopg2

if TYPE_CHECKING:
    from ols.app.models.config import PostgresConfig


class QuotaLimiter(ABC):
    """Abstract class that is parent for all quota limiter implementations."""

    @abstractmethod
    def available_quota(self, subject_id: str) -> int:
        """Retrieve available quota for given user."""

    @abstractmethod
    def revoke_quota(self) -> None:
        """Revoke quota for given user."""

    @abstractmethod
    def increase_quota(self) -> None:
        """Increase quota for given user."""

    @abstractmethod
    def ensure_available_quota(self, subject_id: str = "") -> None:
        """Ensure that there's avaiable quota left."""

    @abstractmethod
    def consume_tokens(
        self, input_tokens: int, output_tokens: int, subject_id: str = ""
    ) -> None:
        """Consume tokens by given user."""

    @abstractmethod
    def __init__(self) -> None:
        """Initialize connection config."""
        self.connection_config: Optional[PostgresConfig] = None

    # pylint: disable=W0201
    def connect(self) -> None:
        """Initialize connection to database."""
        config = self.connection_config
        self.connection = psycopg2.connect(
            host=config.host,
            port=config.port,
            user=config.user,
            password=config.password,
            dbname=config.dbname,
            sslmode=config.ssl_mode,
            # sslrootcert=config.ca_cert_path,
            gssencmode=config.gss_encmode,
        )
        self.connection.autocommit = True
