"""Cache that uses Postgres to store cached values."""

import json
import logging
from typing import Any

import psycopg2

from ols.app.models.config import PostgresConfig
from ols.app.models.models import CacheEntry, MessageDecoder, MessageEncoder
from ols.src.cache.cache import Cache
from ols.src.cache.cache_error import CacheError
from ols.utils.connection_decorator import connection

logger = logging.getLogger(__name__)


class PostgresCache(Cache):
    """Cache that uses Postgres to store cached values.

    The cache itself is stored in following table:

    ```
         Column      |            Type             | Nullable | Default | Storage  |
    -----------------+-----------------------------+----------+---------+----------+
     user_id         | text                        | not null |         | extended |
     conversation_id | text                        | not null |         | extended |
     topic_summary   | text                        | not null |         | extended |
     value           | bytea                       |          |         | extended |
     updated_at      | timestamp without time zone |          |         | plain    |
    Indexes:
        "cache_pkey" PRIMARY KEY, btree (user_id, conversation_id)
        "cache_key_key" UNIQUE CONSTRAINT, btree (key)
        "timestamps" btree (updated_at)
    Access method: heap
    ```
    """

    CREATE_CACHE_TABLE = """
        CREATE TABLE IF NOT EXISTS cache (
            user_id         text NOT NULL,
            conversation_id text NOT NULL,
            value           bytea,
            topic_summary   text,
            updated_at      timestamp,
            PRIMARY KEY(user_id, conversation_id)
        );
        """

    CREATE_INDEX = """
        CREATE INDEX IF NOT EXISTS timestamps
            ON cache (updated_at)
        """

    SELECT_CONVERSATION_HISTORY_STATEMENT = """
        SELECT value
          FROM cache
         WHERE user_id=%s AND conversation_id=%s LIMIT 1
        """

    UPDATE_CONVERSATION_HISTORY_STATEMENT = """
        UPDATE cache
           SET value=%s, updated_at=CURRENT_TIMESTAMP
         WHERE user_id=%s AND conversation_id=%s
        """

    INSERT_CONVERSATION_HISTORY_STATEMENT = """
        INSERT INTO cache(user_id, conversation_id, value, topic_summary, updated_at)
        VALUES (%s, %s, %s, %s, CURRENT_TIMESTAMP)
        """

    DELETE_CONVERSATION_HISTORY_STATEMENT = """
        DELETE FROM cache
         WHERE (user_id, conversation_id) in
               (SELECT user_id, conversation_id FROM cache ORDER BY updated_at LIMIT
        """

    QUERY_CACHE_SIZE = """
        SELECT count(*) FROM cache;
        """

    DELETE_SINGLE_CONVERSATION_STATEMENT = """
        DELETE FROM cache
         WHERE user_id=%s AND conversation_id=%s
        """

    LIST_CONVERSATIONS_STATEMENT = """
        SELECT conversation_id, topic_summary
        FROM cache
        WHERE user_id=%s
        ORDER BY updated_at DESC
    """

    def __init__(self, config: PostgresConfig) -> None:
        """Create a new instance of Postgres cache."""
        self.postgres_config = config

        # initialize connection to DB
        self.connect()
        self.capacity = config.max_entries

    # pylint: disable=W0201
    def connect(self) -> None:
        """Initialize connection to database."""
        logger.info("Connecting to storage")
        # make sure the connection will have known state
        # even if PG is not alive
        self.connection = None
        config = self.postgres_config
        self.connection = psycopg2.connect(
            host=config.host,
            port=config.port,
            user=config.user,
            password=config.password,
            dbname=config.dbname,
            sslmode=config.ssl_mode,
            sslrootcert=config.ca_cert_path,
            gssencmode=config.gss_encmode,
        )
        try:
            self.initialize_cache()
        except Exception as e:
            self.connection.close()
            logger.exception("Error initializing Postgres cache:\n%s", e)
            raise
        self.connection.autocommit = True

    def connected(self) -> bool:
        """Check if connection to cache is alive."""
        if self.connection is None:
            logger.warning("Not connected, need to reconnect later")
            return False
        try:
            with self.connection.cursor() as cursor:
                cursor.execute("SELECT 1")
            logger.info("Connection to storage is ok")
            return True
        except psycopg2.OperationalError as e:
            logger.error("Disconnected from storage: %s", e)
            return False

    def initialize_cache(self) -> None:
        """Initialize cache - clean it up etc."""
        with self.connection.cursor() as cursor:
            logger.info("Initializing table for cache")
            cursor.execute(PostgresCache.CREATE_CACHE_TABLE)

            logger.info("Initializing index for cache")
            cursor.execute(PostgresCache.CREATE_INDEX)

        self.connection.commit()

    @connection
    def get(
        self, user_id: str, conversation_id: str, skip_user_id_check: bool = False
    ) -> list[CacheEntry]:
        """Get the value associated with the given key.

        Args:
            user_id: User identification.
            conversation_id: Conversation ID unique for given user.
            skip_user_id_check: Skip user_id suid check.

        Returns:
            The value associated with the key, or None if not found.
        """
        # just check if user_id and conversation_id are UUIDs
        super().construct_key(user_id, conversation_id, skip_user_id_check)

        with self.connection.cursor() as cursor:
            try:
                value = PostgresCache._select(cursor, user_id, conversation_id)
                if value is None:
                    return []
                history = [CacheEntry.from_dict(cache_entry) for cache_entry in value]
                return history
            except psycopg2.DatabaseError as e:
                logger.error("PostgresCache.get %s", e)
                raise CacheError("PostgresCache.get", e) from e

    @connection
    def insert_or_append(
        self,
        user_id: str,
        conversation_id: str,
        cache_entry: CacheEntry,
        topic_summary: str = "",
        skip_user_id_check: bool = False,
    ) -> None:
        """Set the value associated with the given key.

        Args:
            user_id: User identification.
            conversation_id: Conversation ID unique for given user.
            cache_entry: The `CacheEntry` object to store.
            topic_summary: Summary of the conversation's initial topic.
            skip_user_id_check: Skip user_id suid check.
        """
        value = cache_entry.to_dict()
        # the whole operation is run in one transaction
        with self.connection.cursor() as cursor:
            try:
                old_value = self._select(cursor, user_id, conversation_id)
                if old_value:
                    old_value.append(value)
                    PostgresCache._update(
                        cursor,
                        user_id,
                        conversation_id,
                        json.dumps(old_value, cls=MessageEncoder).encode("utf-8"),
                    )
                else:
                    PostgresCache._insert(
                        cursor,
                        user_id,
                        conversation_id,
                        json.dumps([value], cls=MessageEncoder).encode("utf-8"),
                        topic_summary,
                    )
                    PostgresCache._cleanup(cursor, self.capacity)
                # commit is implicit at this point
            except psycopg2.DatabaseError as e:
                logger.error("PostgresCache.insert_or_append: %s", e)
                raise CacheError("PostgresCache.insert_or_append", e) from e

    @connection
    def delete(
        self, user_id: str, conversation_id: str, skip_user_id_check: bool = False
    ) -> bool:
        """Delete conversation history for a given user_id and conversation_id.

        Args:
            user_id: User identification.
            conversation_id: Conversation ID unique for given user.
            skip_user_id_check: Skip user_id suid check.

        Returns:
            bool: True if the conversation was deleted, False if not found.

        """
        with self.connection.cursor() as cursor:
            try:
                return PostgresCache._delete(cursor, user_id, conversation_id)
            except psycopg2.DatabaseError as e:
                logger.error("PostgresCache.delete: %s", e)
                raise CacheError("PostgresCache.delete", e) from e

    @connection
    def list(
        self, user_id: str, skip_user_id_check: bool = False
    ) -> list[dict[str, str]]:
        """List all conversations for a given user_id.

        Args:
            user_id: User identification.
            skip_user_id_check: Skip user_id suid check.

        Returns:
             A list of dictionaries containing conversation_id and topic_summary

        """
        with self.connection.cursor() as cursor:
            try:
                cursor.execute(PostgresCache.LIST_CONVERSATIONS_STATEMENT, (user_id,))
                rows = cursor.fetchall()
                return [
                    {"conversation_id": row[0], "topic_summary": row[1]} for row in rows
                ]
            except psycopg2.DatabaseError as e:
                logger.error("PostgresCache.list: %s", e)
                raise CacheError("PostgresCache.list", e) from e

    def ready(self) -> bool:
        """Check if the cache is ready.

        Postgres cache checks if the connection is alive.

        Returns:
            True if the cache is ready, False otherwise.
        """
        # TODO: when the connection is closed and the database is back online,
        # we need to reestablish the connection => implement this there?
        # (it will be reconnected on any other DB operation anyway)
        if self.connection is None or self.connection.closed == 1:
            return False
        try:
            return self.connection.poll() == psycopg2.extensions.POLL_OK
        except (psycopg2.OperationalError, psycopg2.InterfaceError):
            # OperationalError - the once alive connection is closed
            # InterfaceError - cannot reach the database server
            return False

    @staticmethod
    def _select(
        cursor: psycopg2.extensions.cursor,
        user_id: str,
        conversation_id: str,
        skip_user_id_check: bool = False,
    ) -> Any:
        """Select conversation history for given user_id and conversation_id."""
        cursor.execute(
            PostgresCache.SELECT_CONVERSATION_HISTORY_STATEMENT,
            (user_id, conversation_id),
        )
        value = cursor.fetchone()

        # check if history exists at all
        if value is None:
            return None

        # check the retrieved value
        if len(value) != 1:
            raise ValueError("Invalid value read from cache:", value)

        # convert from memoryview object to a string
        text_value = str(value[0], "utf-8")
        deserialized = json.loads(text_value, cls=MessageDecoder)

        # try to deserialize the value
        return deserialized

    @staticmethod
    def _update(
        cursor: psycopg2.extensions.cursor,
        user_id: str,
        conversation_id: str,
        value: bytes,
    ) -> None:
        """Update conversation history for given user_id and conversation_id."""
        cursor.execute(
            PostgresCache.UPDATE_CONVERSATION_HISTORY_STATEMENT,
            (value, user_id, conversation_id),
        )

    @staticmethod
    def _insert(
        cursor: psycopg2.extensions.cursor,
        user_id: str,
        conversation_id: str,
        value: bytes,
        topic_summary: str,
    ) -> None:
        """Insert new conversation history for given user_id and conversation_id."""
        cursor.execute(
            PostgresCache.INSERT_CONVERSATION_HISTORY_STATEMENT,
            (user_id, conversation_id, value, topic_summary),
        )

    @staticmethod
    def _cleanup(cursor: psycopg2.extensions.cursor, capacity: int) -> None:
        """Perform cleanup old conversation histories."""
        cursor.execute(PostgresCache.QUERY_CACHE_SIZE)
        value = cursor.fetchone()
        if value is not None:
            count = value[0]
            limit = count - capacity
            if limit > 0:
                cursor.execute(
                    f"{PostgresCache.DELETE_CONVERSATION_HISTORY_STATEMENT} {count - capacity})"
                )

    @staticmethod
    def _delete(
        cursor: psycopg2.extensions.cursor, user_id: str, conversation_id: str
    ) -> bool:
        """Delete conversation history for given user_id and conversation_id."""
        cursor.execute(
            PostgresCache.DELETE_SINGLE_CONVERSATION_STATEMENT,
            (user_id, conversation_id),
        )
        return cursor.fetchone() is not None
